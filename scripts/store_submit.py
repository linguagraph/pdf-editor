"""Finish the Store submission that ``msstore publish --noCommit`` drafted (#110).

Puts the listing from the repo on the draft, then commits it. The uploaded package is processed
only by the commit, so the listing has to come first, and a failed update stops before it: old
text can't go out with a new package. Uses the Microsoft Store submission API:
https://learn.microsoft.com/windows/uwp/monetize/create-and-manage-submissions-using-windows-store-services
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from store_listing import Listing, ListingError, load

API = "https://manage.devcenter.microsoft.com/v1.0/my"
LANGUAGE = "en-us"
TRIES = 4
RETRY_SECONDS = 15.0
POLL_SECONDS = 15.0
POLL_LIMIT_SECONDS = 1800.0
# Past these, Microsoft has the submission and the workflow's part is done.
ACCEPTED = {"PreProcessing", "Certification", "Release", "PendingPublication", "Publishing"}
ACCEPTED |= {"Published"}
FAILED = {"CommitFailed", "PreProcessingFailed", "CertificationFailed", "ReleaseFailed"}
FAILED |= {"PublishFailed", "Canceled"}

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class SubmitError(Exception):
    pass


def http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class Store:
    def __init__(
        self,
        app_id: str,
        token: str,
        transport: Transport = http,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.app = f"{API}/applications/{urllib.parse.quote(app_id)}"
        self.token = token
        self.transport = transport
        self.sleep = sleep

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        # The API can fail for a moment. A commit isn't repeated: the first may have gone through.
        tries = 1 if path.endswith("/commit") else TRIES
        for attempt in range(tries):
            status, reply = self.transport(method, f"{self.app}{path}", headers, data)
            if status < 400:
                return json.loads(reply) if reply else {}
            if (status < 500 and status != 429) or attempt == tries - 1:
                break
            print(f"{method} {path or '/'}: HTTP {status}, trying again")
            self.sleep(RETRY_SECONDS)
        detail = reply.decode("utf-8", "replace")[:800]
        raise SubmitError(f"{method} {path or '/'} failed: HTTP {status}: {detail}")

    def draft_id(self) -> str:
        app = self.call("GET", "")
        pending = app.get("pendingApplicationSubmission") or {}
        if not pending.get("id"):
            raise SubmitError("the app has no draft submission: did msstore publish run?")
        return str(pending["id"])

    def update_listing(self, submission_id: str, listing: Listing) -> None:
        path = f"/submissions/{submission_id}"
        submission = self.call("GET", path)
        status = submission.get("status")
        if status != "PendingCommit":
            raise SubmitError(f"the submission is “{status}”, not a draft waiting to be committed")
        apply_listing(submission, listing)
        self.call("PUT", path, submission)

    def commit(self, submission_id: str) -> str:
        """Commit, and follow it until Microsoft has accepted the submission or refused it."""
        path = f"/submissions/{submission_id}"
        self.call("POST", f"{path}/commit")
        waited = 0.0
        while waited <= POLL_LIMIT_SECONDS:
            reply = self.call("GET", f"{path}/status")
            status = str(reply.get("status"))
            print(f"submission status: {status}")
            if status in ACCEPTED:
                return status
            if status in FAILED:
                details = json.dumps(reply.get("statusDetails"), ensure_ascii=False)[:1500]
                raise SubmitError(f"the submission ended as {status}: {details}")
            self.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        raise SubmitError("the commit hasn't finished. Check the submission in Partner Center.")


def apply_listing(submission: dict[str, Any], listing: Listing) -> None:
    """Our text on the draft. Everything else in it, images and packages included, stays."""
    listings = submission.get("listings") or {}
    language = next((key for key in listings if key.casefold() == LANGUAGE), None)
    if language is None:
        raise SubmitError(f"the submission has no {LANGUAGE} listing: {sorted(listings)}")
    base = listings[language].setdefault("baseListing", {})
    base["shortDescription"] = listing.short_description
    base["description"] = listing.description
    base["releaseNotes"] = listing.release_notes
    base["features"] = list(listing.features)
    base["keywords"] = list(listing.keywords)
    submission["notesForCertification"] = listing.notes_for_certification


def get_token(tenant: str, client_id: str, secret: str, transport: Transport = http) -> str:
    form = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "resource": "https://manage.devcenter.microsoft.com",
        }
    ).encode("ascii")
    url = f"https://login.microsoftonline.com/{urllib.parse.quote(tenant)}/oauth2/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    status, reply = transport("POST", url, headers, form)
    if status >= 400:
        # Its reply names the problem and doesn't repeat the secret.
        raise SubmitError(
            f"signing in failed: HTTP {status}: {reply.decode('utf-8', 'replace')[:400]}"
        )
    return str(json.loads(reply)["access_token"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-commit", action="store_true", help="update the draft's listing and stop there"
    )
    args = parser.parse_args()
    try:
        listing = load()
        token = get_token(
            os.environ["MSSTORE_TENANT_ID"],
            os.environ["MSSTORE_CLIENT_ID"],
            os.environ["MSSTORE_CLIENT_SECRET"],
        )
        store = Store(os.environ["MSSTORE_PRODUCT_ID"], token)
        submission_id = store.draft_id()
        store.update_listing(submission_id, listing)
        print("the draft has the listing from the repo")
        if args.no_commit:
            print("not submitted, as asked: review and submit the draft in Partner Center")
            return 0
        print(f"submitted to Microsoft: {store.commit(submission_id)}")
    except (ListingError, SubmitError, KeyError) as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
