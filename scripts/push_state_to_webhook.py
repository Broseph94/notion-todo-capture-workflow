#!/usr/bin/env python3
"""Push local pending approval state to remote webhook service."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any
from urllib import error, request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync pending suggestions state to remote approval webhook.")
    parser.add_argument(
        "--webhook-base-url",
        required=True,
        help="Base URL, e.g. https://your-service.onrender.com",
    )
    parser.add_argument(
        "--sync-token",
        default=os.getenv("STATE_SYNC_TOKEN", "").strip(),
        help="Bearer token for /internal/state/sync (or STATE_SYNC_TOKEN env).",
    )
    parser.add_argument(
        "--pending-suggestions-file",
        default=os.getenv(
            "APPROVAL_PENDING_FILE",
            "/Users/jacobskogli/Desktop/work-ops-automations/notion-todo-capture/state/pending_suggestions.json",
        ),
    )
    parser.add_argument(
        "--cursor-file",
        default=os.getenv(
            "APPROVAL_CURSOR_FILE",
            "/Users/jacobskogli/Desktop/work-ops-automations/notion-todo-capture/state/approval_cursor.json",
        ),
    )
    parser.add_argument(
        "--include-cursor",
        action="store_true",
        help="Include local approval cursor in sync payload.",
    )
    return parser.parse_args()


def load_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"Expected JSON object in {path}")
    return data


def main() -> None:
    args = parse_args()
    token = str(args.sync_token or "").strip()
    if not token:
        raise SystemExit("Missing sync token. Pass --sync-token or set STATE_SYNC_TOKEN.")

    base = str(args.webhook_base_url).rstrip("/")
    endpoint = f"{base}/internal/state/sync"

    payload: dict[str, Any] = {
        "pending_suggestions": load_json(args.pending_suggestions_file),
    }
    if args.include_cursor and os.path.exists(args.cursor_file):
        payload["approval_cursor"] = load_json(args.cursor_file)

    req = request.Request(
        url=endpoint,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"State sync failed ({exc.code}): {details}") from exc
    except error.URLError as exc:
        raise SystemExit(f"State sync network error: {exc.reason}") from exc

    print(body)


if __name__ == "__main__":
    main()
