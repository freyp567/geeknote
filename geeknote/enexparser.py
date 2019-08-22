#
"""
enex parser
"""

import os
import re
from lxml import etree
import dateutil


class EnNote:
    """ wrap note from enex file to mimic EN api node """

    def __init__(self, note):
        self._extract_note_info(note)
        self._note = note

    def _extract_note_info(self, note):
        self.title = note.xpath('title')[0].text

        self.created = self._extract_dateval(note, 'created')
        self.updated = self._extract_dateval(note, 'updated')

        self.tags = [tag.text for tag in note.xpath('tag')]
        content = note.xpath('content')
        if content:
            self.content = content[0].text
        else:
            self.content = ''

        # attachements / files
        self.resources = []
        resources = note.xpath('resource')
        for resource in resources:
            resource_info = {}
            resource_info['filename'] = resource.xpath('resource-attributes/file-name')[0].text
            # Base64 encoded data has new lines!
            resource_info['data'] = re.sub(r'\n', '', resource.xpath('data')[0].text).strip()
            resource_info['mime_type'] = resource.xpath('mime')[0].text
            self.resources.append(resource_info)

    def _extract_dateval(self, note, date_field):
        if note.xpath(date_field):
            return dateutil.parser.parse(note.xpath(date_field)[0].text)
        else:
            return dateutil.parser.parse('19700101T000017Z')


class EnexParser:

    def __init__(self, enex_file):
        assert os.path.exists(self.enex_file), "missing %s" % repr(enex_file)
        self._enex_file = enex_file

    def parse(self):
        try:
            parser = etree.XMLParser(huge_tree=True)
            xml_tree = etree.parse(self._enex_file, parser)
        except (etree.XMLSyntaxError, ) as exc:
            raise ValueError("syntax error in enex file: %s" % exc)

        raw_notes = xml_tree.xpath('//note')
        for note in raw_notes:
            yield EnNote(note)
        return
