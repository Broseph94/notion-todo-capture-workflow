#!/usr/bin/env python3
"""Always-on Slack Events webhook for realtime Notion approval handling."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import hmac
import json
import os
import queue
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, parse, request

from approval_listener import process_approval_event


def _log(message: str) -> None:
    print(message, flush=True)


@dataclasses.dataclass
class ServiceConfig:
    slack_bot_token: str
    slack_signing_secret: str
    pending_suggestions_file: str
    cursor_file: str
    confirmation_message_file: str
    database_id: str | None
    database_name: str
    notion_token: str | None
    title_property: str
    status_property: str
    due_property: str
    source_property: str
    client_project_property: str
    type_property: str
    priority_property: str
    status_value: str
    skip_existing: bool
    dry_run: bool
    host: str
    port: int
    max_age_seconds: int = 60 * 5
    state_sync_token: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slack Events webhook for realtime approval replies.")
    parser.add_argument("--host", default=os.getenv("APPROVAL_WEBHOOK_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("APPROVAL_WEBHOOK_PORT", "8787")))
    parser.add_argument(
        "--signature-max-age-seconds",
        type=int,
        default=int(os.getenv("SLACK_SIGNATURE_MAX_AGE_SECONDS", "300")),
        help="Reject signed Slack requests older than this age in seconds (default: 300).",
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
        "--confirmation-message-file",
        default=os.getenv(
            "APPROVAL_CONFIRM_FILE",
            "/Users/jacobskogli/Desktop/work-ops-automations/notion-todo-capture/state/approval_confirmation_live.txt",
        ),
    )
    parser.add_argument("--database-id", default=os.getenv("NOTION_DATABASE_ID"))
    parser.add_argument("--database-name", default=os.getenv("NOTION_DATABASE_NAME", "Oppgaver"))
    parser.add_argument("--token", default=os.getenv("NOTION_API_TOKEN"))
    parser.add_argument("--title-property", default=os.getenv("NOTION_TITLE_PROPERTY", "Oppgave"))
    parser.add_argument("--status-property", default=os.getenv("NOTION_STATUS_PROPERTY", "Status"))
    parser.add_argument("--due-property", default=os.getenv("NOTION_DUE_PROPERTY", "Frist"))
    parser.add_argument("--source-property", default=os.getenv("NOTION_SOURCE_PROPERTY", "Source"))
    parser.add_argument(
        "--client-project-property",
        default=os.getenv("NOTION_CLIENT_PROJECT_PROPERTY", "Kunde/Prosjekt"),
    )
    parser.add_argument("--type-property", default=os.getenv("NOTION_TYPE_PROPERTY", "🏷️ Type"))
    parser.add_argument("--priority-property", default=os.getenv("NOTION_PRIORITY_PROPERTY", "🔥 Prioritet"))
    parser.add_argument("--status-value", default=os.getenv("NOTION_STATUS_VALUE", "Ikke startet"))
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--state-sync-token",
        default=os.getenv("STATE_SYNC_TOKEN", "").strip(),
        help="Optional bearer token enabling /internal/state/sync endpoint.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> ServiceConfig:
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    signing_secret = os.getenv("SLACK_SIGNING_SECRET", "").strip()
    if not bot_token:
        raise SystemExit("Missing SLACK_BOT_TOKEN.")
    if not signing_secret:
        raise SystemExit("Missing SLACK_SIGNING_SECRET.")
    return ServiceConfig(
        slack_bot_token=bot_token,
        slack_signing_secret=signing_secret,
        pending_suggestions_file=args.pending_suggestions_file,
        cursor_file=args.cursor_file,
        confirmation_message_file=args.confirmation_message_file,
        database_id=args.database_id,
        database_name=args.database_name,
        notion_token=args.token,
        title_property=args.title_property,
        status_property=args.status_property,
        due_property=args.due_property,
        source_property=args.source_property,
        client_project_property=args.client_project_property,
        type_property=args.type_property,
        priority_property=args.priority_property,
        status_value=args.status_value,
        skip_existing=bool(args.skip_existing),
        dry_run=bool(args.dry_run),
        host=args.host,
        port=int(args.port),
        max_age_seconds=int(args.signature_max_age_seconds),
        state_sync_token=str(args.state_sync_token).strip() or None,
    )


def verify_slack_signature(
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    max_age_seconds: int = 60 * 5,
) -> bool:
    if not timestamp or not signature:
        return False
    try:
        ts_int = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts_int) > max_age_seconds:
        return False
    base = f"v0:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def slack_api_json(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    req = request.Request(
        url=f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Slack API {method} failed ({exc.code}): {details}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Slack API network error for {method}: {exc.reason}") from exc

    if not parsed.get("ok"):
        details = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        raise RuntimeError(f"Slack API {method} returned error payload: {details}")
    return parsed


def slack_api_form(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    encoded = parse.urlencode({key: str(value) for key, value in payload.items()}).encode("utf-8")
    req = request.Request(
        url=f"https://slack.com/api/{method}",
        data=encoded,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Slack API {method} failed ({exc.code}): {details}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Slack API network error for {method}: {exc.reason}") from exc

    if not parsed.get("ok"):
        details = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        raise RuntimeError(f"Slack API {method} returned error payload: {details}")
    return parsed


def fetch_thread_root_text(token: str, channel_id: str, thread_ts: str) -> str | None:
    payload = {
        "channel": channel_id,
        "ts": thread_ts,
    }
    data = slack_api_form(token, "conversations.replies", payload)
    messages = data.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return None
    first = messages[0]
    if not isinstance(first, dict):
        return None
    text = first.get("text")
    return str(text).strip() if text else None


def post_thread_reply(token: str, channel_id: str, thread_ts: str, text: str) -> None:
    payload = {
        "channel": channel_id,
        "thread_ts": thread_ts,
        "text": text,
    }
    slack_api_json(token, "chat.postMessage", payload)


def write_json_file(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)


def apply_state_sync(config: ServiceConfig, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object.")
    pending = payload.get("pending_suggestions")
    if not isinstance(pending, dict):
        raise ValueError("Missing 'pending_suggestions' object in payload.")

    write_json_file(config.pending_suggestions_file, pending)
    result: dict[str, Any] = {"pending_synced": True, "cursor_synced": False}

    cursor = payload.get("approval_cursor")
    if isinstance(cursor, dict):
        write_json_file(config.cursor_file, cursor)
        result["cursor_synced"] = True

    return result


def should_process_event(event: dict[str, Any]) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("type") != "message":
        return False
    if event.get("subtype"):
        return False
    text = str(event.get("text", "")).strip()
    if not text:
        return False
    thread_ts = str(event.get("thread_ts", "")).strip()
    ts = str(event.get("ts", "")).strip()
    if not thread_ts or not ts or thread_ts == ts:
        return False
    return True


def build_listener_args(config: ServiceConfig) -> argparse.Namespace:
    return argparse.Namespace(
        pending_suggestions_file=config.pending_suggestions_file,
        cursor_file=config.cursor_file,
        run_id=None,
        database_id=config.database_id,
        database_name=config.database_name,
        token=config.notion_token,
        title_property=config.title_property,
        status_property=config.status_property,
        due_property=config.due_property,
        source_property=config.source_property,
        client_project_property=config.client_project_property,
        type_property=config.type_property,
        priority_property=config.priority_property,
        status_value=config.status_value,
        skip_existing=config.skip_existing,
        dry_run=config.dry_run,
        confirmation_message_file=config.confirmation_message_file,
        result_file=None,
        verbose=False,
    )


def build_ignored_feedback(reason: str) -> str | None:
    normalized = (reason or "").strip().casefold()
    if normalized == "no pending suggestions matched decision":
        return "Denne tråden er allerede behandlet, så jeg legger ikke til noe nytt i Notion-databasen."
    if normalized == "thread reply did not match any pending approval prompt":
        return "Jeg fant ingen aktiv pending oppgave i denne tråden ennå."
    return None


class ApprovalEventWorker(threading.Thread):
    def __init__(self, config: ServiceConfig, verbose: bool = False) -> None:
        super().__init__(daemon=True)
        self.config = config
        self.verbose = verbose
        self.queue: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        self._stop = threading.Event()

    def submit(self, payload: dict[str, Any]) -> None:
        self.queue.put(payload)

    def stop(self) -> None:
        self._stop.set()
        self.queue.put(None)

    def run(self) -> None:
        while not self._stop.is_set():
            item = self.queue.get()
            if item is None:
                self.queue.task_done()
                break
            try:
                self.handle_event(item)
            except Exception as exc:  # noqa: BLE001
                _log(f"[approval-webhook] worker error: {exc}")
            finally:
                self.queue.task_done()

    def handle_event(self, body: dict[str, Any]) -> None:
        event = body.get("event")
        if not isinstance(event, dict):
            if self.verbose:
                _log("[approval-webhook] ignored payload without event object")
            return
        if not should_process_event(event):
            if self.verbose:
                event_type = str(event.get("type", ""))
                subtype = str(event.get("subtype", ""))
                ts = str(event.get("ts", ""))
                thread_ts = str(event.get("thread_ts", ""))
                _log(
                    "[approval-webhook] ignored event before processing: "
                    f"type={event_type} subtype={subtype} ts={ts} thread_ts={thread_ts}"
                )
            return

        channel = str(event.get("channel", "")).strip()
        ts = str(event.get("ts", "")).strip()
        thread_ts = str(event.get("thread_ts", "")).strip()
        user = str(event.get("user", "")).strip() or None
        text = str(event.get("text", "")).strip()
        event_id = str(body.get("event_id") or f"slack:{channel}:{ts}")

        if not channel or not thread_ts or not ts or not text:
            return

        root_text: str | None = None
        try:
            root_text = fetch_thread_root_text(self.config.slack_bot_token, channel, thread_ts)
        except Exception as exc:  # noqa: BLE001
            # Do not block decision processing if thread-root lookup fails.
            # Explicit run-id replies can still be processed without root text.
            _log(f"[approval-webhook] root fetch failed channel={channel} thread_ts={thread_ts}: {exc}")

        listener_payload = {
            "event_id": event_id,
            "event": {
                "text": text,
                "channel": channel,
                "ts": ts,
                "thread_ts": thread_ts,
                "user": user,
                "thread_root_text": root_text,
            },
        }
        args = build_listener_args(self.config)
        try:
            result = process_approval_event(args, listener_payload)
        except Exception as exc:  # noqa: BLE001
            details = str(exc)
            _log(f"[approval-webhook] worker error: {details}")
            if "Notion API error 404" in details and "Could not find database" in details:
                post_thread_reply(
                    token=self.config.slack_bot_token,
                    channel_id=channel,
                    thread_ts=thread_ts,
                    text=(
                        "Jeg får ikke tilgang til Notion-databasen enda. "
                        "Del databasen med integrasjonen som brukes i Render, og prøv igjen."
                    ),
                )
            return

        if self.verbose:
            _log(f"[approval-webhook] processed {event_id}: {result.status} ({result.reason})")

        if result.status == "processed" and result.confirmation_message:
            post_thread_reply(
                token=self.config.slack_bot_token,
                channel_id=channel,
                thread_ts=thread_ts,
                text=result.confirmation_message,
            )
            return

        if result.status == "ignored":
            feedback = build_ignored_feedback(result.reason)
            if feedback:
                post_thread_reply(
                    token=self.config.slack_bot_token,
                    channel_id=channel,
                    thread_ts=thread_ts,
                    text=feedback,
                )


def make_handler(worker: ApprovalEventWorker, config: ServiceConfig):
    class SlackWebhookHandler(BaseHTTPRequestHandler):
        server_version = "SlackApprovalWebhook/1.0"

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._write_json(HTTPStatus.OK, {"ok": True})
            else:
                self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/internal/state/sync":
                if not config.state_sync_token:
                    self._write_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "state_sync_disabled"})
                    return

                auth = str(self.headers.get("Authorization", "")).strip()
                expected = f"Bearer {config.state_sync_token}"
                if auth != expected:
                    self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "invalid_auth"})
                    return

                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                try:
                    sync_payload = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
                    return

                try:
                    result = apply_state_sync(config, sync_payload)
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._write_json(HTTPStatus.OK, {"ok": True, **result})
                return

            if self.path != "/slack/events":
                self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
                return

            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if worker.verbose:
                _log(f"[approval-webhook] incoming request path=/slack/events bytes={length}")

            timestamp = self.headers.get("X-Slack-Request-Timestamp")
            signature = self.headers.get("X-Slack-Signature")
            if not verify_slack_signature(
                config.slack_signing_secret,
                timestamp,
                signature,
                body,
                max_age_seconds=config.max_age_seconds,
            ):
                if worker.verbose:
                    _log("[approval-webhook] rejected request: invalid signature")
                self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "invalid_signature"})
                return

            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                if worker.verbose:
                    _log("[approval-webhook] rejected request: invalid json")
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
                return

            if payload.get("type") == "url_verification":
                challenge = payload.get("challenge")
                if worker.verbose:
                    _log("[approval-webhook] handled url_verification challenge")
                self._write_json(HTTPStatus.OK, {"challenge": challenge})
                return

            worker.submit(payload)
            if worker.verbose:
                ptype = str(payload.get("type", ""))
                event = payload.get("event", {})
                etype = str(event.get("type", "")) if isinstance(event, dict) else ""
                _log(f"[approval-webhook] queued payload type={ptype} event.type={etype}")
            self._write_json(HTTPStatus.OK, {"ok": True})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return SlackWebhookHandler


def main() -> None:
    args = parse_args()
    config = build_config(args)
    worker = ApprovalEventWorker(config=config, verbose=bool(args.verbose))
    worker.start()

    handler = make_handler(worker, config)
    server = ThreadingHTTPServer((config.host, config.port), handler)

    def shutdown_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        worker.stop()
        server.shutdown()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    _log(f"[approval-webhook] listening on {config.host}:{config.port}")
    try:
        server.serve_forever()
    finally:
        worker.stop()
        server.server_close()


if __name__ == "__main__":
    main()
