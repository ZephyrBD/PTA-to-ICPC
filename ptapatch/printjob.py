"""DOMjudge-style print job: build + POST (pure logic, no I/O here).

PTA Patch plays DOMjudge for the contest's print problem: a team's
submission to that problem is a print request. The receiving end (xcpctools
/ Hydro) expects the same multipart shape the DOMjudge print script sends:

    curl --form "file=@$filename" \
         --form-string "filename=.." --form-string "lang=.." \
         --form-string "tname=.." --form-string "team=.." \
         --form-string "location=.." [--form-string "group=.."] "$PRINT_SERVER"

``build_job`` is a pure function producing httpx ``files``/``data``
kwargs; ``send_job`` does the POST (2xx → True, anything else → False
with a warning). Testable via ``httpx.MockTransport`` — never hits the
network in the test suite.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

PRINT_TIMEOUT = 5.0


def build_job(*, source: str, filename: str, lang: str, tname: str,
              team: str, location: str | None = None,
              group: str | None = None) -> dict:
    """Pure: assemble the multipart kwargs for one print request.

    Returns ``{"files": {"file": (filename, source_bytes, "text/plain")},
    "data": {...}}``. The source is utf-8 encoded. ``location``/``group``
    are omitted from ``data`` when None or empty — the script only adds
    those --form-string flags conditionally.
    """
    data: dict[str, str] = {
        "filename": filename,
        "lang": lang,
        "tname": tname,
        "team": team,
    }
    if location:
        data["location"] = location
    if group:
        data["group"] = group
    return {
        "files": {"file": (filename, (source or "").encode("utf-8"),
                           "text/plain")},
        "data": data,
    }


async def send_job(http_client, print_server: str, job: dict,
                   timeout: float = PRINT_TIMEOUT) -> bool:
    """POST one multipart print job; 2xx → True, error/non-2xx → False.

    Transport exceptions and non-2xx responses are swallowed with a
    warning (the poller decides whether to retry) — never raise.
    """
    try:
        resp = await http_client.post(print_server, files=job["files"],
                                      data=job["data"], timeout=timeout)
    except Exception as e:  # noqa: BLE001 - any transport failure is a retry
        log.warning("print job post failed: %s", e)
        return False
    if not 200 <= resp.status_code < 300:
        log.warning("print server rejected job: HTTP %d", resp.status_code)
        return False
    return True
