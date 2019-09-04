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
from datetime import datetime
import pytz
from slugify import slugify
from bs4 import BeautifulSoup

import logging
logger = logging.getLogger("en2mongo.updatenote")

DATE_INVALID_BEFORE = datetime(1990, 01, 01)
DATE_EQUAL_DELTA = 2.0


class UpdateNote:

    def __init__(self, notebook_name):
        assert notebook_name, 'must have notebook name, cannot determine from .enex'
        self.notebook_name = notebook_name.lower()
        self.mongo_client = MongoClient(
            config.DB_URI,
            tz_aware=False,
            wTimeoutMS=2500,
        )
        self.db = self.mongo_client[config.DB_NAME]
        self.authenticate()
        self.imghandler = ImageHandler()

    def authenticate(self):
        """ authenticate using preconfigured user """
        self.user = self.db.users.find_one({"Username": config.DB_USERNAME})
        assert self.user is not None, "failed to lookup db user %s" % config.DB_USERNAME

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
        cond = {
            "$and": [
                # check date created to handle duplicate note titles properly
                {"CreatedTime": {"$eq": note_created}},
                {"Title": note.title},
            ]
        }
        db_note = self.db.notes.find_one(cond)
        if db_note is None:
            cond = {"Title": note.title}
            db_note = self.db.notes.find_one(cond)
            if db_note is not None:
                db_note_created = self._get_db_timestamp(db_note, 'CreatedTime')
                delta_created = self._compare_timestamps(note_created, db_note_created)
                if delta_created >= 0:
                    # happens, e.g. '2019-08-21T13:36:55+00:00' vs '2019-08-21T13:36:56+00:00' - assume rounding issue
                    logger.debug("found note with nearly equal CreatedTime: %s (%s)", note.title, delta_created)
                else:
                    logger.warning("found note with different CreatedTime: %s\ncreated in EN: %s\ncreated in db: %s", 
                                   note.title, note_created.isoformat(), db_note_created.isoformat())
                    db_note = None  # not same, so create new note
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

    def update(self, note):
        """ update note in mongodb from EN note if missing or updated """
        db_note = self._lookup_db_note(note)
        if db_note is not None and db_note["IsDeleted"]:  # TODO deleted in EN?
            db_note = self._purge_note(db_note)
        if db_note is not None:
            note_updated = self._get_note_timestamp(note.updated)
            if note_updated is None:
                note_updated = self._get_note_timestamp(note.created)
            force_update = False  # args.force_update
            db_note_updated = self._get_db_timestamp(db_note, 'UpdatedTime')
            if db_note_updated is None and note_updated is not None:
                # handle differences .enex vs EN api for date updated
                db_note_updated = self._get_db_timestamp(db_note, 'CreatedTime')
            delta_updated = self._compare_timestamps(note_updated, db_note_updated)

            if delta_updated < 0 or force_update:
                logger.warning("note updated: '%s'\nupdated in db: %s\nupdated in EN: %s",
                               note.title, 
                               db_note_updated and db_note_updated.isoformat() or '(unknown)',
                               note_updated and note_updated.isoformat() or '(unknown)',
                               )
                note.load_content()
                self._update_db_note(db_note, note)
            else:
                logger.debug("note unchanged: '%s'", note.title)

        else:  # new note
            note.load_content()
            db_note = self._create_db_note(note)

        self._update_tags(db_note, note)

    def _get_note_timestamp(self, timestamp):
        """ convert timestamp to mongodb timestamp """
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                # avoid TypeError: can't compare offset-naive and offset-aware datetimes
                timestamp = pytz.utc.localize(timestamp)
            if timestamp < pytz.utc.localize(DATE_INVALID_BEFORE):
                # date not known / set
                # have no dates < 1990 so assume all dates before are invalid
                return None
        else:
            timestamp = datetime.utcfromtimestamp(timestamp / 1000.0)
            if timestamp < DATE_INVALID_BEFORE:
                # 1.1.1970 (around) means date not known / set
                # have no dates < 1990 so assume all dates before are invalid
                return None
            timestamp = pytz.utc.localize(timestamp)  
        return timestamp

    def _purge_note(self, db_note):
        self.db.note_content_histories.delete_one({"_id": db_note["_id"]})
        self.db.note_contents.delete_one({"_id": db_note["_id"]})
        self.db.notes.delete_one({"_id": db_note["_id"]})
        #TODO delete note_images, too
        return None

    def _create_db_note(self, note):
        """
        Creates mongodb note from EN note
        """
        logger.info('create new note "%s" in %s', note.title, self.notebook_name)
        notebook_db = self.db.notebooks.find_one({"Title": self.notebook_name})

        # in transaction?
        # with session.start_transaction():
        if notebook_db is None:
            usn = self._get_user_usn(self.user)
            self.db.notebooks.insert_one({
                "Title": self.notebook_name,
                "UrlTitle": slugify(self.notebook_name),
                "NumberNotes": 0,
                "Seq": -1,
                "UserId": self.user['_id'],
                "IsDeleted": False,
                "Usn": usn,
            })
            notebook_db = self.db.notebooks.find_one({"Title": self.notebook_name})

        assert notebook_db is not None, "must have notebook to sync to"
        noteId = bson.objectid.ObjectId()
        content = note.content
        EN_NOTE_2 = '\<\!DOCTYPE\s+en-note\s+SYSTEM\s+"http://xml.evernote.com/pub/enml2.dtd"\s*\>'  # noqa: W605
        EN_NOTE_1 = '\<\!DOCTYPE\s+en-note\s+SYSTEM\s+"http://xml.evernote.com/pub/enml.dtd"\s*\>'  # noqa: W605
        if re.search(EN_NOTE_2, content, re.MULTILINE+re.DOTALL) is None:
            # note created 2019-10 using enml.dtd
            if re.search(EN_NOTE_1, content, re.MULTILINE+re.DOTALL) is None:
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
        self.db.notes.insert_one({
            "_id": noteId,  # "NoteId"
            "Title": note.title,
            "Desc": "",  # note.Desc,
            "NotebookId": notebook_db['_id'],
            # "PublicTime":"2014-09-04T07:42:24.070Z",
            # "RecommendTime":"2014-09-04T07:42:24.070Z",
            "CreatedTime": self._get_note_timestamp(note.created),
            "UpdatedTime": self._get_note_timestamp(note.updated),
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

        self._update_note_count(notebook_db)
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
        notebook_db = self.db.notebooks.find_one({"Title": self.notebook_name})
        if notebook_db:
            self._update_note_count(notebook_db)


    def _update_note_count(self, notebook):
        """ update notes count for given notebook """
        note_count = self.db.notes.count_documents({'NotebookId': notebook['_id']})
        logger.debug("update notebook note count to %s", note_count)
        self.db.notebook.update_one(
            {"_id": notebook['_id']},
            {"$set": {"NumberNotes": note_count}}
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
                assert img_tag.attrs.keys() == [u'src', ], "extra attribs in img tag"
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
            logger.warning("removed tag: %s from '%s'", tag_name, note.title)
            # handle tag removal?

        if added or removed:
            # update tag list of note
            logger.debug("update tags for note: %s", tag_names_new)
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
        EN_NOTE_2 = '\<\!DOCTYPE\s+en-note\s+SYSTEM\s+"http://xml.evernote.com/pub/enml2.dtd"\>'
        assert re.search(EN_NOTE_2, content) is not None, "content format unsupported: %s" % content[:180]
        noteId = db_note["_id"]
        db_note_created = self._get_db_timestamp(db_note, 'CreatedTime')
        note_created = self._get_note_timestamp(note.created)
        delta = self._compare_timestamps(note_created, db_note_created)
        if delta < 0:
            # must be invariant, otherwise followup later update will fail to lookup note as titles are not really unique
            logger.error('failed to update note "%s", date created mismatch\nin db: %s\nin EN: %s', 
                         note.title,
                         db_note_created and db_note_created.isoformat() or '(not set)',
                         note_created and note_created.isoformat() or '(not set)',
                         )
            return

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
        return

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
                logger.info('new image {}'.format(img_path))
            else:
                img_path = file_obj['Path']
                img_dir = img_path[:img_path.rfind('/') + 1]
                img_name = img_path[len(img_dir):]
                logger.info('existing image {}'.format(img_path))

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
                    # "AlbumId": "52d3e8ac99c37b7f0d000001",  # TODO verify handling
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
            resource = note.get_resource_by_hash(binaryHash)
            if resource is None:
                logger.warning("failed to match image by hash (%s)", imageInfo['hash'])
            else:
                handle_image(resource)

        return img_map
