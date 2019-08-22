#
"""
extract notes and related data from Evernote .enex 
and import into mongodb
"""

import argparse
from enexparser import EnexParser


def get_argparse():
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='enex file to import from')
    parser.add_argument('--tag', '-t', action='store', help='tag to apply additionally to all notes')
    return parser


def main():
    arg_parser = get_argparse()
    args = arg_parser.parse_args()

    enex_parser = EnexParser(args.input)
    for note in enex_parser.parse():
        note = note


if __name__ == "__main__":
    main()
