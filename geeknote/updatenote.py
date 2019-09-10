# -*- coding: utf-8 -*-
"""
update note in mongodb
"""

import config
import tools
from imagehandler import ImageHandler

import re
from pymongo import MongoClient
import binascii
import bson
import uuid
from datetime import datetime, timedelta
import pytz
import dateutil.tz
from slugify import slugify
from bs4 import BeautifulSoup

import logging
logger = logging.getLogger("en2mongo.updatenote")

DATE_INVALID_BEFORE = datetime(1990, 01, 01)
DATE_UNKNOWN_YEAR = 1970
DATE_EQUAL_DELTA = 2.0


def log_title(value):
    if not isinstance(value, unicode):
        value = unicode(value, 'utf-8', 'replace')
    else:
        value = value
        # value = value.encode('latin-1', 'charrefreplace')  # fails
    if len(value) > 40:
        return u'"%s"..' % value[:40]
    else:
        return u'"%s"' % value


def log_date(value):
    if not value:
        return '(not set)'
    if not isinstance(value, datetime):
        return repr(value)
    # assume timezone aware, convert to local timezone
    value2 = value.astimezone(dateutil.tz.tzlocal())
    return value2.strftime("%Y-%m-%dT%H:%M")  # .isoformat() without timezone


class UpdateNote:

    def __init__(self, notebook_name, force_update=False):
        assert notebook_name, 'must have notebook name, cannot determine from .enex'
        self.notebook_name = notebook_name.lower()
        self.force_update = force_update
        self.mongo_client = MongoClient(
            config.DB_URI,
            tz_aware=False,
            wTimeoutMS=2500,
        )
        self.db = self.mongo_client[config.DB_NAME]
        self.authenticate()
        self.imghandler = ImageHandler()
        self._select_notebook(self.notebook_name)

    def authenticate(self):
        """ authenticate using preconfigured user """
        self.user = self.db.users.find_one({"Username": config.DB_USERNAME})
        assert self.user is not None, "failed to lookup db user %s" % config.DB_USERNAME

    def _select_notebook(self, notebook_name):
        notebook_db = self.db.notebooks.find_one({"Title": self.notebook_name})
        if notebook_db is None:
            # create new notebook for given name
            usn = self._get_user_usn(self.user)
            self.db.notebooks.insert_one({
                "Title": notebook_name,
                "UrlTitle": slugify(notebook_name),
                "NumberNotes": 0,
                "Seq": -1,
                "UserId": self.user['_id'],
                "IsDeleted": False,
                "Usn": usn,
            })
            notebook_db = self.db.notebooks.find_one({"Title": self.notebook_name})
            assert notebook_db is not None
        else:
            pass
        self._db_notebook = notebook_db

    def _get_db_timestamp(self, db_note, field_name):
        date_value = db_note[field_name]
        if date_value is None:
            return None
        if date_value.tzinfo is None:
            date_value = pytz.utc.localize(date_value)
        invalid_before = pytz.utc.localize(DATE_INVALID_BEFORE)
        if date_value < invalid_before:
            # caused by difference for date updated in .enex vs EN API
            return None
        return date_value

    def _lookup_db_note(self, note):
        """ lookup equivalent note in mongodb """
        note_created = self._get_note_timestamp(note.created)
        rounded = timedelta(seconds=2)
        cond = {
            "$and": [
                {"Title": note.title},
                {"IsTrash": False},  # ignore notes in trash
                {"NotebookId": self._db_notebook['_id']},
                # check date created to handle duplicated note titles properly
                # dont use $eq to avoid rounding errors (+-1s)
                {"CreatedTime": {"$gte": note_created - rounded, "$lte": note_created + rounded}},
            ]
        }
        candidates = list(self.db.notes.find(cond))
        if not candidates:
            return None

        if len(candidates) > 1:
            # how to pick appropriate note by title if not unique?  # to be verified (rare cases)
            # raise ValueError("failed to lookup note")
            # e.g. " Cathedral of St John The Baptist, Savannah Georgia"
            logger.warning(u'failed to determine existing note for %s created=%s',
                           log_title(note.title), note_created and note_created.isoformat() or '(not set)')
            for db_note in candidates:
                logger.info(u"candidate: %s %s", log_date(self._get_db_timestamp(db_note), 'CreatedTime'), log_title(db_note['Title']))
            # to be fixed, but at time beeing dont let fail but pick best guess
            db_note = candidates[0]

        else:
            db_note = candidates[0]
        return db_note

    def _compare_timestamps(self, first, second):
        """ return 0 if equal, > 0 (= seconds difference) if nearly equal, or -1 if timestamps different """
        if second is None:
            if first is not None:
                return -1
            else:
                return 0  # both not set?
        elif first is None:
            return -1  # second not None
        time_delta = abs((first - second).total_seconds())
        if time_delta == 0.0:
            return 0.0
        if time_delta == 1.0:
            # happens, e.g. '2019-08-21T13:36:55+00:00' vs '2019-08-21T13:36:56+00:00' - assume rounding issue
            return time_delta
        if time_delta < DATE_EQUAL_DELTA:  # more than 1 second possible?
            logger.warning("detected date equality gab of %s", time_delta)
            return time_delta
        else:
            return -1

    def _get_note_updated_or_created(self, note):
        note_updated = self._get_note_timestamp(note.updated)
        if note_updated is None:
            note_updated = self._get_note_timestamp(note.created)
        return note_updated

    def update(self, note):
        """ update note in mongodb from EN note if missing or updated """
        updated = False
        db_note = self._lookup_db_note(note)
        if db_note is not None and db_note["IsDeleted"]:  
            # TODO deleted in EN?
            db_note = self._purge_note(db_note)

        if db_note is not None:
            note_updated = self._get_note_updated_or_created(note)
            db_note_updated = self._get_db_timestamp(db_note, 'UpdatedTime')
            if db_note_updated is None and note_updated is not None:
                # handle differences .enex vs EN api for date updated
                db_note_updated = self._get_db_timestamp(db_note, 'CreatedTime')
            delta_updated = self._compare_timestamps(note_updated, db_note_updated)

            if delta_updated < 0 or self.force_update:
                logger.debug(u'note changed: "%s"\nupdated in db: %s\nupdated in EN: %s',
                             log_title(note.title), log_date(db_note_updated), log_date(note_updated),
                            )
                note.load_content()
                updated = self._update_db_note(db_note, note)
            else:
                # logger.debug(u"note unchanged: %s", log_title(note.title)) # blather
                updated = False

        else:  # new note
            note.load_content()
            note_created = self._get_note_timestamp(note.created)
            logger.debug(u'new note: %s created=%s', log_title(note.title), log_date(note_created))
            db_note = self._create_db_note(note)
            updated = True

        self._update_tags(db_note, note)
        return updated

    def _get_note_timestamp(self, timestamp):
        """ convert timestamp to mongodb timestamp """
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                # avoid TypeError: can't compare offset-naive and offset-aware datetimes
                timestamp = pytz.utc.localize(timestamp)
        else:
            timestamp = datetime.utcfromtimestamp(timestamp / 1000.0)
            timestamp = pytz.utc.localize(timestamp)
        if timestamp.year <= DATE_UNKNOWN_YEAR:
            # date not known / set
            return None
        return timestamp

    def _purge_note(self, db_note):
        self.db.note_content_histories.delete_one({"_id": db_note["_id"]})
        self.db.note_contents.delete_one({"_id": db_note["_id"]})
        self.db.notes.delete_one({"_id": db_note["_id"]})
        # TODO delete note_images, too
        return None

    def _create_db_note(self, note):
        """
        Creates mongodb note from EN note
        """
        logger.info(u'create new note %s (%s) created=%s', 
                    log_title(note.title), 
                    self.notebook_name, 
                    log_date(self._get_note_timestamp(note.created)))

        assert self._db_notebook is not None, "must have notebook to sync to"
        noteId = bson.objectid.ObjectId()
        content = note.content
        EN_NOTE = '\<\!DOCTYPE\s+en-note\s+SYSTEM\s+"http://xml.evernote.com/pub/%s"\s*\>'  # noqa: W605
        EN_NOTE_1 = EN_NOTE % 'enml.dtd'
        EN_NOTE_2 = EN_NOTE % 'enml2.dtd'
        if re.search(EN_NOTE_2, content, re.MULTILINE + re.DOTALL) is None:
            # note created 2019-10 using enml.dtd
            if re.search(EN_NOTE_1, content, re.MULTILINE + re.DOTALL) is None:
                raise ValueError("content format unsupported: %s" % content[:180])

        # Save images
        imageList = self.get_images(content)
        img_map = self._handle_images(noteId, note, imageList)

        if img_map:
            # update img src= in note content to match target location
            logger.debug("update image refs in note %s", noteId)
            content = self._fixup_img_refs(content, img_map)

        is_markdown = False
        usn = self._get_user_usn(self.user)
        updated_time = self._get_note_timestamp(note.updated)
        if not updated_time:
            updated_time = None  # use .created?
        self.db.notes.insert_one({
            "_id": noteId,  # "NoteId"
            "Title": note.title,
            "Desc": "",  # note.Desc,
            "NotebookId": self._db_notebook['_id'],
            # "PublicTime":"2014-09-04T07:42:24.070Z",
            # "RecommendTime":"2014-09-04T07:42:24.070Z",
            "CreatedTime": self._get_note_timestamp(note.created),
            "UpdatedTime": updated_time,
            "UpdatedUserId": self.user['_id'],
            "SyncedTime": datetime.utcnow().replace(tzinfo=pytz.utc),
            "UrlTitle": slugify(note.title),
            "UserId": self.user['_id'],
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

        self.update_note_count()
        self.db.note_contents.insert_one({
            "_id": noteId,  # "NoteId"
            "UserId": self.user['_id'],
            "IsBlog": False,
            "Content": content,
            "CreatedTime": self._get_note_timestamp(note.created),
            "UpdatedTime": self._get_note_timestamp(note.updated),
            "UpdatedUserId": self.user['_id'],
        })

        return db_note

    def update_note_count(self):
        """ update notes count for current notebook """
        note_count = self.db.notes.count_documents({'NotebookId': self._db_notebook['_id']})
        logger.debug("update notebook note count for %s to %s", self.notebook_name, note_count)
        self._db_notebook["Seq"] += 1
        seq_no = self._db_notebook["Seq"]
        self.db.notebooks.update_one(
            {"_id": self._db_notebook['_id']},
            {"$set": {"NumberNotes": note_count, "Seq": seq_no}}
        )

    def get_images(self, content):
        '''
        Creates a list of image resources to save.
        Each has a hash and extension attribute.
        '''
        soup = BeautifulSoup(content, features="lxml")
        imageList = []
        for section in soup.findAll('en-media'):
            if 'type' in section.attrs and 'hash' in section.attrs:
                imageType, imageExtension = section['type'].split('/')
                if imageType == "image":
                    imageList.append({'hash': section['hash'], 'extension': imageExtension})
        return imageList

    def _get_user_usn(self, user):
        """  return per-user value for UpdateSequenceNum """
        usn = user['Usn'] + 1
        self.db.users.update_one(
            {'_id': user['_id']},
            {"$set": {"Usn": usn}}
        )
        return usn

    def _fixup_img_refs(self, content, img_map):
        """
        transform EN image refs:
            <img src="file:/C:/Users/pifre/AppData/Local/Temp/enhtmlclip/Image.jpg"/>
            <en-media hash="9aad6b0d39f6e0856afde5d941a5c6a2" type="image/jpeg"></en-media>
        """
        soup = BeautifulSoup(content, 'html.parser')
        img_tag_count = 0
        for img_tag in soup.select('img'):
            assert len(img_tag.contents) == 0  # expect img elements to have no content
            # handle img tag followed by en-media tag from EN
            next_elmt = img_tag.next_sibling
            if next_elmt and next_elmt.name == 'en-media':
                # drop img tag preceeding en-media elmt
                if img_tag.attrs.keys() != [u'src', ]:
                    logger.debug("extra attribs in img tag: %s", img_tag.attrs.keys())  # width, height
                img_tag.extract()
            else:
                # logger.warning("missing en-media after img tag?")
                img_tag_count += 1

        img_tags = soup.findAll('img')
        assert len(img_tags) == img_tag_count

        for en_media in soup.findAll('en-media'):
            if 'type' in en_media.attrs and 'hash' in en_media.attrs:
                imageType, imageExtension = en_media['type'].split('/')
                if imageType == "image":
                    newTag = soup.new_tag("img")
                    # new_path = img_map[en_media['hash']]['Path']
                    if en_media['hash'] not in img_map:
                        logger.warning("failed to fetch image from img_map")
                        continue
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

    def _update_tags(self, db_note, note):
        """ update tags """
        note.load_tags()
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
        if self.notebook_name not in tag_names_new:
            # automatically add notebook_name as tag name - for easier searching
            tag_names_new.add(self.notebook_name)
        tag_names_db = set(db_note.get('Tags', []))
        tag_names_db.add("")
        added = tag_names_new.difference(tag_names_db)
        for tag_name in added:
            user_tags.add(tag_name.lower())

        db_tags = self.db.tags.find_one({'_id': user_id})
        if db_tags is None:
            logger.debug("set tags for user: %s", user_tags)
            self.db.tags.insert_one({
                "_id": user_id,
                "Tags": list(user_tags)
            })
        if added:
            # update tag list for user; note: adding tags only
            # logger.debug("add new tags for user: %s", user_tags) #blather
            self.db.tags.update_one({'_id': user_id}, {'$set': {'Tags': list(user_tags)}})

        removed = tag_names_db.difference(tag_names_new)
        for tag_name in removed:
            logger.warning(u"removed tag %s from %s", tag_name, log_title(note.title))
            # handle tag removal?

        if added or removed:
            # update tag list of note
            logger.debug(u'update tags for note: %s (%s)', tag_names_new, log_title(note.title))
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
        content = note.content
        EN_NOTE_2 = '\<\!DOCTYPE\s+en-note\s+SYSTEM\s+"http://xml.evernote.com/pub/enml2.dtd"\>'  # noqa: W605
        assert re.search(EN_NOTE_2, content) is not None, "content format unsupported: %s" % content[:180]
        noteId = db_note["_id"]
        db_note_created = self._get_db_timestamp(db_note, 'CreatedTime')
        note_created = self._get_note_timestamp(note.created)
        delta = self._compare_timestamps(note_created, db_note_created)
        if delta < 0:
            # must be invariant, otherwise followup later update will fail to lookup note as titles are not really unique
            logger.error(u'failed to update note %s, date created mismatch\nin db: %s\nin EN: %s',
                         log_title(note.title), log_date(db_note_created), log_date(note_created))
            return False

        logger.info(u'update note %s created=%s updated=%s',
                    log_title(note.title), 
                    log_date(self._get_note_timestamp(note.created)), 
                    log_date(self._get_note_timestamp(note.updated)))

        # Save images
        imageList = self.get_images(content)
        img_map = self._handle_images(noteId, note, imageList)

        if img_map:
            # update img src= in note content to match target location
            logger.debug("update image refs in note %s", noteId)
            content = self._fixup_img_refs(content, img_map)

        # TODO purge removed images

        usn = self._get_user_usn(self.user)
        self.db.notes.update_one(
            {
                "_id": noteId
            },
            {
                "$set": {
                    "Title": note.title,
                    "Desc": "",  # note.Desc,
                    # "NotebookId": notebook_db['_id'],
                    # "PublicTime":...
                    # "RecommendTime":...
                    "UpdatedTime": self._get_note_timestamp(note.updated),
                    "SyncedTime": datetime.utcnow().replace(tzinfo=pytz.utc),
                    "UpdatedUserId": self.user['_id'],
                    "UrlTitle": slugify(note.title),
                    "UserId": self.user['_id'],
                    "Usn": usn,
                    # "ImgSrc": imgSrc,
                    # "IsBlog": False,
                    # "IsMarkdown": is_markdown,
                    # "IsTrash": False,
                    # "IsDeleted": False,
                    # "ReadNum": db_notes["ReadNum"],  # preserve
                }
            }
        )

        # create entry in note_content_histories with old note content
        # TODO future

        # update note content
        self.db.note_contents.update_one(
            {
                "_id": noteId,
            },
            {
                "$set": {
                    "UserId": self.user['_id'],
                    # "IsBlog": False,
                    "Content": content,
                    "CreatedTime": self._get_note_timestamp(note.created),
                    "UpdatedTime": self._get_note_timestamp(note.updated),
                    "UpdatedUserId": self.user['_id'],
                }
            }
        )

        # note: note tags to be updated by caller
        return True

    def _handle_images(self, noteId, note, imageList):
        img_map = {}
        if not imageList:
            return img_map

        def handle_image(resource):
            img_title = '{}.{}'.format(imageInfo['hash'], imageInfo['extension'])
            file_obj = self.db.files.find_one({'Title': img_title, 'UserId': self.user['_id']})
            if not file_obj:
                # new image
                new_guid = uuid.uuid4().hex
                img_dir = tools.get_random_filepath(str(self.user['_id']), new_guid)
                img_name = '{}.{}'.format(new_guid, imageInfo['extension'])
                img_path = '{}/{}'.format(img_dir, img_name)
                # logger.info('new image {}'.format(img_path))  # log bloat
            else:
                img_path = file_obj['Path']
                img_dir = img_path[:img_path.rfind('/') + 1]
                img_name = img_path[len(img_dir):]
                logger.debug('existing image {}'.format(img_path))

            # resource.data.body is bytestream of image
            img_path = self.imghandler.upload_image(img_dir, img_name, resource.data.body)

            # add or update files and note_images entries
            if not file_obj:
                img_id = bson.objectid.ObjectId()
                self.db.files.insert_one({
                    "_id": img_id,
                    "UserId": self.user['_id'],
                    "Name": img_name,
                    "Title": img_title,
                    "Size": len(resource.data.body),
                    "Type": "",
                    "Path": img_path,
                    # "AlbumId": "52d3e8ac99c37b7f0d000001",  # what for?
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
            resource = note.get_image_resource(imageInfo)
            if resource is None:
                logger.warning(u'failed to lookup image for %s: %s', log_title(note.title), imageInfo)
            else:
                handle_image(resource)

        return img_map
