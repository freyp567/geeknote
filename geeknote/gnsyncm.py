#!/usr/bin/env python2 # noqa: E902
# -*- coding: utf-8 -*-
""" sync evernote notes to mongodb

ATTN still beta testing ahead
known issues:
+ fix encoding / display when logging to console vs logfile (e.g. 'Der Spion des K├╢nigs - reading')  - python3 migration?
+ tags seem to get dropped under not yet determined circumstances
  e.g. removed tag personalkb from "Automating EN backups?"

"""

import os
import sys
import io
import codecs
import argparse
import logging
import re
from datetime import datetime, timedelta
import pytz
import json
import binascii

from evernote.edam.limits.constants import EDAM_USER_NOTES_MAX

import config
from geeknote import GeekNote
from storage import Storage
import tools
from updatenote import UpdateNote


def setup_logging(logname):
    # set default logger
    # FORMAT = "%(asctime)-15s %(module)s %(funcName)s %(lineno)d : %(message)s"
    LOG_FORMAT = '%(asctime)-15s %(levelname)s  %(message)s'
    LOG_FORMAT_2 = '%(asctime)-15s  %(message)s'
    LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
    # TODO add/use logging_gnsyncm.conf to/from app folder
    logging._defaultFormatter = logging.Formatter(u"%(message)s")
    logging.basicConfig(format=LOG_FORMAT_2)  # datefmt=
    logpath = os.path.join(config.APP_DIR, 'gnsyncm.%s.log' % datetime.now().strftime("%Y-%m-%d"))
    logpath = os.path.abspath(logpath)
    print("setup logging, logpath='%s'\n" % logpath)
    if not os.path.isfile(logpath):
        io.open(logpath, 'w', encoding='utf-8-sig').write(u'\n')

    # set default logger (write log to file)
    formatter = logging.Formatter(LOG_FORMAT, LOG_DATEFMT)

    logger = logging.getLogger()  # root logger
    #log_fh = io.open(logpath, "a", encoding="utf-8-sig")
    handler = logging.FileHandler(logpath)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    logger = logging.getLogger(logname)
    handler = logging.StreamHandler(sys.stderr)
    logger.addHandler(handler)
    logger.setLevel(os.environ.get('LOGLEVEL') or logging.INFO)
    logger.info("setup gnsyncm")
    logger.info(u"## äöüéδ\n")
    return logger


logger = setup_logging("en2mongo")


# http://en.wikipedia.org/wiki/Unicode_control_characters
CONTROL_CHARS_RE = re.compile(u'[\x00-\x08\x0e-\x1f\x7f-\x9f]')

FILE_FORMAT = {
    '.md': 'markdown',
    '.html': 'html',
    '.txt': 'text',
}


def remove_control_characters(s):
    return CONTROL_CHARS_RE.sub('', s)


def log(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.error("action %s failed", func.__name__)
            raise
    return wrapper


def all_notebooks(sleep_on_ratelimit=False):
    geeknote = GeekNote(sleepOnRateLimit=sleep_on_ratelimit)
    return [notebook for notebook in geeknote.findNotebooks()]


def all_linked_notebooks():
    geeknote = GeekNote()
    return geeknote.findLinkedNotebooks()


class ENNoteObj:
    """ wrap NoteMetadata object (evernote.edam.notestore.ttypes.NoteMetadata) for mongo sync """

    def __init__(self, note, sleep_on_ratelimit):
        self.sleep_on_ratelimit = sleep_on_ratelimit
        self._note = note
        self._note.content = None

    def load_tags(self):
        self.gn = GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit)
        self.gn.loadNoteTags(self._note)

    def load_content(self):
        self.gn = GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit)
        self.gn.loadNoteContent(self._note)

    def get_image_resource(self, imageInfo):
        guid = self._note.guid
        binary_hash = binascii.unhexlify(imageInfo['hash'])
        try:
            resource = self.gn.handleMedia(guid, binary_hash, lambda r: r)
        except Exception as err:
            # EDAMNotFoundException - what else?
            logger.error('failed to lookup image for %s  - %s', imageInfo, err)
            return None
        return resource

    def __getattr__(self, name):
        notfound = object()
        value = getattr(self._note, name, notfound)
        if value is notfound:
            raise AttributeError(name)
        return value


class GNSyncM:
    """ sync application targeting mongodb """

    notebook_name = None
    notebook_guid = None

    sleep_on_ratelimit = False

    def __init__(self, notebook_name, sleep_on_ratelimit=True):
        # check auth
        if not Storage().getUserToken():
            raise Exception("Auth error. There is not any oAuthToken.")

        # establish mongodb connectivity
        self.updater = UpdateNote(notebook_name)

        # set notebook to sync from
        logger.debug('Sync notebook=%s ...', notebook_name)
        self.notebook_guid,\
            self.notebook_name = self._get_notebook(notebook_name)

        # all is Ok
        self.all_set = True

        self.sleep_on_ratelimit = sleep_on_ratelimit

    def _get_notebook(self, notebook_name):
        """
        Get notebook guid and name.
        Takes default notebook if notebook's name does not select.
        """
        notebooks = GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit).findNotebooks()
        assert notebook_name
        notebook_name = notebook_name.lower()  # avoid troubles with case-sensitivity

        notebook = [item for item in notebooks if item.name.lower() == notebook_name]
        guid = None
        if notebook:
            guid = notebook[0].guid
        else:
            logger.warning("missing notebook %s", notebook_name)

        if not guid:
            notebook = GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit).createNotebook(notebook_name)

            if(notebook):
                logger.info('Notebook "{0}" was'
                            ' created'.format(notebook_name))
            else:
                raise Exception('Notebook "{0}" was'
                                ' not created'.format(notebook_name))

            guid = notebook.guid

        return (guid, notebook_name)

    @log
    def sync(self, changed_after=None):
        """
        Synchronize notes to mongodb
        """
        assert self.all_set, "cannot sync with partial initialization"
        notes = self._get_notes(changed_after)
        if not notes:
            logger.info("no notes found to be synced in %s", self.notebook_name)
            return 0

        logger.info("found %s notes to be synced in notebook %s", len(notes), self.notebook_name)
        synced = 0
        for note in notes:
            if changed_after is not None:
                # double checked, as changed_after is used as constaint by _get_notes already
                note_changed = self.updater._get_note_timestamp(note.updated or note.created)
                if note_changed < changed_after:
                    logger.debug("ignore note '%s', last changed %s", note.title, changed_after)
                    continue

            # wrap note (NoteMetadata object) to provide get_resource_by_hash ...
            note_obj = ENNoteObj(note, self.sleep_on_ratelimit)
            if self.updater.update(note_obj):
                synced += 1  # count number of notes effectively synced

        self.updater.update_note_count()
        logger.info('Sync Complete\n')
        return synced

    def _get_notes(self, changed_after=None):
        """ Get notes from evernote notebook.
        """
        gn = GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit)
        if changed_after is not None:
            # limit number of notes to check using constraint on date updated
            # e.g. 'updated:20070704T150000Z'  # does not work as expected (in EN sandbox)
            keywords = 'created:' + changed_after.strftime("%Y%m%dT%H%M%S")  # e.g. 'created:20070704T20190801'
            # logger.debug("restrict notes using filter: %s", keywords)  # log bloat
        else:
            keywords = ''
        return gn.findNotes(keywords, EDAM_USER_NOTES_MAX, notebookGuid=self.notebook_guid).notes


def fix_last_update(dct):
    for k, v in dct.items():
        # if isinstance(v, str) and datetime_format_regex.match(v):
        if k == 'succeeded':
            v = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")       
            v = pytz.utc.localize(v)
            dct[k] = v
    return dct


def main():
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument('--notebook', '-n', action='store', help='Notebook name for synchronize. Default is default notebook unless all is selected')
        parser.add_argument('--all', '-a', action='store_true', help='Synchronize all notebooks', default=False)
        parser.add_argument('--all-linked', action='store_true', help='Get all linked notebooks')
        parser.add_argument('--date', action='store', help='only notes created or updated after this date', default=None)
        parser.add_argument('--incremental', action='store_true', help='only notes created or updated since last successful run')
        parser.add_argument('--no-sleep-on-ratelimit', action='store_true', help='dont sleep on being ratelimited')

        args = parser.parse_args()
        logger.info("run gnsyncm with args: %s", args)

        notebook_name = args.notebook
        sleepOnRateLimit = not args.no_sleep_on_ratelimit
        geeknote = GeekNote(sleepOnRateLimit=sleepOnRateLimit)
        logger.debug("using Evernote with consumerKey=%s", geeknote.consumerKey)

        last_update_fn = config.LAST_UPDATE_FN
        changed_after = None
        if args.date:
            changed_after = datetime.strptime(args.date, "%Y-%m-%d")
            changed_after = pytz.utc.localize(changed_after)
            assert not args.incremental, "cannot combine --date and --incremental"
        elif args.incremental:
            if not os.path.isfile(last_update_fn):
                logger.error("missing state of last gsyncm, created dummy; please update: %s", last_update_fn)
                now = datetime.now().replace(microsecond=0)
                last_update_info = {'succeeded': now}
                json.dump(last_update_info, open(last_update_fn, 'w'), default=str)  #json_util.default)
                sys.exit(1)

            last_update_info = json.load(open(last_update_fn, 'r'), object_hook=fix_last_update)
            changed_after = last_update_info['succeeded']
            args.all = True  # --incremental implies --all

        if args.all:
            logger.info("Synching all notebooks ...")
            notebook_count = 0
            notes_synced = 0
            for notebook in all_notebooks(sleep_on_ratelimit=sleepOnRateLimit):
                logger.debug("Syncing notebook %s (%s)", notebook.name, notebook.guid)
                GNS = GNSyncM(notebook.name, sleep_on_ratelimit=sleepOnRateLimit)
                assert GNS.all_set, "GNSyncM initialization incomplete"
                notes_synced += GNS.sync(changed_after)
                notebook_count += 1
            logger.info("synced total %s notebooks, %s notes", notebook_count, notes_synced)
        else:
            GNS = GNSyncM(notebook_name, sleep_on_ratelimit=sleepOnRateLimit)
            assert GNS.all_set, "troubles with GNSyncM initialization"
            notes_synced = GNS.sync(changed_after)
            logger.info("synced notebook %s, %s notes", notebook_name, notes_synced)

        if args.incremental:
            assert os.path.isfile(last_update_fn)
            now = datetime.now().replace(microsecond=0)
            last_update_info = {
                'succeeded': now
            }
            json.dump(last_update_info, open(last_update_fn, 'w'), default=str, indent=4)

    except (KeyboardInterrupt, SystemExit, tools.ExitException):
        # import traceback; traceback.print_exc()
        logger.warning("sync interrupted, incomplete")

    except Exception:
        logger.exception("gnsync failed")
        return


if __name__ == "__main__":
    main()
