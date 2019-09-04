#!/usr/bin/env python2 # noqa: E902
# -*- coding: utf-8 -*-
""" sync evernote notes to mongodb
"""

import os
import sys
import argparse
import logging
import re
from datetime import datetime
import pytz

from evernote.edam.limits.constants import EDAM_USER_NOTES_MAX

import config
from geeknote import GeekNote
from storage import Storage
import tools
from updatenote import UpdateNote

# set default logger (write log to file)
def_logpath = os.path.join(config.APP_DIR, 'gnsyncm.log')
formatter = logging.Formatter('%(asctime)-15s : %(message)s')
handler = logging.FileHandler(def_logpath)
handler.setFormatter(formatter)

logger = logging.getLogger("en2mongo")
logger.setLevel(os.environ.get('LOGLEVEL') or logging.DEBUG)
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler(sys.stderr))

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


def reset_logpath(logpath):
    """
    Reset logpath to path from command line
    """
    global logger

    if not logpath:
        return

    # remove temporary log file if it's empty
    if os.path.isfile(def_logpath):
        if os.path.getsize(def_logpath) == 0:
            os.remove(def_logpath)

    # save previous handlers
    handlers = logger.handlers

    # remove old handlers
    for handler in handlers:
        logger.removeHandler(handler)

    # try to set new file handler
    handler = logging.FileHandler(logpath)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


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

    def get_resource_by_hash(self, hash):
        guid = self._note.guid
        resource = self.gn.handleMedia(guid, hash, lambda r: r)
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

        # TODO check mongodb connectivity - fail early
        self.updater = UpdateNote(notebook_name)

        logger.info('Sync notebook=%s ...', notebook_name)

        # set notebook
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
        logger.info("found %s notes to be synced", len(notes))
        for note in notes:
            if changed_after is not None:  
                # to be handled by findNote, but for time beeing isnt
                note_changed = self.updater._get_note_timestamp(note.updated or note.created)
                if note_changed < changed_after:
                    logger.debug("ignore note '%s', last changed %s", note.title, changed_after)
                    continue

            # wrap note (NoteMetadata object) to provide get_resource_by_hash ...
            note_obj = ENNoteObj(note, self.sleep_on_ratelimit)
            self.updater.update(note_obj)

        self.updater.update_note_count()
        logger.info('Sync Complete')

    def _get_notes(self, changed_after=None):
        """ Get notes from evernote notebook.
        """
        keywords = ''
        gn = GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit)
        if changed_after is not None:
            # limit number of notes to check
            # keywords = 'created:' +changed_after.strftime("%Y%m%dT%H%M%SZ")
            # e.g. 'created:20070704T150000Z'  # does not work as expected (in EN sandbox)
            keywords = 'created:' + changed_after.strftime("%Y%m%d")  # e.g. 'created:20070704T20190801'
            logger.info("restrict notes using filter: %s", keywords)
        return gn.findNotes(keywords, EDAM_USER_NOTES_MAX, notebookGuid=self.notebook_guid).notes


def main():
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument('--notebook', '-n', action='store', help='Notebook name for synchronize. Default is default notebook unless all is selected')
        parser.add_argument('--all', '-a', action='store_true', help='Synchronize all notebooks', default=False)
        parser.add_argument('--all-linked', action='store_true', help='Get all linked notebooks')
        parser.add_argument('--date', action='store', help='only notes created or changed after this date', default=None)
        parser.add_argument('--no-sleep-on-ratelimit', action='store_true', help='dont sleep on being ratelimited')

        args = parser.parse_args()
        logger.info("run gnsyncm with args: %s", args)

        notebook_name = args.notebook
        sleepOnRateLimit = not args.no_sleep_on_ratelimit
        geeknote = GeekNote(sleepOnRateLimit=sleepOnRateLimit)
        logger.debug("using Evernote with consumerKey=%s", geeknote.consumerKey)

        changed_after = None
        if args.date:
            changed_after = datetime.strptime(args.date, "%Y-%m-%d")
            changed_after = pytz.utc.localize(changed_after)

        if args.all:
            for notebook in all_notebooks(sleep_on_ratelimit=sleepOnRateLimit):
                logger.info("Syncing notebook %s (%s)", notebook.name, notebook.guid)
                GNS = GNSyncM(notebook.name, sleep_on_ratelimit=sleepOnRateLimit)
                assert GNS.all_set, "GNSyncM initialization incomplete"
                GNS.sync(changed_after)
        else:
            GNS = GNSyncM(notebook_name, sleep_on_ratelimit=sleepOnRateLimit)
            assert GNS.all_set, "troubles with GNSyncM initialization"
            GNS.sync(changed_after)

    except (KeyboardInterrupt, SystemExit, tools.ExitException):
        #import traceback; traceback.print_exc()
        logger.warning("sync interrupted, incomplete")

    except Exception:
        logger.exception("gnsync failed")
        return

if __name__ == "__main__":
    main()
