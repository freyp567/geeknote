"""
update note in mongodb
"""

import config
import tools
from imagehandler import ImageHandler

import re
from pymongo import MongoClient
import bson
import uuid
from datetime import datetime
import pytz
from slugify import slugify
from bs4 import BeautifulSoup

import logging
logger = logging.getLogger("en2mongo.updatenote")


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
        if date_value.tzinfo is None:
            date_value = pytz.utc.localize(date_value)
        return date_value

    def update(self, note):
        db_note = self.db.notes.find_one({"Title": note.title})
        if db_note is not None and db_note["IsDeleted"]:
            db_note = self._purge_note(db_note)
        if db_note is not None:
            note_updated = self._get_note_timestamp(note.updated)
            force_update = False  # for debugging 
            db_note_updated = self._get_db_timestamp(db_note, 'UpdatedTime')
            if db_note_updated < note_updated or force_update:
                logger.warning("note updated (or duplicate): '%s'\nupdated in db: %s\nupdated in en: %s", 
                               note.title, db_note_updated.isoformat(), note_updated.isoformat())
                self._update_db_note(db_note, note)
            else:
                logger.debug("note unchanged: '%s'", note.title)

        else:  # new note
            db_note = self._create_db_note(note)

        self._update_tags(db_note, note)

    def _get_note_timestamp(self, timestamp):
        """ convert timestamp to mongodb timestamp """
        if isinstance(timestamp, datetime):
            pass
        else:
            timestamp = datetime.fromtimestamp(timestamp / 1000.0)  # timezone awareness??
        if timestamp.tzinfo is None:
            # avoid TypeError: can't compare offset-naive and offset-aware datetimes
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
        logger.info("create new note '%s' in %s", note.title, self.notebook_name)
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

        noteId = bson.objectid.ObjectId()

        content = note.content
        EN_NOTE_2 = '\<\!DOCTYPE\s+en-note\s+SYSTEM\s+"http://xml.evernote.com/pub/enml2.dtd"\>'
        assert re.search(EN_NOTE_2, content) is not None, "content format unsupported: %s" % content[:180]

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

    def _update_note_count(self, notebook):
        """ update notes count for given notebook """
        note_count = self.db.notes.count_documents({'NotebookId': notebook['_id']})
        logger.info("update notebook note count to %s", note_count)
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
        content = note.content
        #TODO implement note update
        # similar to create: 
        # update images / add new ones  (evtl drop removed ones)
        # fix image refs (img / en-media)
        # compare new and old content, if changed:
        # create entry in note_content_histories with old note content
        # update note content
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
            resource = note.get_resource_by_hash(imageInfo['hash'])
            if resource is None:
                logger.warning("failed to match image by hash")
            else:
                handle_image(resource)
        
        return img_map
