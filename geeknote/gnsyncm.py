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
import uuid
from slugify import slugify
from ftplib import FTP
import io

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
            if db_note is not None and db_note["IsDeleted"]:
                db_note = self._purge_note(db_note)
            if db_note is not None:
                note_updated = self._get_note_timestamp(n.updated)
                force_update = True  #TODO for debugging 
                if db_note['UpdatedTime'] < note_updated or force_update:
                    self._update_db_note(db_note, n)
                else:
                    logger.debug("note unchanged: '%s'", n.title)

            else:  # new note
                db_note = self._create_db_note(n)

            self._update_tags(db_note, n)

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
        imageList = Editor.getImages(note.content)
        img_map = self._handle_images(noteId, note, imageList, user)

        is_markdown = True
        #content = Editor.ENMLtoText(note.content, format='markdown', imageOptions={})
        content = note.content
        EN_NOTE_2 = '<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">'
        if EN_NOTE_2 in content: # have evernote HTML
            is_markdown = False
        else:
            # what else?
            logger.debug("detected markdown: %s", content[:100])  #TODO verify  
            assert False  # TODO to be handled, when needed

        if img_map:
            # update img src= in note content to match target location
            logger.debug("update image refs in note %s", noteId)  
            assert not is_markdown
            content = self._fixup_img_refs(content, img_map)

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
        db_note = self.db.notes.find_one({'_id': noteId})
        assert db_note

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
        return db_note

    def _fixup_img_refs(self, content, img_map):
        """
        transform EN image refs:
            <img src="file:/C:/Users/pifre/AppData/Local/Temp/enhtmlclip/Image.jpg"/>
            <en-media hash="9aad6b0d39f6e0856afde5d941a5c6a2" type="image/jpeg"></en-media>
        """
        soup = BeautifulSoup(content.decode('utf-8'), 'html.parser')
        for img_tag in soup.select('img'):
            assert len(img_tag.contents) == 0  # expect img elements to have no content
            # handle img tag followed by en-media tag from EN
            next_elmt = img_tag.next_sibling
            if next_elmt and next_elmt.name == 'en-media':
                # drop img tag preceeding en-media elmt
                assert img_tag.attrs.keys() == [u'src', ], "extra attribs in img tag"
                img_tag.extract()
            else:
                logger.warning("missing en-media after img tag?")

        img_tags = soup.findAll('img')
        assert len(img_tags) == 0

        for en_media in soup.findAll('en-media'):
            if 'type' in en_media.attrs and 'hash' in en_media.attrs:
                imageType, imageExtension = en_media['type'].split('/')
                if imageType == "image":
                    newTag = soup.new_tag("img")
                    # new_path = img_map[en_media['hash']]['Path']
                    image_id = img_map[en_media['hash']]['ImageId']
                    new_path = '/api/file/getImage?fileId=%s' % image_id
                    newTag['src'] = new_path

                    en_media.replace_with(newTag)
                    # TODO keep en-media elmt for (future) usecase to restore EN note
                else:
                    logger.info("ignore en-media elmt for type=%s", en_media['type'])
            else:
                logger.warning("detected en-media elmt without type/hash attribs")  # unexpected

        return str(soup)

    def _prepare_upload_target(self, ftp, img_dir):
        if not img_dir.startswith('files/'):
            img_dir = 'files/' + img_dir
        if not img_dir.endswith('/'):
            img_dir += '/'

        create_steps = []
        dir_path = img_dir
        while not ftp.nlst(dir_path):
            create_steps.append(dir_path)
            dir_path = dir_path[:-1]  # strip trailing slash
            assert '/' in dir_path
            dir_path = dir_path[:dir_path.rfind('/')+1]

        while create_steps:
            dir_path = create_steps.pop()
            try:
                ftp.mkd(dir_path)
            except Exception as err:
                # error_perm('550 Create directory operation failed.',)
                logger.error("ftp.mkd failed for %s %s", dir_path, err)
                raise RuntimeError("failed create directory %s to upload image " % dir_path)

        return img_dir

    def _upload_image(self, img_dir, img_name, img_data):
        # TODO refactor, create file storage service to avoid repeated login
        logger.info("prepare image upload to %s", img_dir)
        ftp = FTP(config.FTP_HOST)
        ftp.login(config.FTP_USER, config.FTP_PWD)
        img_dir = self._prepare_upload_target(ftp, img_dir)
        logger.info("upload image '%s' to '%s'", img_name, img_dir)
        fp = io.BytesIO(img_data)
        img_path = "%s/%s" % (img_dir, img_name)
        ftp.storbinary("STOR %s" % img_path, fp)
        return img_path

    def _handle_images(self, noteId, note, imageList, user):
        img_map = {}
        if not imageList:
            return img_map

        def handle_image(resource):
            img_title = '{}.{}'.format(imageInfo['hash'], imageInfo['extension'])
            file_obj = self.db.files.find_one({'Title': img_title, 'UserId': user['_id']})
            if not file_obj:
                # new image
                new_guid = uuid.uuid4().hex
                img_dir = tools.get_random_filepath(str(user['_id']), new_guid)
                img_name = '{}.{}'.format(new_guid, imageInfo['extension'])
                img_path = '{}/{}'.format(img_dir, img_name)
                logger.info('handle new image {}'.format(img_path))
            else:
                img_path = file_obj['Path']
                img_dir = img_path[:img_path.rfind('/') + 1]
                img_name = img_path[len(img_dir):]
                logger.info('handle image update {}'.format(img_path))

            # resource.data.body is bytestream of image
            img_path = self._upload_image(img_dir, img_name, resource.data.body)

            # add or update files and note_images entries
            if not file_obj:
                img_id = bson.objectid.ObjectId()
                self.db.files.insert_one({
                    "_id": img_id,
                    "UserId": user['_id'],
                    "Name": img_name,
                    "Title": img_title,
                    "Size": len(resource.data.body),
                    "Type": "",
                    "Path": img_path,
                    #"AlbumId": "52d3e8ac99c37b7f0d000001",  # TODO verify handling
                    # "IsDefaultAlbum": True,
                    "CreatedTime": self._get_note_timestamp(note.created),
                })
            else:
                img_id = file_obj['_id']

            # collect info to update image ref in note content
            img_map[imageInfo['hash']] = {'Path': img_path, 'ImageId': str(img_id)}

            note_image = self.db.note_images.find_one({
                'NoteId': noteId,
                "ImageId": img_id,
            })
            if not note_image:
                self.db.note_images.insert_one({
                    "_id": bson.objectid.ObjectId(),
                    "NoteId": noteId,
                    "ImageId": img_id
                })

        for imageInfo in imageList:
            binaryHash = binascii.unhexlify(imageInfo['hash'])
            GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit).handleMedia(note.guid, binaryHash, handle_image)
        
        return img_map

    def _purge_note(self, db_note):
        self.db.note_content_histories.delete_one({"_id": db_note["_id"]})
        self.db.note_contents.delete_one({"_id": db_note["_id"]})
        self.db.notes.delete_one({"_id": db_note["_id"]})
        #TODO delete note_images, too
        return None

    def _update_tags(self, db_note, note):
        """ update tags """
        user_id = db_note['UserId']
        user = self.db.users.find_one({"_id": user_id})
        assert user is not None, 'must have user to update tags'
        tags = self.db.tags.find_one({'_id': user_id})
        user_tags = set()
        if tags is not None:
            user_tags = set(tags['Tags'])
        user_tags.add("")

        tag_names_new = set(note.tagNames)
        tag_names_new.add("")
        tag_names_db = set(db_note.get('Tags', []))
        tag_names_db.add("")
        added = tag_names_new.difference(tag_names_db)
        for tag_name in added:
            user_tags.add(tag_name.lower())

        db_tags = self.db.tags.find_one({'_id': user_id})
        if db_tags is None:
            self.db.tags.insert_one({
                "_id": user_id,
                "Tags": list(user_tags)
            })
        if added:
            # update tag list for user; note: adding tags only
            self.db.tags.update_one({'_id': user_id}, {'$set': {'Tags': list(user_tags)}})

        removed = tag_names_db.difference(tag_names_new)
        for tag_name in removed:
            logger.warning("removed tag: %s from '%s'", tag_name, note.title)
            # handle tag removal?

        if added or removed:
            # update tag list of note
            self.db.notes.update_one(
                {'_id': db_note['_id']}, 
                {"$set": {"Tags": list(tag_names_new)}}
            )

        # update note_tags
        for tag_name in added:
            if not tag_name:
                continue
            usn = self._get_user_usn(user)
            note_tags = self.db.note_tags.find_one({"UserId": user_id, "Tag": tag_name})
            if note_tags is None:
                tag_id = bson.objectid.ObjectId()
                self.db.note_tags.insert_one({
                    "_id": tag_id,
                    "UserId": user_id,
                    "Tag": tag_name,
                    "Usn": usn,
                    "Count": 1,
                    "CreatedTime": self._get_note_timestamp(note.created),
                    "UpdatedTime": self._get_note_timestamp(note.updated),
                    "IsDeleted": False
                })
            else:
                self.db.note_tags.update_one(
                    {"_id": note_tags["_id"]},
                    {"$set": {
                        "Usn": usn,
                        "Count": note_tags["Count"] + 1,
                        "UpdatedTime": self._get_note_timestamp(note.updated),
                    }}
                )
        for tag_name in removed:
            if not tag_name:
                continue
            usn = self._get_user_usn(user)
            note_tags = self.db.note_tags.find_one({"UserId": user_id, "Tag": tag_name})
            if note_tags is not None:
                self.db.note_tags.update_one(
                    {"_id": note_tags["_id"]},
                    {"$set": {
                        "Usn": usn,
                        "Count": note_tags["Count"] - 1,
                        "UpdatedTime": self._get_note_timestamp(note.updated),
                    }}
                )

    def _update_db_note(self, db_note, note):
        """
        Updates mongodb note from EN note
        """
        gn = GeekNote(sleepOnRateLimit=self.sleep_on_ratelimit)
        gn.loadNoteContent(note)
        # content = Editor.ENMLtoText(note.content)
        # content = note.content
        #TODO implement note update
        # similar to create: 
        # update images / add new ones  (evtl drop removed ones)
        # fix image refs (img / en-media)
        # compare new and old content, if changed:
        # create entry in note_content_histories with old note content
        # update note content
        logger.warning("ignore note update: '%s'", note.title)


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
