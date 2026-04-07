#!/usr/bin/env python3
"""Unit tests for approval_listener.py."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from approval_listener import process_approval_event


def sample_pending(run_id: str) -> dict:
    return {
        "version": 1,
        "runs": [
            {
                "run_id": run_id,
                "job_label": "12:00 run",
                "created_at": "2026-04-07T10:03:24.287526+00:00",
                "status": "pending_review",
                "suggestions": [
                    {
                        "index": 1,
                        "status": "pending",
                        "confidence": "medium",
                        "score": 75,
                        "reason": "handling, objekt",
                        "source": "slack_dm",
                        "item_id": "slack-1",
                        "task": {
                            "title": "Send rask status på konkurransen",
                            "due_date": "2026-04-07",
                            "source_excerpt": "kan du sende en rask status på konkurransen?",
                            "due_phrase": None,
                        },
                    }
                ],
            }
        ],
    }


def build_args(pending_file: str, cursor_file: str) -> argparse.Namespace:
    return argparse.Namespace(
        pending_suggestions_file=pending_file,
        cursor_file=cursor_file,
        run_id=None,
        database_id=None,
        database_name="Oppgaver",
        token=None,
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
        confirmation_message_file=None,
        result_file=None,
        verbose=False,
    )


class ApprovalListenerTests(unittest.TestCase):
    def test_process_approve_from_natural_thread_reply(self) -> None:
        run_id = "run-20260407T100324.287526_plus_0000-12-00-run"
        event_payload = {
            "event_id": "Ev001",
            "event": {
                "text": "Ja, legg til denne i notion to do databasen",
                "channel": "D123",
                "ts": "1775547000.000100",
                "thread_ts": "1775546900.000100",
                "user": "U123",
                "thread_root_text": f"*Skal jeg legge til disse i Notion?*\n_Run: {run_id}_",
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            pending_file = os.path.join(tmp, "pending.json")
            cursor_file = os.path.join(tmp, "cursor.json")
            with open(pending_file, "w", encoding="utf-8") as fh:
                json.dump(sample_pending(run_id), fh)

            args = build_args(pending_file=pending_file, cursor_file=cursor_file)
            result = process_approval_event(args, event_payload)

            self.assertEqual(result.status, "processed")
            self.assertEqual(result.run_id, run_id)
            self.assertEqual(result.approved_count, 1)
            self.assertEqual(result.rejected_count, 0)
            self.assertIn("i en test", result.confirmation_message)

            with open(pending_file, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            status = stored["runs"][0]["suggestions"][0]["status"]
            self.assertEqual(status, "approved")

    def test_process_duplicate_event(self) -> None:
        run_id = "run-abc"
        event_payload = {
            "event_id": "EvDup",
            "event": {
                "text": f"JA {run_id} 1",
                "channel": "D123",
                "ts": "1775547000.000100",
                "thread_ts": "1775546900.000100",
                "user": "U123",
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            pending_file = os.path.join(tmp, "pending.json")
            cursor_file = os.path.join(tmp, "cursor.json")
            with open(pending_file, "w", encoding="utf-8") as fh:
                json.dump(sample_pending(run_id), fh)

            args = build_args(pending_file=pending_file, cursor_file=cursor_file)
            first = process_approval_event(args, event_payload)
            second = process_approval_event(args, event_payload)

            self.assertEqual(first.status, "processed")
            self.assertEqual(second.status, "duplicate")

    def test_process_reject_from_natural_thread_reply(self) -> None:
        run_id = "run-xyz"
        event_payload = {
            "event_id": "Ev002",
            "event": {
                "text": "Nei, ikke legg til denne",
                "channel": "D123",
                "ts": "1775547001.000100",
                "thread_ts": "1775546900.000100",
                "user": "U123",
                "thread_root_text": f"_Run: {run_id}_",
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            pending_file = os.path.join(tmp, "pending.json")
            cursor_file = os.path.join(tmp, "cursor.json")
            with open(pending_file, "w", encoding="utf-8") as fh:
                json.dump(sample_pending(run_id), fh)

            args = build_args(pending_file=pending_file, cursor_file=cursor_file)
            result = process_approval_event(args, event_payload)

            self.assertEqual(result.status, "processed")
            self.assertEqual(result.approved_count, 0)
            self.assertEqual(result.rejected_count, 1)

            with open(pending_file, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            status = stored["runs"][0]["suggestions"][0]["status"]
            self.assertEqual(status, "rejected")

    def test_select_run_from_thread_title_when_run_id_missing(self) -> None:
        run_a = sample_pending("run-a")
        run_b = sample_pending("run-b")
        run_b["runs"][0]["suggestions"][0]["task"]["title"] = "Annen oppgave"
        payload = {
            "version": 1,
            "runs": [
                run_b["runs"][0],
                run_a["runs"][0],
            ],
        }
        event_payload = {
            "event_id": "Ev003",
            "event": {
                "text": "Ja, legg til denne",
                "channel": "D123",
                "ts": "1775547002.000100",
                "thread_ts": "1775546900.000100",
                "user": "U123",
                "thread_root_text": "*Skal jeg legge til disse i Notion?*\n1. Send rask status på konkurransen",
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            pending_file = os.path.join(tmp, "pending.json")
            cursor_file = os.path.join(tmp, "cursor.json")
            with open(pending_file, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)

            args = build_args(pending_file=pending_file, cursor_file=cursor_file)
            result = process_approval_event(args, event_payload)

            self.assertEqual(result.status, "processed")
            self.assertEqual(result.run_id, "run-a")


if __name__ == "__main__":
    unittest.main()
