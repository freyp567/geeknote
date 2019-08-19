#!/usr/bin/env python2 # noqa: E902
# -*- coding: utf-8 -*-
""" sync evernote notes to mongodb
"""

import os
import sys
import argparse
import binascii
import logging
import re
from datetime import datetime
import hashlib
import mimetypes
from slugify import slugify

import evernote.edam.type.ttypes as Types
from evernote.edam.limits.constants import EDAM_USER_NOTES_MAX
from bs4 import BeautifulSoup

from pymongo import MongoClient
import bson

import config
from geeknote import GeekNote
from storage import Storage
from editor import Editor
import tools

import urlparse
import evernote.edam.notestore.NoteStore as NoteStore

# set default logger (write log to file)
def_logpath = os.path.join(config.APP_DIR, 'gnsyncm.log')
formatter = logging.Formatter('%(asctime)-15s : %(message)s')
handler = logging.FileHandler(def_logpath)
handler.setFormatter(formatter)

logger = logging.getLogger("gnsyncm")
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
        self.mongo_client = MongoClient(
            config.DB_URI,
            tz_aware=False,
            wTimeoutMS=2500,
        )
        self.db = self.mongo_client[config.DB_NAME]

        logger.info('Sync Start')

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

        notebook = [item for item in notebooks if item.name == notebook_name]
        guid = None
        if notebook:
            guid = notebook[0].guid

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
    def sync(self):
        """
        Synchronize notes to mongodb
        """
        assert self.all_set, "cannot sync with partial initialization"
        notes = self._get_notes()

        for n in notes:
            db_note = self.db.notes.find_one({"Title": n.title})
            if db_note is not None:
                note_updated = self._get_note_timestamp(n.updated)
                force_update = True  #TODO for debugging 
                if db_note['UpdatedTime'] < note_updated or force_update:
                    self._update_db_note(db_note, n)
                    break

            else:  # new note
                self._create_db_note(n)

        logger.info('Sync Complete')

    def _get_notes(self):
        """ Get notes from evernote notebook.
        """
        keywords = ''
        gn = GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit)
        return gn.findNotes(keywords, EDAM_USER_NOTES_MAX, notebookGuid=self.notebook_guid).notes

    def _get_user_usn(self, user):
        """  return per-user value for UpdateSequenceNum """
        usn = user['Usn'] + 1
        self.db.users.update_one(
            {'_id': user['_id']},
            {"$set": {"Usn": usn}}
        )
        return usn

    def _update_note_count(self, notebook):
        """ update notes count for given notebook """
        note_count = self.db.notes.count_documents({'NotebookId': notebook['_id']})
        logger.info("update notebook note count to %s", note_count)
        self.db.notebook.update_one(
            {"_id": notebook['_id']},
            {"$set": {"NumberNotes": note_count}}
        )

    def _get_note_timestamp(self, timestamp):
        """ convert timestamp to mongodb timestamp """
        value = datetime.fromtimestamp(timestamp / 1000.0)  # timezone awareness??
        return value

    def _create_db_note(self, note):
        """
        Creates mongodb note from EN note
        """
        GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit).loadNoteContent(note)
        escaped_title = note.title  # need to escape??

        user = self.db.users.find_one({"Username": config.DB_USERNAME})
        assert user is not None, "failed to lookup db user %s" % config.DB_USERNAME

        notebook_db = self.db.notebooks.find_one({"Title": self.notebook_name})

        # in transaction?
        # with session.start_transaction():
        if notebook_db is None:
            usn = self._get_user_usn(user)
            self.db.notebooks.insert_one({
                "Title": self.notebook_name,
                "UrlTitle": slugify(self.notebook_name),
                "NumberNotes": 0,
                "Seq": -1,
                "UserId": user['_id'],
                "IsDeleted": False,
                "Usn": usn,
            })

        noteId = bson.objectid.ObjectId()

        # Save images
        if False:  #TODO to be implemented
            imageList = Editor.getImages(note.content)
            if imageList:
                if 'imagesInSubdir' in self.imageOptions and self.imageOptions['imagesInSubdir']:
                    os.mkdir(os.path.join(self.path, escaped_title + "_images"))
                    imagePath = os.path.join(self.path, escaped_title + "_images", escaped_title)
                    self.imageOptions['baseFilename'] = escaped_title + "_images/" + escaped_title
                else:
                    imagePath = os.path.join(self.path, escaped_title)
                    self.imageOptions['baseFilename'] = escaped_title
                for imageInfo in imageList:
                    filename = "{}-{}.{}".format(imagePath, imageInfo['hash'], imageInfo['extension'])
                    logger.info('Saving image to {}'.format(filename))
                    binaryHash = binascii.unhexlify(imageInfo['hash'])
                    GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit).saveMedia(note.guid, binaryHash, filename)

        is_markdown = True
        content = Editor.ENMLtoText(note.content, format='markdown', imageOptions={})
        if '<!DOCTYPE en-note' in content:
            is_markdown = False
        else:
            logger.debug("detected markdown: %s", content[:100])  #TODO verify  
        usn = self._get_user_usn(user)
        self.db.notes.insert_one({
            "_id": noteId,  # "NoteId"
            "Title": note.title,
            "Desc": "",  # note.Desc,
            "NotebookId": notebook_db['_id'],
            # "PublicTime":"2014-09-04T07:42:24.070Z",
            # "RecommendTime":"2014-09-04T07:42:24.070Z",
            "CreatedTime": self._get_note_timestamp(note.created),
            "UpdatedTime": self._get_note_timestamp(note.updated),
            "UpdatedUserId": user['_id'],
            "UrlTitle": slugify(note.title),
            "UserId": user['_id'],
            "Usn": usn,
            # "ImgSrc": imgSrc,
            "IsBlog": False,
            "IsMarkdown": is_markdown,
            "IsTrash": False,
            "IsDeleted": False,
            "ReadNum": 0,
        })
        self._update_note_count(notebook_db)
        self.db.note_contents.insert_one({
            "_id": noteId,  # "NoteId"
            "UserId": user['_id'],
            "IsBlog": False,
            "Content": content,
            "CreatedTime": self._get_note_timestamp(note.created),
            "UpdatedTime": self._get_note_timestamp(note.updated),
            "UpdatedUserId": user['_id'],  
        })
        return True

    def _update_db_note(self, db_note, note):
        """
        Updates mongodb note from EN note
        """
        GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit).loadNoteContent(note)
        content = Editor.ENMLtoText(note.content)
        assert 0  #TODO to be implemented
        open(file_note['path'], "w").write(content)
        updated_seconds = note.updated / 1000.0
        os.utime(file_note['path'], (updated_seconds, updated_seconds))

def main():
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument('--notebook', '-n', action='store', help='Notebook name for synchronize. Default is default notebook unless all is selected')
        parser.add_argument('--all', '-a', action='store_true', help='Synchronize all notebooks', default=False)
        parser.add_argument('--all-linked', action='store_true', help='Get all linked notebooks')
        parser.add_argument('--no-sleep-on-ratelimit', action='store_true', help='dont sleep on being ratelimited')

        args = parser.parse_args()

        notebook_name = args.notebook
        sleepOnRateLimit = not args.no_sleep_on_ratelimit
        geeknote = GeekNote(sleepOnRateLimit=sleepOnRateLimit)
        logger.debug("using Evernote with consumerKey=%s", geeknote.consumerKey)

        if args.all:
            for notebook in all_notebooks(sleep_on_ratelimit=args.sleep_on_ratelimit):
                logger.info("Syncing notebook %s (%s)", notebook.name, notebook.guid)
                GNS = GNSyncM(notebook.name, sleep_on_ratelimit=sleepOnRateLimit)
                assert GNS.all_set, "troubles with GNSyncM initialization"
                GNS.sync()
        else:
            GNS = GNSyncM(notebook_name, sleep_on_ratelimit=sleepOnRateLimit)
            assert GNS.all_set, "troubles with GNSyncM initialization"
            GNS.sync()

    except (KeyboardInterrupt, SystemExit, tools.ExitException):
        logger.warning("sync interrupted, incomplete")

    except Exception:
        logger.exception("gnsync failed")
        return

if __name__ == "__main__":
    main()
