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
        self.db.note_contents.remove({})
        self.db.note_content_histories.remove({})
        self.db.notes.remove({})
        self.db.share_notes.remove({})
        self.db.note_images.remove({})
        self.db.note_tags.remove({})
        self.db.files.remove({})
        if 0:
            self.db.tag_count.remove({})
            self.db.tags.remove({})
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
