"""Keep the XMP metadata packet in step with the Info dictionary.

PDF 2.0 readers (and PDF/A validators) take title/author/... from XMP, older ones from the
Info dictionary; editing only one leaves them disagreeing. ``sync_xmp`` writes the Info fields
into an existing packet (keeping everything else in it, e.g. a PDF/A identification) or builds
a minimal packet when there is none.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from pdfeditor.model.metadata import Metadata

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "pdf": "http://ns.adobe.com/pdf/1.3/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "pdfaid": "http://www.aiim.org/pdfa/ns/id/",
    "pdfuaid": "http://www.aiim.org/pdfua/ns/id/",
    "xmpMM": "http://ns.adobe.com/xap/1.0/mm/",
    "xmpRights": "http://ns.adobe.com/xap/1.0/rights/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "pdfaExtension": "http://www.aiim.org/pdfa/ns/extension/",
    "pdfaSchema": "http://www.aiim.org/pdfa/ns/schema#",
    "pdfaProperty": "http://www.aiim.org/pdfa/ns/property#",
}
# conventional prefixes on output (some validators and tools expect them)
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)

_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_PACKET_HEAD = '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
_PACKET_TAIL = '\n<?xpacket end="w"?>'


def _q(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


# (prefix, name, container) — container: "Alt" (language alternatives), "Seq", or None (simple)
_FIELDS: dict[str, tuple[str, str, str | None]] = {
    "title": ("dc", "title", "Alt"),
    "author": ("dc", "creator", "Seq"),
    "subject": ("dc", "description", "Alt"),
    "keywords": ("pdf", "Keywords", None),
    "creator": ("xmp", "CreatorTool", None),
    "producer": ("pdf", "Producer", None),
}


def _parse(xml: str) -> ET.Element | None:
    body = re.sub(r"<\?xpacket[^>]*\?>", "", xml).strip()
    if not body:
        return None
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None


def _new_root() -> ET.Element:
    root = ET.Element(_q("x", "xmpmeta"))
    rdf = ET.SubElement(root, _q("rdf", "RDF"))
    desc = ET.SubElement(rdf, _q("rdf", "Description"))
    desc.set(_q("rdf", "about"), "")
    return root


def _set_field(
    descs: list[ET.Element], prefix: str, name: str, container: str | None, value: str
) -> None:
    tag = _q(prefix, name)
    for d in descs:  # drop every existing form: attribute or element, in any Description
        d.attrib.pop(tag, None)
        for child in d.findall(tag):
            d.remove(child)
    if not value:
        return
    prop = ET.SubElement(descs[0], tag)
    if container is None:
        prop.text = value
        return
    box = ET.SubElement(prop, _q("rdf", container))
    item = ET.SubElement(box, _q("rdf", "li"))
    if container == "Alt":
        item.set(_XML_LANG, "x-default")
    item.text = value


def sync_xmp(xml: str, meta: Metadata) -> str:
    """``xml`` with the Info fields of ``meta`` written in; "" stays "" when meta is empty."""
    root = _parse(xml) if xml else None
    if root is None:
        if not any(getattr(meta, f) for f in _FIELDS):
            return xml
        root = _new_root()
    rdf = root if root.tag == _q("rdf", "RDF") else root.find(_q("rdf", "RDF"))
    if rdf is None:
        rdf = ET.SubElement(root, _q("rdf", "RDF"))
    descs = rdf.findall(_q("rdf", "Description"))
    if not descs:
        d = ET.SubElement(rdf, _q("rdf", "Description"))
        d.set(_q("rdf", "about"), "")
        descs = [d]
    for field_name, (prefix, name, container) in _FIELDS.items():
        _set_field(descs, prefix, name, container, getattr(meta, field_name))
    return _PACKET_HEAD + ET.tostring(root, encoding="unicode") + _PACKET_TAIL


def read_xmp_field(xml: str, prefix: str, name: str) -> str:
    """The text of a simple or first-list-item property ("" if absent); for tests and checks."""
    root = _parse(xml)
    if root is None:
        return ""
    tag = _q(prefix, name)
    for d in root.iter(_q("rdf", "Description")):
        if tag in d.attrib:
            return d.attrib[tag]
        el = d.find(tag)
        if el is not None:
            li = el.find(f".//{_q('rdf', 'li')}")
            return (li.text if li is not None else el.text) or ""
    return ""
