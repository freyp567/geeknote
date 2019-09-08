#!/usr/bin/env python2 # noqa: E902
# -*- coding: utf-8 -*-
"""
extract notes and related data from Evernote .enex
and import into mongodb

note: should use note.guiid as base for lookup, but unfortunately do not have it in enex

TODO:
+ analyze 'failed to match image by hash', e.g. for "ein Rufer in der Wueste" in bibel (and "failed to fetch image from img_map")
+ seen tag removal during repated sync, to be verified - normalization issues? see "removed tag:"
+ during initial import, see 'note updated' messages (what is unexpected)
+ data encoding mussing (rare cases): e.g. note "fst_verknuepfungen  - EDBCore, mgmt script" in hrs
+ fix encoding / display when logging to console vs logfile (e.g. 'Der Spion des K├╢nigs - reading')

"""

import sys
import os
import argparse
from enexparser import EnexParser
from updatenote import UpdateNote
from datetime import datetime
import json
import logging
import config


def setup_logger(logname):
    LOG_FORMAT = '%(asctime)-15s %(levelname)s  %(message)s'
    LOG_FORMAT_2 = '%(asctime)-15s  %(message)s'
    LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
    logpath = os.path.join(config.APP_DIR, 'enex2mongo.log')  # config.ERROR_LOG
    logpath = os.path.abspath(logpath)
    print("setup logging, logpath='%s'\n" % logpath)
    logging.basicConfig(format=LOG_FORMAT, filename=logpath)  # datefmt=

    # set default logger (write log to file)
    formatter = logging.Formatter(LOG_FORMAT_2, LOG_DATEFMT)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    logger = logging.getLogger()  # root logger
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    logger = logging.getLogger(logname)
    logger.setLevel(os.environ.get('LOGLEVEL') or logging.INFO)
    return logger


logger = setup_logger("en2mongo")


def get_argparse():
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='enex file to import from')
    parser.add_argument('--tag', '-t', action='store', help='tag to apply additionally to all notes')
    parser.add_argument('--notebook', '-n', action='store', help='notebook name')
    return parser


def update_notebook(enex_path, notebook_name):
    updater = UpdateNote(notebook_name)
    enex_parser = EnexParser(enex_path)
    note_count = 0
    last_update = datetime(1970, 01, 01)
    for note in enex_parser.parse():
        # add or update note in mongodb
        updater.update(note)
        note_count += 1
        if note.updated > last_update:
            last_update = note.updated
    logger.info("total %s notes for notebook %s last_update=%s", note_count, notebook_name, last_update)
    return last_update


def main():
    arg_parser = get_argparse()
    args = arg_parser.parse_args()
    logger.info("run enex2mongo with args: %s", args)

    last_update = datetime(1970, 01, 01)
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
                last_update_nb = update_notebook(enex_path, notebook_name)
                if last_update:
                    last_update = max(last_update, last_update_nb)
                else:
                    last_update = last_update_nb

            if last_update.year > 1970:
                last_update_info = {
                    'succeeded': last_update
                }
                last_update_fn = config.LAST_UPDATE_FN
                # if os.path.isfile(last_update_fn):
                #    last_update_info = json.load(open(last_update_fn, 'r'), object_hook=fix_last_update)
                #    changed_after = last_update_info['succeeded']

                json.dump(last_update_info, open(last_update_fn, 'w'), default=str, indent=4)
                logger.info("set last_update=%s in %s", last_update, last_update_fn)

        else:
            notebook_name = os.path.splitext(os.path.basename(enex_path))[0]
            if args.notebook and notebook_name != args.notebook:
                raise ValueError("bad notebook name: %s != %s", args.notebook, notebook_name)
            update_notebook(enex_path, notebook_name)
        logger.info("enex2mongo succeeded")

    except Exception as err:
        logger.exception("enex2mongo failed syncing %s - %s", notebook_name, err)
        sys.exit(1)
    return


if __name__ == "__main__":
    main()
