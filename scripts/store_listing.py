"""The Store listing in packaging/store/listing-en.md, as the data the Store API takes (#110).

Stdlib only: the release checks it before anything is installed or built. Run it to check the
file: ``python scripts/store_listing.py`` (``--tag vX.Y.Z`` also checks What's New has changed).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LISTING = ROOT / "packaging" / "store" / "listing-en.md"
LISTING_PATH_IN_GIT = "packaging/store/listing-en.md"
# In a tag's message: this release has no user-facing changes, so What's New stays as it is.
SAME_NOTES_MARKER = "[same-whats-new]"
# A hard-wrapped line is this long or longer, so a shorter one really ends its line (a heading).
WRAPPED_LINE_LENGTH = 70
MAX_SHORT_DESCRIPTION = 1000
MAX_DESCRIPTION = 10000
MAX_RELEASE_NOTES = 1500
MAX_FEATURES = 20
MAX_FEATURE = 200
MAX_KEYWORDS = 7


class ListingError(Exception):
    """The listing can't be sent as it is."""


@dataclass(frozen=True)
class Listing:
    short_description: str
    description: str
    release_notes: str
    features: tuple[str, ...]
    keywords: tuple[str, ...]
    notes_for_certification: str


def sections(text: str) -> dict[str, str]:
    """Each ``## Heading [note]`` with its body, by heading without the note."""
    found: dict[str, str] = {}
    parts = re.split(r"^## +(.+?) *$", text.replace("\r\n", "\n"), flags=re.M)
    for heading, body in zip(parts[1::2], parts[2::2], strict=True):
        found[re.sub(r"\s*\[.*\]$", "", heading)] = body.strip()
    return found


def unwrap(text: str) -> str:
    """The file is wrapped for reading, but the Store shows every line break. Join wrapped lines,
    and keep paragraphs, bullets and short lines such as headings."""
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        lines: list[str] = []
        for raw in paragraph.split("\n"):
            line = raw.strip()
            continues = lines and not line.startswith("•") and len(lines[-1]) >= WRAPPED_LINE_LENGTH
            continues = continues or (lines and raw.startswith(" ") and not line.startswith("•"))
            if continues:
                lines[-1] = f"{lines[-1]} {line}"
            else:
                lines.append(line)
        paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs)


def _required(found: dict[str, str], heading: str) -> str:
    body = found.get(heading, "")
    if not body:
        raise ListingError(f"the listing has no text under “{heading}”")
    return body


def _limit(name: str, value: str, maximum: int) -> None:
    if len(value) > maximum:
        raise ListingError(
            f"{name} is {len(value)} characters, over the Store's limit of {maximum}"
        )


def parse(text: str) -> Listing:
    """The listing, or :class:`ListingError` naming what's missing or over a Store limit."""
    found = sections(text)
    features = tuple(
        unwrap(item)
        for item in re.split(r"^\d+\. +", _required(found, "Product features"), flags=re.M)
        if item.strip()
    )
    keywords = tuple(
        word.strip() for word in _required(found, "Search terms").split("·") if word.strip()
    )
    listing = Listing(
        short_description=unwrap(_required(found, "Short description")),
        description=unwrap(_required(found, "Description")),
        release_notes=unwrap(_required(found, "What's new in this version")),
        features=features,
        keywords=keywords,
        notes_for_certification=unwrap(_required(found, "Notes for certification")),
    )
    _limit("the short description", listing.short_description, MAX_SHORT_DESCRIPTION)
    _limit("the description", listing.description, MAX_DESCRIPTION)
    _limit("What's New", listing.release_notes, MAX_RELEASE_NOTES)
    if len(features) > MAX_FEATURES:
        raise ListingError(f"{len(features)} product features, over the limit of {MAX_FEATURES}")
    for feature in features:
        _limit(f"the feature “{feature[:40]}…”", feature, MAX_FEATURE)
    if len(keywords) > MAX_KEYWORDS:
        raise ListingError(f"{len(keywords)} search terms, over the limit of {MAX_KEYWORDS}")
    return listing


def load() -> Listing:
    return parse(LISTING.read_text(encoding="utf-8"))


def _git(*args: str) -> str | None:
    done = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", cwd=ROOT, check=False
    )
    return done.stdout if done.returncode == 0 else None


def check_notes_changed(new: Listing, previous: str | None, tag_message: str) -> None:
    """What's New must describe this release. ``previous`` is the last tag's listing, if any."""
    if previous is None or SAME_NOTES_MARKER in tag_message:
        return
    if parse(previous).release_notes == new.release_notes:
        raise ListingError(
            "What's New is the same as in the last release. Update it in "
            f"{LISTING_PATH_IN_GIT}, or put {SAME_NOTES_MARKER} in the tag's message if this "
            "release has no user-facing changes."
        )


def check_release(tag: str) -> None:
    listing = load()
    last_tag = (_git("describe", "--tags", "--abbrev=0", f"{tag}^") or "").strip()
    previous = _git("show", f"{last_tag}:{LISTING_PATH_IN_GIT}") if last_tag else None
    check_notes_changed(listing, previous, _git("tag", "-l", "--format=%(contents)", tag) or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="the release's tag: also check What's New has changed")
    args = parser.parse_args()
    try:
        if args.tag:
            check_release(args.tag)
        else:
            load()
    except ListingError as exc:
        print(f"::error::{exc}")
        return 1
    print("the Store listing is ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
