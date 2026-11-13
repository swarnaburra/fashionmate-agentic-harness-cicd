#!/usr/bin/env python3
"""
Create or update one PR comment identified by a hidden marker.

Usage: upsert_pr_comment.py <pr-number> <marker> <body-file>

Each CI comment (advisory review, audit summary) uses its own marker, so a
rerun updates its own comment instead of adding a new one, and the two never
overwrite each other. Uses the workflow's automatic token (GH_TOKEN); the
token is only sent as a request header and is never printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

MAX_BODY = 60_000  # GitHub's comment limit is 65,536 characters


def api(method: str, url: str, data: dict | None = None):
    request = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(data).encode("utf-8") if data is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def main() -> int:
    pr, marker, body_file = sys.argv[1], sys.argv[2], sys.argv[3]
    repo = os.environ["GITHUB_REPOSITORY"]
    base = f"https://api.github.com/repos/{repo}"

    body = marker + "\n\n" + Path(body_file).read_text(encoding="utf-8")
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n[truncated]\n"

    existing = None
    page = 1
    while True:
        comments = api("GET", f"{base}/issues/{pr}/comments?per_page=100&page={page}")
        if not comments:
            break
        for comment in comments:
            if (comment.get("body") or "").startswith(marker):
                existing = comment["id"]
        page += 1

    if existing:
        api("PATCH", f"{base}/issues/comments/{existing}", {"body": body})
        print(f"Updated comment {existing}")
    else:
        created = api("POST", f"{base}/issues/{pr}/comments", {"body": body})
        print(f"Created comment {created['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
