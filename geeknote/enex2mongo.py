#
"""
extract notes and related data from Evernote .enex 
and import into mongodb

note: should use note.guiid as base for lookup, but unfortunately do not have it in enex
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
    logger.setLevel(os.environ.get('LOGLEVEL') or logging.INFO)
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


def update_notebook(enex_path, notebook_name):
    updater = UpdateNote(notebook_name)
    enex_parser = EnexParser(enex_path)
    for note in enex_parser.parse():
        # add or update note in mongodb
        updater.update(note)

def main():
    arg_parser = get_argparse()
    args = arg_parser.parse_args()
    logger.info("run enex2mongo with args: %s", args)

    notebook_name = '(loading)'
    try:
        enex_path = args.input
        if os.path.isdir(enex_path):
            # import all .enex files in given directory
            enex_dir = enex_path
            enex_files = [fn for fn in os.listdir(enex_dir) if fn.endswith('.enex')]
            for enex_file in enex_files:
                # assume .enex file name matches notebook name (MUST, dont know how to map otherwise)
                notebook_name = os.path.splitext(os.path.basename(enex_file))[0]
                enex_path = os.path.join(enex_dir, enex_file)
                update_notebook(enex_path, notebook_name)
        else:
            notebook_name = args.notebook
            update_notebook(enex_path, notebook_name)
        logger.info("enex2mongo succeeded")

    except Exception as err:
        logger.exception("enex2mongo failed syncing %s - %s", notebook_name, err)
        sys.exit(1)
    return

if __name__ == "__main__":
    main()
