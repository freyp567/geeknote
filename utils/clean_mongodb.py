#!/usr/bin/env python2 # noqa: E902
# -*- coding: utf-8 -*-
""" cleanup data in mongodb
"""

import os
import sys
import argparse
import logging

from pymongo import MongoClient

from geeknote import config


# set default logger (write log to file)
def_logpath = os.path.join(config.APP_DIR, 'clean_mongodb.log')
formatter = logging.Formatter('%(asctime)-15s : %(message)s')
handler = logging.FileHandler(def_logpath)
handler.setFormatter(formatter)

logger = logging.getLogger("clean_mongodb")
logger.setLevel(os.environ.get('LOGLEVEL') or logging.DEBUG)
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler(sys.stderr))


class CleanMongoDB:
    """ cleanup mongodb """

    def __init__(self, args):
        self._args = args

        # connect to mongodb
        self.mongo_client = MongoClient(
            config.DB_URI,
            tz_aware=False,
            wTimeoutMS=2500,
        )
        self.db = self.mongo_client[config.DB_NAME]
        logger.info('cleanup start')

    def run(self):
        logger.info("removing %s note_contents ...", self.db.note_contents.count())
        self.db.note_contents.remove({})
        logger.info("removing %s note_content_histories ...", self.db.note_content_histories.count())
        self.db.note_content_histories.remove({})
        logger.info("removing %ss notes ...", self.db.notes.count())
        self.db.notes.remove({})
        logger.info("removing %s share_notes ...", self.db.share_notes.count())
        self.db.share_notes.remove({})
        logger.info("removing %s note_images ...", self.db.note_images.count())
        self.db.note_images.remove({})
        logger.info("removing %s note_tags ...", self.db.note_tags.count())
        self.db.note_tags.remove({})
        logger.info("removing %s files ...", self.db.files.count())
        self.db.files.remove({})
        if 0:
            logger.info("removing %s tag_count objs ...", self.db.tag_count.count())
            self.db.tag_count.remove({})
            logger.info("removing %s tags objs ...", self.db.tags.count())
            self.db.tags.remove({})
            logger.info("removing %s sessions objs ...", self.db.sessions.count())
            self.db.sessions.remove({})
        logger.info('cleanup done')


def main():
    parser = argparse.ArgumentParser()
    # parser.add_argument('--notebook', '-n', action='store')
    args = parser.parse_args()
    logger.debug('clean mongodb, args: %s', repr(args))

    try:
        app = CleanMongoDB(args)
        app.run()

    except Exception:
        logger.exception("clean mongodb failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
