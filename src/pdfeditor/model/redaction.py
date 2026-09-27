"""Redaction and sanitization options."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ImageRedaction(Enum):
    NONE = "none"  # leave images alone
    PIXELS = "pixels"  # blank only the covered pixels
    REMOVE = "remove"  # remove any image that touches a mark


class GraphicsRedaction(Enum):
    NONE = "none"
    COVERED = "covered"  # remove vector graphics fully inside a mark
    TOUCHED = "touched"  # remove vector graphics that touch a mark


@dataclass(frozen=True, slots=True)
class RedactOptions:
    images: ImageRedaction = ImageRedaction.PIXELS
    graphics: GraphicsRedaction = GraphicsRedaction.COVERED
    text: bool = True


@dataclass(frozen=True, slots=True)
class SanitizeOptions:
    metadata: bool = True  # Info dictionary
    xmp: bool = True
    javascript: bool = True
    attachments: bool = True  # embedded files and file-attachment annotations
    links: bool = True
    hidden_text: bool = True  # invisible text (render mode 3), e.g. old OCR layers
    comments: bool = True  # every annotation except form fields
    form_data: bool = True  # reset form field values
    thumbnails: bool = True
