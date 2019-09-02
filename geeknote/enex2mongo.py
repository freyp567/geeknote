#
"""
extract notes and related data from Evernote .enex 
and import into mongodb
"""

import sys
import os
import traceback
import argparse
from enexparser import EnexParser
from updatenote import UpdateNote
import logging
import config


def setup_logger():
    # set default logger (write log to file)
    def_logpath = os.path.join(config.APP_DIR, 'enex2mongo.log')
    formatter = logging.Formatter('%(asctime)-15s : %(message)s')
    handler = logging.FileHandler(def_logpath)
    handler.setFormatter(formatter)

    logger = logging.getLogger("en2mongo")
    logger.setLevel(os.environ.get('LOGLEVEL') or logging.DEBUG)
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stderr))
    return logger


logger = setup_logger()

def get_argparse():
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='enex file to import from')
    parser.add_argument('--tag', '-t', action='store', help='tag to apply additionally to all notes')
    parser.add_argument('--notebook', '-n', action='store', help='notebook name')
    return parser


def main():
    arg_parser = get_argparse()
    args = arg_parser.parse_args()
    logger.info("run enex2mongo with args: %s", args)

    note = None
    notebook_name = args.notebook
    updater = UpdateNote(notebook_name)
    try:
        enex_parser = EnexParser(args.input)
        for note in enex_parser.parse():
            # add or update note in mongodb
            updater.update(note)
        logger.info("enex2mongo succeeded")

    except Exception as err:
        logger.exception("enex2mongo failed - %s" % err)
        sys.exit(1)
    return

if __name__ == "__main__":
    main()
