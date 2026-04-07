#!/usr/bin/env python3
"""Unit tests for slack_approval_webhook.py."""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import tempfile
import unittest
from unittest import mock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from approval_listener import ListenerResult
from slack_approval_webhook import (
    ApprovalEventWorker,
    ServiceConfig,
    apply_state_sync,
    fetch_thread_root_text,
    should_process_event,
    verify_slack_signature,
)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    base = f"v0:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


class SlackApprovalWebhookTests(unittest.TestCase):
    def test_verify_slack_signature_valid(self) -> None:
        secret = "signing-secret"
        ts = "1775572800"
        body = b'{"type":"event_callback"}'
        signature = sign(secret, ts, body)
        with mock.patch("slack_approval_webhook.time.time", return_value=1775572810):
            ok = verify_slack_signature(secret, ts, signature, body)
        self.assertTrue(ok)

    def test_verify_slack_signature_rejects_old_timestamp(self) -> None:
        secret = "signing-secret"
        ts = "1775572800"
        body = b'{"type":"event_callback"}'
        signature = sign(secret, ts, body)
        with mock.patch("slack_approval_webhook.time.time", return_value=1775573801):
            ok = verify_slack_signature(secret, ts, signature, body, max_age_seconds=300)
        self.assertFalse(ok)

    def test_should_process_event_requires_thread_reply(self) -> None:
        self.assertTrue(
            should_process_event(
                {
                    "type": "message",
                    "text": "Ja, legg til denne",
                    "ts": "1.200",
                    "thread_ts": "1.100",
                }
            )
        )
        self.assertFalse(
            should_process_event(
                {
                    "type": "message",
                    "text": "Ja",
                    "ts": "1.100",
                    "thread_ts": "1.100",
                }
            )
        )
        self.assertFalse(
            should_process_event(
                {
                    "type": "message",
                    "subtype": "bot_message",
                    "text": "Ja",
                    "ts": "1.200",
                    "thread_ts": "1.100",
                }
            )
        )

    def test_fetch_thread_root_text(self) -> None:
        with mock.patch(
            "slack_approval_webhook.slack_api_json",
            return_value={"ok": True, "messages": [{"text": "*Skal jeg legge til disse i Notion?*"}]},
        ) as mocked:
            text = fetch_thread_root_text("xoxb-1", "D123", "1775572800.000100")

        mocked.assert_called_once()
        self.assertEqual(text, "*Skal jeg legge til disse i Notion?*")

    def test_worker_posts_confirmation_when_processed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = ServiceConfig(
                slack_bot_token="xoxb-test",
                slack_signing_secret="secret",
                pending_suggestions_file=os.path.join(tmp, "pending.json"),
                cursor_file=os.path.join(tmp, "cursor.json"),
                confirmation_message_file=os.path.join(tmp, "confirm.txt"),
                database_id=None,
                database_name="Oppgaver",
                notion_token=None,
                title_property="Oppgave",
                status_property="Status",
                due_property="Frist",
                source_property="Source",
                client_project_property="Kunde/Prosjekt",
                type_property="🏷️ Type",
                priority_property="🔥 Prioritet",
                status_value="Ikke startet",
                skip_existing=False,
                dry_run=True,
                host="127.0.0.1",
                port=8787,
            )
            worker = ApprovalEventWorker(config=config, verbose=False)
            payload = {
                "event_id": "Ev123",
                "event": {
                    "type": "message",
                    "text": "Ja, legg til denne",
                    "channel": "D123",
                    "ts": "1775572801.000100",
                    "thread_ts": "1775572800.000100",
                    "user": "U123",
                },
            }
            result = ListenerResult(
                status="processed",
                reason="decision applied",
                run_id="run-1",
                approved_count=1,
                rejected_count=0,
                created=1,
                updated=0,
                skipped=0,
                pending_remaining=0,
                confirmation_message="Ok, jeg legger denne til i Notion-databasen.",
            )
            with (
                mock.patch(
                    "slack_approval_webhook.fetch_thread_root_text",
                    return_value="*Skal jeg legge til disse i Notion?*",
                ),
                mock.patch("slack_approval_webhook.process_approval_event", return_value=result),
                mock.patch("slack_approval_webhook.post_thread_reply") as post_reply,
            ):
                worker.handle_event(payload)

            post_reply.assert_called_once_with(
                token="xoxb-test",
                channel_id="D123",
                thread_ts="1775572800.000100",
                text="Ok, jeg legger denne til i Notion-databasen.",
            )

    def test_apply_state_sync_writes_pending_and_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pending_file = os.path.join(tmp, "pending.json")
            cursor_file = os.path.join(tmp, "cursor.json")
            config = ServiceConfig(
                slack_bot_token="xoxb-test",
                slack_signing_secret="secret",
                pending_suggestions_file=pending_file,
                cursor_file=cursor_file,
                confirmation_message_file=os.path.join(tmp, "confirm.txt"),
                database_id=None,
                database_name="Oppgaver",
                notion_token=None,
                title_property="Oppgave",
                status_property="Status",
                due_property="Frist",
                source_property="Source",
                client_project_property="Kunde/Prosjekt",
                type_property="🏷️ Type",
                priority_property="🔥 Prioritet",
                status_value="Ikke startet",
                skip_existing=False,
                dry_run=True,
                host="127.0.0.1",
                port=8787,
                state_sync_token="sync-token",
            )
            payload = {
                "pending_suggestions": {"version": 1, "runs": []},
                "approval_cursor": {"version": 1, "processed": {}},
            }
            result = apply_state_sync(config, payload)
            self.assertTrue(result["pending_synced"])
            self.assertTrue(result["cursor_synced"])
            with open(pending_file, "r", encoding="utf-8") as fh:
                pending = fh.read()
            with open(cursor_file, "r", encoding="utf-8") as fh:
                cursor = fh.read()
            self.assertIn('"runs": []', pending)
            self.assertIn('"processed": {}', cursor)


if __name__ == "__main__":
    unittest.main()
