from __future__ import annotations

from pdfeditor.core.xmp import read_xmp_field, sync_xmp
from pdfeditor.model.metadata import Metadata

PDFA = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"
  xmlns:pdf="http://ns.adobe.com/pdf/1.3/" pdfaid:part="2" pdfaid:conformance="B"
  pdf:Producer="Old producer"/>
</rdf:RDF></x:xmpmeta>
<?xpacket end="w"?>"""


def test_new_packet_from_info() -> None:
    xml = sync_xmp("", Metadata(title="Report", author="Ann", keywords="a, b"))
    assert xml.startswith("<?xpacket begin")
    assert read_xmp_field(xml, "dc", "title") == "Report"
    assert read_xmp_field(xml, "dc", "creator") == "Ann"
    assert read_xmp_field(xml, "pdf", "Keywords") == "a, b"
    assert sync_xmp("", Metadata()) == ""  # nothing to write: no packet


def test_existing_packet_keeps_other_properties() -> None:
    xml = sync_xmp(PDFA, Metadata(title="T", producer="New producer"))
    assert 'pdfaid:part="2"' in xml  # the PDF/A identification survives
    assert read_xmp_field(xml, "pdf", "Producer") == "New producer"  # attribute form replaced
    assert xml.count("<pdf:Producer") == 1 and "pdf:Producer=" not in xml
    assert read_xmp_field(xml, "dc", "title") == "T"
    cleared = sync_xmp(xml, Metadata(producer="New producer"))
    assert read_xmp_field(cleared, "dc", "title") == ""  # emptied fields are removed
