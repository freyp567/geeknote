"""
search for notes in MongoDB
"""

from pymongo import MongoClient
from geeknote import config

import sys
import os
import argparse
import re
from datetime import datetime

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
        uri2 = re.sub("(mongodb://.+?)\:(.+?)@(.+)", r"\1@\3", uri)  # noqa: W605
        return uri2

    def search_title(self, keywords):
        query = {"Title": {"$regex": ".*%s.*" % re.escape(keywords)}}
        start = datetime.now()
        result = self.db.notes.find(query)
        duration = datetime.now() - start
        result_count = result.count()
        # TODO verify cause:
        # bson.errors.InvalidStringData: strings in documents must be valid UTF-8
        if result_count:
            LOGGER.info('found %s notes (in titles) dT=%.2f', 
                        result.count(), duration.total_seconds())
            for note in result:
                LOGGER.info('+ "%s"', note["Title"])
        else:
            LOGGER.info('no notes found for "%s" (in note titles)', keywords)
        search_info = {
            'field': 'Title',
            'term': keywords,
            'count': result_count,
            'duration': duration.total_seconds()
        }
        return search_info

    def search_content(self, keywords):
        LOGGER.info('searching for "%s"', keywords)
        # db.note_contents.createIndex( { name: "text", description: "note fulltext" } )
        # query = {"$text": {"contents": keywords}}

        # for beginning, use contains search
        # query = {"Content": {"$regex": ".*%s.*" % re.escape(keywords)}}
        # result = self.db.note_contents.find(query)

        query = {"content": {"$regex": ".*%s.*" % re.escape(keywords)}}
        result = self.db.note_contents.find(query)
        result_count = result.count()
        if result_count:
            LOGGER.info('found %s notes (in content)', result_count)
            # TODO show timing
            for note in result:
                note = note
        else:
            LOGGER.info('no notes found for "%s" (in note contents)', keywords)
        search_info = {
            'field': 'Citle',
            'term': keywords,
            'count': result_count,
            'duration': duration.total_seconds()
        }
        return search_info


def get_argparser():
    parser = argparse.ArgumentParser()
    # parser.add_argument('--notebook', '-n', action='store')
    return parser


def main():
    # future:
    # parser = get_argparser()
    # args = parser.parse_args()

    search_note = SearchNote()
    # TODO search terms (and expectation) from external config
    for search_term in (
        # 'Karawane',
        # u'Alexander der Große'
        'Haefs',  # expect 4 notes
        u'Gablé',  # expect 17 notes
        'Rose',  # expect 51 notes
        'Marco Polo: Bis ans Ende der Welt',  # expect 6 notes (fulltext), 1 note (exact)
        'Die Hüter der Rose – Wikipedia',  # expect 1 note
    ):
        search_note.search_title(search_term)
        # search_note.search_content(search_term)

    # TODO show tabular output of search term, duration, hits


if __name__ == "__main__":
    main()
