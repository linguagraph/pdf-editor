"""Elanero - PDF Editor: an Acrobat-style desktop PDF editor."""

__version__ = "0.1.0"
# The product name users see (window titles, About, the exe's version resource, the installer,
# Explorer's "Open with"; the exe itself is Elanero.exe). Internal identifiers keep "pdfeditor"
# (``core.paths.APP_NAME``, the settings and data folders, the .pdf ProgID): renaming those
# would lose users' settings and break existing file associations.
APP_DISPLAY_NAME = "Elanero - PDF Editor"
# Where the complete corresponding source is offered (AGPL section 6/13).
SOURCE_URL = "https://github.com/linguagraph/pdf-editor"
# The privacy policy (PRIVACY.md), linked from Help > About and the Store listing.
PRIVACY_URL = SOURCE_URL + "/blob/main/PRIVACY.md"
