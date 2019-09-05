#!/usr/bin/env python2 # noqa: E902
# -*- coding: utf-8 -*-
"""
search for notes in MongoDB
"""

import pymongo
import bson
from geeknote import config

import sys
import os
import argparse
import re
from datetime import datetime
import csv

import logging

SEARCH_TERMS = (
    # TODO read search terms (and expectation) from external config
    u'Karawane',  # expect 14 notes
    u'Alexander der Große',
    u'Haefs',  # expect 4 notes
    u'Gablé',  # expect 17 notes
    u'Rose',  # expect 51 notes
    u'Marco Polo: Bis ans Ende der Welt',  # expect 6 notes (fulltext), 1 note (exact)
    u'Die Hüter der Rose – Wikipedia',  # expect 1 note
)


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


def encode_log(value):
    output_encoding = 'ascii'  # 'latin-1'
    if not isinstance(value, basestring):
        value = str(value)
    if isinstance(value, unicode):
        return value.encode(output_encoding, 'xmlcharrefreplace')
    else:
        return value


class SearchSpecBase:

    def __init__(self, db):
        self.db = db

    def prepare(self):
        pass  # optional method, to check for index / setup on demand

    def info(self):
        assert False, 'info to be implemented by derived class'

    def get_collection(self):
        assert False, 'get_collection to be implemented by derived class'

    def build_query(self):
        assert False, 'build_query to be implemented by derived class'


class SearchTitleContains(SearchSpecBase):

    def info(self):
        return 'TitleContains'

    def get_collection(self):
        return self.db.notes

    def build_query(self, search_term):
        query = {"Title": {"$regex": ".*%s.*" % re.escape(search_term)}}
        return query


class SearchContentRegex(SearchSpecBase):

    def info(self):
        return 'ContentRegex'

    def prepare(self):
        # coll = self.get_collection()
        # coll.create_index({'Content': 1}, background=False)
        # but regex with left truncation is unable to use the index
        pass

    def get_collection(self):
        return self.db.note_contents

    def build_query(self, search_term):
        # query = {"Content": {"$regex": ".*%s.*" % re.escape(search_term)}}
        search_term = ".*%s.*" % re.escape(search_term)
        regx = bson.regex.Regex(".*%s.*" % search_term)
        query = {"Content": regx}
        return query


class SearchContentRegex2(SearchSpecBase):

    def info(self):
        return 'ContentRegex2'

    def prepare(self):
        # speedup using combined fulltext and regex search? see
        # https://medium.com/statuscode/how-to-speed-up-mongodb-regex-queries-by-a-factor-of-up-to-10-73995435c606
        collection = self.get_collection()
        # index = pymongo.IndexModel([('Content', pymongo.TEXT), ])
        # collection.create_index(index, name='note_content', default_language='german')  # fails
        index = [('Content', pymongo.TEXT)]  
        collection.create_index(index, name='note_content', default_language='german')

    def get_collection(self):
        return self.db.note_contents

    def build_query(self, search_term):
        regex_match = {"$regex": "|".join([re.escape(word) for word in search_term.split()])}
        query = {
            "$and": [
                {"$text": {"$search": search_term}},
                {"Content": {"$elemMatch": regex_match}}
            ]
        }
        # ATTN does not work as expected / described  -- use aggregate pipeline instead?
        return query


class SearchContentFulltext(SearchSpecBase):

    def info(self):
        return 'ContentFulltext'

    def prepare(self):
        collection = self.get_collection()
        index = [('Content', pymongo.TEXT)]  
        collection.create_index(index, name='note_content', default_language='german')
        # TODO fix SyntaxError: Invalid Syntax

    def get_collection(self):
        return self.db.note_contents

    def build_query(self, search_term):
        # TODO verify / precodition (but done otherplace):
        # db.note_contents.createIndex( { name: "Content", description: "note fulltext" } )
        query = {"$text": 
        {
            "$search": search_term,
            # "$language": 
            # "$caseSensitive":
            # "$diacrticSensitive": 
        }}
        return query


# TODO allow to select using arguments 'ContentRegex2', 'ContentFulltext', 'TitleContains'
# SEARCH_SPEC = SearchTitleContains
#SEARCH_SPEC = SearchContentRegex2  # ATTN does not work
SEARCH_SPEC = SearchContentFulltext


class SearchNote:

    def __init__(self):
        # connect to mongodb
        self.mongo_client = pymongo.MongoClient(
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

    def search(self, search_term, search_spec):
        LOGGER.info('searching for "%s" in %s', encode_log(search_term), search_spec.info())
        query = search_spec.build_query(search_term)
        collection = search_spec.get_collection()
        start = datetime.now()
        result = collection.find(query)
        result_count = result.count()  # experience long delay with regex search
        duration = datetime.now() - start
        if result_count:
            LOGGER.info('found %s notes (in %s) dT=%.2f',
                        result_count, search_spec.info(), duration.total_seconds())
            for doc in result:
                if 'Title' in doc:
                    note = doc
                else:
                    # have note_contents document, need to lookup note (metadata)
                    note = self.db.notes.find_one({'_id': doc['_id']})
                    assert note is not None
                notebook = self.db.notebooks.find_one({'_id': note['NotebookId'] })
                LOGGER.debug('+ "%s" in "%s"', encode_log(note["Title"]), notebook['Title'])
            duration2 = datetime.now() - start  # takes very long with regex finds
        else:
            duration2 = datetime.now() - start
            LOGGER.info('no notes found for "%s" (in %s)', encode_log(search_term), search_spec.info())
        search_info = {
            'term': search_term,
            'count': str(result_count),
            'dTfind': str(duration.total_seconds()),
            'dTfetch': str(duration2.total_seconds())
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
    search_results = []

    search_spec = SEARCH_SPEC(search_note.db)
    LOGGER.info("prepare search %s", search_spec.info())
    search_spec.prepare()

    for search_term in SEARCH_TERMS:
        try:
            result_info = search_note.search(search_term, search_spec)
        except Exception as err:
            LOGGER.error('search failed for "%s" (%s): %s',
                         encode_log(search_term), search_spec, encode_log(err))
            result_info = {
                'term': search_term,
                'count': '--',
                'dTfind': '??',
                'dTfetch': '??',
                'failed': str(err)
            }
        search_results.append(result_info)

    # output summary of search results
    search_info = SEARCH_SPEC(search_note.db).info()
    if not os.path.isdir("search_notes"):
        os.mkdir('search_notes')
    result_path = 'search_notes\\%s.%s.csv' % (search_info, datetime.now().strftime('%Y-%m-%dT%H%M'))
    open(result_path +'.txt', 'w').write('search result summary for %s\n%s\n' % (search_info, datetime.now().isoformat()))
    columns = ('term', 'count', 'dTfind', 'dTfetch')
    with open(result_path, 'w') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns) # TODO handle utf-8
        writer.writeheader()

        for info in search_results:
            info['term'] = encode_log(info['term'])  # avoid hassles with unicode - get rid if utf-8 supported
            writer.writerow(info)

    LOGGER.info("\n")
    LOGGER.info("search result summary see %s\n", result_path)
    return


if __name__ == "__main__":
    main()
