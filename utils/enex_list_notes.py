#!/usr/bin/env python2 # noqa: E902
# -*- coding: utf-8 -*-
"""
ls for notes in .enex files - list title, size, created, updated
"""
import sys
import os
import argparse
from datetime import datetime

import warnings

from geeknote.enexparser import EnexParser
import geeknote.config as config

import logging

warnings.filterwarnings("ignore", message="Unicode equal comparison failed to convert both arguments to Unicode")


class SortNote:

    def __init__(self, args):
        self.args = args 

    def __call__(self, note):
        sort = self.args.sort
        if sort == 'time':
            return note['updated']
        elif sort == 'size':
            return note['size']
        else:
            return note['title']


def setup_logger(log_name):
    # set default logger (write log to file)
    def_logpath = os.path.join(config.APP_DIR, 'enex2mongo.log')
    formatter = logging.Formatter('%(asctime)-15s : %(message)s')
    handler = logging.FileHandler(def_logpath)
    handler.setFormatter(formatter)

    logger = logging.getLogger(log_name)
    logger.setLevel(os.environ.get('LOGLEVEL') or logging.INFO)
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stderr))
    return logger


logger = setup_logger("en2mongo.list")


def get_note_updated(note):
    updated = note.updated or note.created
    if updated.year == 1970:
        updated = note.created
    return updated

def list_notes(enex_path, notebook_name, args):
    logger.info("%s:", notebook_name)
    enex_parser = EnexParser(enex_path)
    notes = []
    notes_count = 0
    for note in enex_parser.parse():
        notes_count += 1
        info = {}
        note_size = len(note.content)
        if args.minsize and note_size < args.minsize:
            continue
        info['size'] = note_size
        info['updated'] = get_note_updated(note)
        info['title'] = note.title
        notes.append(info)
    logger.info("total %s", notes_count)  # len(notes))

    if args.sort:  # sorting on notes list
        sort_notes = SortNote(args)
        notes = sorted(notes, key=sort_notes)
    if args.reverse:
        notes = reversed(notes)

    for info in notes:
        updated = info['updated'] or info['created']
        updated = updated.strftime("%c")
        title = info['title']
        if len(title) > 60:
            title = title[:60] + '..'
        # TODO adjust title encoding to match console codepage - assume latin-1 for now
        if isinstance(title, unicode):
            # title = title.encode('latin-1', 'xmlcharrefreplace')
            # title = unicode(title, 'utf-8')
            title = title
        logger.info("%8s %12s %s", info['size'], updated, title)
    logger.info("")
    return


def get_argparse():
    parser = argparse.ArgumentParser()
    parser.add_argument('enexdir', help='directory with .enex files to list notes for')
    parser.add_argument('--sort', help='sort by WORD instead of name (size, time)')
    parser.add_argument('--reverse', '-r', action='store_true', help='reverse order while sorting')
    parser.add_argument('--minsize', help='list only notes larger than given size', type=int, default=0)
    return parser


def main():
    arg_parser = get_argparse()
    args = arg_parser.parse_args()
    logger.info("run enex_list_notes with args: %s", args)

    try:
        enex_dir = args.enexdir
        assert os.path.isdir(enex_dir), "directory not found: %s" % enex_dir
        enex_files = [fn for fn in os.listdir(enex_dir) if fn.endswith('.enex')]
        if not enex_files:
            logger.info("no .enex files found in %s", enex_dir)
            return
        for enex_file in enex_files:
            # assume .enex file name matches notebook name - what is when created using evernote-backup.cmd
            notebook_name = os.path.splitext(os.path.basename(enex_file))[0]
            enex_path = os.path.join(enex_dir, enex_file)
            list_notes(enex_path, notebook_name, args)
        logger.info("enex_list_notes done.")

    except Exception as err:
        logger.exception("enex_list_notes failed for '%s' - %s", enex_dir, err)
        sys.exit(1)
    return


if __name__ == "__main__":
    main()
