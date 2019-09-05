"""
search for notes in MongoDB
"""

from pymongo import MongoClient
from geeknote import config

import sys
import os
import argparse
import re 

import logging


def setup_logging(logname):
    # set default logger (write log to file)
    def_logpath = os.path.join(config.APP_DIR, 'clean_mongodb.log')
    formatter = logging.Formatter('%(asctime)-15s : %(message)s')
    handler = logging.FileHandler(def_logpath)
    handler.setFormatter(formatter)

    logger = logging.getLogger("clean_mongodb")
    logger.setLevel(os.environ.get('LOGLEVEL') or logging.DEBUG)
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stderr))
    return logger


LOGGER = setup_logging("en2mongo.search")


class SearchNote:

    def __init__(self):
        # connect to mongodb
        self.mongo_client = MongoClient(
            config.DB_URI,
            tz_aware=False,
            wTimeoutMS=2500,
        )
        self.db = self.mongo_client[config.DB_NAME]
        uri = self._clean_uri_for_logging(config.DB_URI)
        LOGGER.info('connected to MongoDB %s on %s', config.DB_NAME, uri)

    def _clean_uri_for_logging(self, uri):
        """ dont show password """
        uri2 = re.sub("(mongodb://.+?)\:(.+?)@(.+)", r"\1@\3", uri)
        return uri2

    def search(self, keywords):
        LOGGER.info('searching for "%s"', keywords)
        # db.note_contents.createIndex( { name: "text", description: "note fulltext" } )
        # query = {"$text": {"contents": keywords}}

        # for beginning, use contains search
        # query = {"content": {"$regex": ".*%s.*" % re.escape(keywords)}}
        # result = self.db.note_contents.find(query)

        query = {"Titel": {"$regex": ".*%s.*" % re.escape(keywords)}}
        result = self.db.notes.find(query)
        LOGGER.info('found %s notes', result.count()) 
        # TODO show timing
        for note in result:
            note = note
        return


def get_argparser():
    parser = argparse.ArgumentParser()
    # parser.add_argument('--notebook', '-n', action='store')
    return parser


def main():
    # future:
    # parser = get_argparser()
    # args = parser.parse_args()

    search_note = SearchNote()
    for search_term in (
        # 'Karawane',
        # u'Alexander der Große'
        'Haefs',
        ):
        search_note.search(search_term)

    # TODO show tabular output of search term, duration, hits


if __name__ == "__main__":
    main()
