#
"""
enex parser
"""

import os
import re
from lxml import etree
import dateutil
import dateutil.parser
import hashlib
import base64
import pytz


class EnNote:
    """ wrap note from enex file to mimic EN api node """

    def __init__(self, note):
        self._extract_note_info(note)
        self._note = note

    @property
    def tagNames(self):
        return self.tags

    def _parse_date(self, value):
        # value = unicode(value)  #TODO avoid Unicode equal comparison failed
        # https://stackoverflow.com/questions/21296475/python-dateutil-unicode-warning
        return dateutil.parser.parse(value)

    def _extract_note_info(self, note):
        self.title = note.xpath('title')[0].text

        self.created = self._extract_dateval(note, 'created')
        self.updated = self._extract_dateval(note, 'updated')

        self.tags = [tag.text for tag in note.xpath('tag')]
        content = note.xpath('content')
        if content:
            self.content = content[0].text
        else:
            self.content = ''  # no content?

        # attachements / files
        self.resources = []
        resources = note.xpath('resource')
        for resource in resources:
            resource_obj = ENResource(resource)
            if resource_obj.data:
                self.resources.append(resource_obj)

    def _extract_dateval(self, note, date_field):
        if note.xpath(date_field):
            date_value = note.xpath(date_field)[0].text
        else:
            date_value = '19700101T000000Z'
        date_value = self._parse_date(date_value)
        if date_value.tzinfo is None:
            date_value = pytz.utc.localize(date_value)
        return date_value

    def get_resource_by_hash(self, hash):
        for resource in self.resources:
            if resource.hash == hash:
                return resource
        return None

    def load_content(self):
        pass  # already extracted

    def load_tags(self):
        pass  # already extracted

    def __getattr__(self, name):
        # notfound = object()
        # value = getattr(self, name, notfound)
        # if value is notfound:
        raise AttributeError(name)
        # return value


class ENResourceData:

    def __init__(self, data):
        data = re.sub(r'\n', '', data).strip()
        self.body = base64.b64decode(data, altchars=None)  # , validate=True


class ENResource:

    def __init__(self, resource):
        self._extract_resource_info(resource)

    def _extract_resource_info(self, resource):
        self.data = self.hasn = None
        self.mime_type = resource.xpath('mime')[0].text
        fn_node = resource.xpath('resource-attributes/file-name')
        if fn_node:
            self.filename = fn_node[0].text
        else:
            self.filename = 'unnamed'
        # Base64 encoded data has new lines!
        data_node = resource.xpath('data')[0]
        data_encoding = data_node.attrib.get('encoding')
        if data_encoding != "base64":
            # rarely, but happending
            assert data_encoding is None, "unknown data encoding: %s" % data_encoding
            # logger.error("unsupported data encoding: %s" % data_encoding) # note "fst_verknuepfungen  - EDBCore, mgmt script" in hrs
            return
        self.data = ENResourceData(data_node.text)
        self.hash = hashlib.md5(self.data.body).hexdigest()

        """
        additional resource info currently ignored / discarded:
    ...
        <width>72</width><height>36</height>
        <recognition><![CDATA[<?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE recoIndex PUBLIC "SYSTEM" "http://xml.evernote.com/pub/recoIndex.dtd">
            <recoIndex docType="unknown" objType="image" objID="eb75ae62c7ea56c17751b81b41a4f6c2" engineVersion="7.0.24.1"
                recoType="service" lang="de" objWidth="72" objHeight="36"><item x="1" y="0" w="71" h="15">
                <t w="60">:divibib</t></item>
                <item x="8" y="19" w="28" h="8"><t w="42">digitale</t><t w="34">digitate</t></item>
                <item x="41" y="20" w="30" h="6"><t w="55">vIrtuellE</t><t w="40">vIrtuell</t><t w="35">vlttuellE</t><t w="26">vlnuellf</t><t w="24">vlnuene</t><t w="20">vIrtuell!</t><t w="18">virtue</t></item><item x="8" y="29" w="62" h="6"><t w="70">bibliotheken</t><t w="46">bibliothehr</t><t w="38">bibliothek en</t><t w="27">bibliothek e</t><t w="24">bib lio the ken</t><t w="21">bib lio the hr</t><t w="17">bib! i other</t><t w="16">bib! j other</t><t w="16">bib! jot her</t><t w="14">bib l i other</t></item></recoIndex>
            ]]>
        </recognition>
        <resource-attributes>
            <timestamp>20170504T185946Z</timestamp>
            <file-name>24ac2d107f98bca1d444422b3c57813b.png</file-name>
        </resource-attributes>
    </resource>

        """


class EnexParser:

    def __init__(self, enex_file):
        assert os.path.exists(enex_file), "missing %s" % repr(enex_file)
        self._enex_file = enex_file

    def parse(self):
        try:
            parser = etree.XMLParser(huge_tree=True, resolve_entities=False)
            data = self._enex_file
            xml_tree = etree.parse(data, parser)
        except (etree.XMLSyntaxError, ) as exc:
            raise ValueError("syntax error in enex file: %s" % exc)

        raw_notes = xml_tree.xpath('//note')
        for note in raw_notes:
            yield EnNote(note)
        return
