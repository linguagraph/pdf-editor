"""Sending the Store listing and submitting: no network, a fake API records every call."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import ci_changes
import store_listing
import store_submit
from store_listing import ListingError, parse, unwrap
from store_submit import Store, SubmitError

GOOD = """# Listing

Paste each section.

## Short description [up to 1,000 characters; shown first]

Short.

## Description [up to 10,000 characters]

Long text.

## What's new in this version [up to 1,500 characters]

• Something new.

## Product features [up to 20, each up to 200 characters]

1. First
2. Second

## Search terms [up to 7]

PDF editor · edit PDF

## Notes for certification [Submission options ▸ Notes for certification]

Test notes.
"""


def test_the_real_listing_is_valid() -> None:
    """What the next release will send: this fails the PR that breaks it."""
    listing = store_listing.load()
    assert "Digital signatures" in listing.release_notes
    assert listing.features[0] == "Edit text and images directly in your PDF"
    assert "PDF editor" in listing.keywords
    assert "runFullTrust" in listing.notes_for_certification


def test_the_real_listing_reads_right_once_unwrapped() -> None:
    listing = store_listing.load()
    # Wrapped sentences are joined, and sections and bullets keep their own lines.
    assert "It has no account, no telemetry and no ads, and your documents never leave" in (
        listing.description
    )
    assert "\nPrivate by design\nElanero works offline." in listing.description
    assert "\nEdit the document itself\n• Change text" in listing.description
    assert (
        "• Digital signatures: sign PDFs with a USB token, smart card or certificate file, with"
        in (listing.release_notes)
    )
    assert all(line.startswith("•") for line in listing.release_notes.splitlines())


def test_sections_for_pasting_by_hand_arent_sent() -> None:
    listing = store_listing.load()
    sent = " ".join((listing.description, listing.notes_for_certification))
    assert "Restricted capability" not in sent and "store-logo" not in sent


def test_unwrap_keeps_paragraphs() -> None:
    long = "x" * 80
    assert unwrap(f"{long}\nmore\n\nnext") == f"{long} more\n\nnext"
    assert unwrap("• one\n  continued\n• two") == "• one continued\n• two"
    assert unwrap("Heading\nText") == "Heading\nText"


@pytest.mark.parametrize(
    "heading",
    ["Short description", "Description", "What's new", "Product features", "Search terms", "Notes"],
)
def test_a_missing_section_stops_the_release(heading: str) -> None:
    text = GOOD
    start = text.index(f"## {heading}")
    end = text.find("## ", start + 3)
    text = text[:start] + (text[end:] if end != -1 else "")
    with pytest.raises(ListingError, match="no text under"):
        parse(text)


def test_an_empty_section_stops_the_release() -> None:
    with pytest.raises(ListingError, match="What's new"):
        parse(GOOD.replace("• Something new.", ""))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("• Something new.", "n" * 1501, "What's New"),
        ("Long text.", "d" * 10001, "the description"),
        ("Short.", "s" * 1001, "the short description"),
        ("1. First", "1. " + "f" * 201, "the feature"),
        ("1. First", "\n".join(f"{i}. f" for i in range(1, 22)), "product features"),
        ("PDF editor · edit PDF", " · ".join("abcdefgh"), "search terms"),
    ],
)
def test_store_limits_are_checked(old: str, new: str, message: str) -> None:
    with pytest.raises(ListingError, match=message):
        parse(GOOD.replace(old, new))


def test_stale_whats_new_stops_the_release() -> None:
    listing = parse(GOOD)
    with pytest.raises(ListingError, match="same as in the last release"):
        store_listing.check_notes_changed(listing, GOOD, "Elanero 0.3.0")
    store_listing.check_notes_changed(listing, GOOD.replace("new.", "old."), "Elanero 0.3.0")
    store_listing.check_notes_changed(listing, None, "the first tag")
    store_listing.check_notes_changed(listing, GOOD, "Elanero 0.3.0 [same-whats-new]")


def test_a_listing_change_is_checked_but_needs_no_install_tests() -> None:
    assert ci_changes.classify(["packaging/store/listing-en.md"]) == {
        "app": True,
        "packaging": False,
    }


# -- the API --------------------------------------------------------------------------------------
class FakeApi:
    def __init__(self, statuses: tuple[str, ...] = ("CommitStarted", "PreProcessing")) -> None:
        self.calls: list[tuple[str, str]] = []
        self.statuses = list(statuses)
        self.put: dict | None = None
        self.fail: dict[str, list[int]] = {}
        self.submission = {
            "id": "42",
            "status": "PendingCommit",
            "applicationPackages": [{"fileName": "new.msix", "fileStatus": "PendingUpload"}],
            "notesForCertification": "old",
            "listings": {
                "en-us": {
                    "baseListing": {
                        "title": "Elanero - PDF Editor",
                        "description": "old",
                        "images": [{"fileName": "shot.png"}],
                    }
                }
            },
        }

    def __call__(self, method, url, headers, body):
        path = url.rsplit("/applications/", 1)[-1].partition("/")[2]
        self.calls.append((method, path))
        assert headers["Authorization"] == "Bearer token"
        failures = self.fail.get(f"{method} {path}")
        if failures:
            return failures.pop(0), b'{"message": "nope"}'
        if method == "GET" and path == "":
            return 200, json.dumps({"pendingApplicationSubmission": {"id": "42"}}).encode()
        if method == "GET" and path == "submissions/42":
            return 200, json.dumps(self.submission).encode()
        if method == "PUT":
            self.put = json.loads(body)
            return 200, body
        if path.endswith("/commit"):
            return 202, b'{"status": "CommitStarted"}'
        if path.endswith("/status"):
            return 200, json.dumps(
                {"status": self.statuses.pop(0), "statusDetails": {"x": 1}}
            ).encode()
        raise AssertionError(f"unexpected {method} {path}")


def store(api: FakeApi) -> Store:
    return Store("9TEST", "token", api, sleep=lambda _s: None)


def test_the_listing_goes_on_the_draft_and_the_rest_stays() -> None:
    api = FakeApi()
    store(api).update_listing("42", parse(GOOD))
    assert api.put is not None
    base = api.put["listings"]["en-us"]["baseListing"]
    assert base["releaseNotes"] == "• Something new."
    assert base["description"] == "Long text."
    assert base["features"] == ["First", "Second"]
    assert base["keywords"] == ["PDF editor", "edit PDF"]
    assert api.put["notesForCertification"] == "Test notes."
    # Not ours to change: the package msstore uploaded, images, the title.
    assert api.put["applicationPackages"] == api.submission["applicationPackages"]
    assert base["images"] == [{"fileName": "shot.png"}]
    assert base["title"] == "Elanero - PDF Editor"


def test_the_listing_comes_before_the_commit(monkeypatch) -> None:
    api = FakeApi()
    s = store(api)
    s.update_listing(s.draft_id(), parse(GOOD))
    assert s.commit("42") == "PreProcessing"
    methods = [call for call in api.calls if call[0] != "GET"]
    assert methods == [("PUT", "submissions/42"), ("POST", "submissions/42/commit")]


def test_a_failed_listing_update_doesnt_commit(monkeypatch, capsys) -> None:
    api = FakeApi()
    api.fail["PUT submissions/42"] = [400]
    for name, value in {
        "MSSTORE_TENANT_ID": "t",
        "MSSTORE_CLIENT_ID": "c",
        "MSSTORE_CLIENT_SECRET": "very-secret",
        "MSSTORE_PRODUCT_ID": "9TEST",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["store_submit.py"])
    monkeypatch.setattr(store_submit, "get_token", lambda *_a: "token")
    monkeypatch.setattr(store_submit.Store, "__init__", lambda self, *_a: _init(self, api))
    assert store_submit.main() == 1
    assert not any(path.endswith("/commit") for _m, path in api.calls)
    out = capsys.readouterr().out
    assert "HTTP 400" in out and "very-secret" not in out


def _init(self: Store, api: FakeApi) -> None:
    # main() builds its own Store: this is that one, talking to the fake.
    self.app = "https://x/applications/9TEST"
    self.token = "token"
    self.transport = api
    self.sleep = lambda _s: None


def test_no_commit_stops_after_the_listing(monkeypatch, capsys) -> None:
    api = FakeApi()
    for name in ("MSSTORE_TENANT_ID", "MSSTORE_CLIENT_ID", "MSSTORE_CLIENT_SECRET"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("MSSTORE_PRODUCT_ID", "9TEST")
    monkeypatch.setattr(sys, "argv", ["store_submit.py", "--no-commit"])
    monkeypatch.setattr(store_submit, "get_token", lambda *_a: "token")
    monkeypatch.setattr(store_submit.Store, "__init__", lambda self, *_a: _init(self, api))
    assert store_submit.main() == 0
    assert api.put is not None
    assert not any(path.endswith("/commit") for _m, path in api.calls)


def test_a_draft_that_isnt_waiting_for_commit_is_refused() -> None:
    api = FakeApi()
    api.submission["status"] = "Certification"
    with pytest.raises(SubmitError, match="Certification"):
        store(api).update_listing("42", parse(GOOD))
    assert api.put is None


def test_no_draft_is_a_clear_error() -> None:
    def transport(_m, _u, _h, _b):
        return 200, b"{}"

    with pytest.raises(SubmitError, match="no draft"):
        Store("9TEST", "token", transport).draft_id()


def test_a_listing_in_another_language_is_refused() -> None:
    api = FakeApi()
    api.submission["listings"] = {"bg-bg": {"baseListing": {}}}
    with pytest.raises(SubmitError, match="en-us"):
        store(api).update_listing("42", parse(GOOD))


def test_temporary_failures_are_retried() -> None:
    api = FakeApi()
    api.fail["GET "] = [500, 429]
    assert store(api).draft_id() == "42"
    assert api.calls.count(("GET", "")) == 3


def test_a_refused_request_isnt_retried() -> None:
    api = FakeApi()
    api.fail["GET "] = [403]
    with pytest.raises(SubmitError, match="HTTP 403"):
        store(api).draft_id()
    assert len(api.calls) == 1


def test_a_commit_is_never_repeated() -> None:
    """The first may have gone through even if the reply says otherwise."""
    api = FakeApi()
    api.fail["POST submissions/42/commit"] = [500, 500]
    with pytest.raises(SubmitError, match="HTTP 500"):
        store(api).commit("42")
    assert api.calls.count(("POST", "submissions/42/commit")) == 1


def test_a_failed_commit_reports_why() -> None:
    api = FakeApi(("CommitStarted", "CommitFailed"))
    with pytest.raises(SubmitError, match="CommitFailed"):
        store(api).commit("42")


def test_a_commit_that_never_finishes_stops_waiting(monkeypatch) -> None:
    monkeypatch.setattr(store_submit, "POLL_LIMIT_SECONDS", 30.0)
    api = FakeApi(("CommitStarted",) * 10)
    with pytest.raises(SubmitError, match="hasn't finished"):
        store(api).commit("42")


def test_signing_in_failure_doesnt_print_the_secret() -> None:
    def transport(_m, _u, _h, body):
        assert b"very-secret" in body
        return 401, b'{"error": "invalid_client"}'

    with pytest.raises(SubmitError) as caught:
        store_submit.get_token("tenant", "client", "very-secret", transport)
    assert "very-secret" not in str(caught.value) and "invalid_client" in str(caught.value)
