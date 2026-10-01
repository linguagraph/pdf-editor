"""Short ribbon labels and one-line help for commands, keyed by ``action_id``.

Menus and the command palette keep each action's full text; ribbon buttons, which already sit
under a group caption ("Pages", "Redact", ...), show the short label. Tooltips combine the full
name, the current shortcut and the help line, and are refreshed whenever shortcuts change.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

from PySide6.QtGui import QAction, QKeySequence

from pdfeditor.ui.shortcuts import action_id

SHORT_LABELS: dict[str, str] = {
    "document-properties": "Properties",
    "previous-page": "Previous",
    "next-page": "Next",
    "edit-text-images": "Edit",
    "add-text": "Text",
    "add-image": "Image",
    "add-rectangle": "Rectangle",
    "add-ellipse": "Ellipse",
    "add-line": "Line",
    "replace-image": "Replace",
    "export-image": "Export",
    "delete-selected-objects": "Delete",
    "organize-pages": "Organize",
    "insert-pages": "Insert",
    "delete-pages": "Delete",
    "duplicate-pages": "Duplicate",
    "rotate-pages-left": "Rotate Left",
    "rotate-pages-right": "Rotate Right",
    "extract-pages": "Extract",
    "replace-pages": "Replace",
    "split-document": "Split",
    "combine-files-into-pdf": "Combine",
    "crop-pages": "Crop",
    "page-labels": "Labels",
    "bates-numbering": "Bates",
    "find-text-to-redact": "Find Text",
    "mark-whole-pages": "Mark Pages",
    "redaction-properties": "Properties",
    "apply-redactions": "Apply",
    "sanitize-document": "Sanitize",
    "encrypt-with-password": "Encrypt",
    "remove-security": "Remove",
    "comments-list": "List",
    "delete-comment": "Delete",
    "flatten-all-comments": "Flatten",
    "single-page": "Single",
    "single-page-continuous": "Continuous",
    "two-up-continuous": "Two-Up",
    "two-up-with-cover-page": "Two-Up Cover",
    "rotate-view-counterclockwise": "Rotate Left",
    "rotate-view-clockwise": "Rotate Right",
    "night-reading": "Night",
    "recognize-text-ocr": "OCR",
    "export-pdf": "Export",
    "extract-images": "Images",
    "extract-fonts": "Fonts",
    "create-pdf-from-office-file": "From Office",
    "reduce-file-size": "Reduce Size",
    "compare-files": "Compare",
    "pdf-a-preflight": "Preflight",
    "save-as-pdf-a": "Save PDF/A",
    "accessibility-check": "Accessibility",
}

HELP: dict[str, str] = {
    "open": "Open a PDF from your computer.",
    "save": "Save your changes to this file.",
    "print": "Print the document or save it as a PDF.",
    "document-properties": "Title, author, fonts, security and other file details.",
    "undo": "Undo the last change.",
    "redo": "Redo the change you just undid.",
    "select-tool": "Select text, comments and objects. Esc always returns here.",
    "hand-tool": "Drag the page to scroll.",
    "find": "Search the document's text.",
    "back": "Go back to where you were before the last jump.",
    "forward": "Go forward again after going back.",
    "previous-page": "Go to the previous page.",
    "next-page": "Go to the next page.",
    "edit-text-images": "Click text or images on the page to change, move or resize them.",
    "add-text": "Click on the page to add a new line of text.",
    "add-image": "Place an image from a file on the page.",
    "add-rectangle": "Draw a rectangle into the page content.",
    "add-ellipse": "Draw an ellipse into the page content.",
    "add-line": "Draw a line into the page content.",
    "replace-image": "Swap the selected image for another picture.",
    "export-image": "Save the selected image as a file.",
    "delete-selected-objects": "Remove the selected text or images from the page.",
    "organize-pages": "See all pages as thumbnails to reorder, rotate or delete them.",
    "insert-pages": "Insert blank pages or pages from another file.",
    "delete-pages": "Delete the selected pages.",
    "duplicate-pages": "Copy the selected pages.",
    "rotate-pages-left": "Turn the selected pages 90° counterclockwise.",
    "rotate-pages-right": "Turn the selected pages 90° clockwise.",
    "extract-pages": "Save selected pages as a new PDF.",
    "replace-pages": "Replace pages with pages from another file.",
    "split-document": "Split the document into several files.",
    "combine-files-into-pdf": "Merge several files into one PDF.",
    "crop-pages": "Trim page margins.",
    "page-labels": "Number pages with roman numerals, prefixes and so on.",
    "header-footer": "Add text such as page numbers or dates to every page.",
    "bates-numbering": "Stamp sequential legal numbers on every page.",
    "watermark": "Add text or an image across the pages.",
    "background": "Put a color or an image behind the page content.",
    "redact": "Drag over content to mark it for permanent removal.",
    "find-text-to-redact": "Find words or patterns (emails, numbers) and mark them all.",
    "mark-whole-pages": "Mark entire pages for redaction.",
    "redaction-properties": "Fill color and overlay text for redaction marks.",
    "apply-redactions": "Permanently remove everything under the redaction marks.",
    "sanitize-document": "Remove hidden data: metadata, scripts, attachments, hidden layers.",
    "encrypt-with-password": "Require a password to open, print or change the file.",
    "remove-security": "Remove passwords and restrictions.",
    "highlight": "Highlight selected text.",
    "underline": "Underline selected text.",
    "strikethrough": "Strike through selected text.",
    "squiggly": "Draw a wavy underline under selected text.",
    "sticky-note": "Click on the page to add a note.",
    "text-box": "Add a box of text on top of the page.",
    "callout": "Add a text box with a pointer to what it's about.",
    "stamp": "Stamp the page with Approved, Draft or your own image.",
    "attach-file": "Attach a file at a spot on the page.",
    "rectangle": "Draw a rectangle comment.",
    "oval": "Draw an oval comment.",
    "line": "Draw a line comment.",
    "arrow": "Draw an arrow comment.",
    "polygon": "Draw a closed shape: click each corner, double-click to finish.",
    "polyline": "Draw connected lines: click each point, double-click to finish.",
    "pen": "Draw freehand.",
    "comments-list": "Show all comments in the side panel.",
    "delete-comment": "Delete the selected comments.",
    "flatten-all-comments": "Make comments part of the page so they can't be edited.",
    "zoom-out": "Make the page smaller.",
    "zoom-in": "Make the page bigger.",
    "fit-width": "Zoom so the page fills the window's width.",
    "fit-page": "Zoom so the whole page is visible.",
    "actual-size": "Show the page at 100%.",
    "single-page": "Show one page at a time.",
    "single-page-continuous": "Scroll through pages one under another.",
    "two-up-continuous": "Show pages side by side.",
    "two-up-with-cover-page": "Side by side, with the first page alone like a book cover.",
    "rotate-view-counterclockwise": "Turn the view (not the file) to the left.",
    "rotate-view-clockwise": "Turn the view (not the file) to the right.",
    "night-reading": "Dark pages with light text, easier on the eyes.",
    "recognize-text-ocr": "Make scanned pages searchable and selectable.",
    "batch-ocr": "Recognize text in many files at once.",
    "export-pdf": "Save as Word, images, text, HTML or Markdown.",
    "extract-images": "Save every image in the document as a file.",
    "extract-fonts": "Save the document's embedded fonts.",
    "create-pdf-from-office-file": (
        "Convert Word, Excel, PowerPoint or OpenDocument files to PDF (needs LibreOffice)."
    ),
    "reduce-file-size": "Make the file smaller by compressing images and removing waste.",
    "space-usage": "See what takes up space in the file.",
    "compare-files": "Show the differences between two versions of a document.",
    "pdf-a-preflight": "Check whether the file meets the PDF/A archiving standard.",
    "save-as-pdf-a": "Save a copy that meets the PDF/A archiving standard.",
    "accessibility-check": "Check how well the document works with screen readers.",
    "auto-tag-document-experimental": (
        "Add tags (headings, paragraphs, figures) from the layout, for screen readers."
    ),
}


def apply_short_labels(actions: Iterable[QAction]) -> None:
    for action in actions:
        short = SHORT_LABELS.get(action_id(action))
        if short:
            action.setIconText(short)


def tooltip(action: QAction) -> str:
    name = html.escape(action.text().replace("&&", "\0").replace("&", "").replace("\0", "&"))
    name = name.removesuffix("…")
    keys = [s.toString(QKeySequence.SequenceFormat.NativeText) for s in action.shortcuts()]
    tip = f"<b>{name}</b>"
    if keys:
        tip += f"&nbsp;&nbsp;<span>{html.escape(keys[0])}</span>"
    help_text = HELP.get(action_id(action))
    if help_text:
        tip += f"<br>{html.escape(help_text)}"
    return tip


def refresh_tooltips(actions: Iterable[QAction]) -> None:
    for action in actions:
        action.setToolTip(tooltip(action))
