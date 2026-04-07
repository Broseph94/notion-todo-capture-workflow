#!/usr/bin/env python3
"""Unit tests for scheduled_ingest.py."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from capture_to_notion import Task
from scheduled_ingest import (
    IngestItem,
    assess_task,
    dedupe_assessments,
    load_events,
    select_for_upsert,
)


class ScheduledIngestTests(unittest.TestCase):
    def test_assess_high_confidence_with_due(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="1",
            text="Vi skal sende kampanjerapport til Obs innen torsdag.",
            created_at="2026-04-02T10:00:00+00:00",
        )
        task = Task(
            title="Sende kampanjerapport til Obs",
            due_date="2026-04-09",
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "high")

    def test_assess_low_confidence_smalltalk(self) -> None:
        item = IngestItem(
            source="slack_mention",
            item_id="2",
            text="Takk for prat i dag, dette ser bra ut!",
            created_at="2026-04-02T10:00:00+00:00",
        )
        task = Task(title="Takk for prat i dag", due_date=None, source_excerpt=item.text)
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "low")

    def test_assess_medium_confidence_for_request_action(self) -> None:
        item = IngestItem(
            source="slack_mention",
            item_id="2b",
            text="Charlie, legger du til Hanna i invitasjonen fra og med 7. april?",
            created_at="2026-04-02T10:00:00+00:00",
        )
        task = Task(
            title="Legger du til Hanna i invitasjonen",
            due_date="2026-04-07",
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertIn(assessed.confidence, {"medium", "high"})

    def test_assess_high_for_request_plus_confirmation(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="2c",
            text="Kan du refreshe mailen med anbefaling til Ole om media videre? Yes, jeg fikser.",
            created_at="2026-04-02T10:00:00+00:00",
        )
        task = Task(
            title="Refreshe mail med anbefaling til Ole",
            due_date=None,
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "high")

    def test_assess_medium_for_fyi_reminder(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="2d",
            text="FYI - du kan fakturere Briskebyen for 2 ekstra timer. Fint om du noterer det til møtet etter påske.",
            created_at="2026-04-02T10:00:00+00:00",
        )
        task = Task(
            title="Notere fakturering av 2 ekstra timer for Briskebyen til møtet etter påske",
            due_date=None,
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "medium")

    def test_assess_high_for_status_question_with_fix_confirmation(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="2e",
            text="Fikk dere sett på rapport for live-sending? Tror ikke han rakk det i går. Fikser det nå.",
            created_at="2026-04-02T10:00:00+00:00",
        )
        task = Task(
            title="Rapport for live-sending",
            due_date=None,
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "high")

    def test_assess_low_for_status_question_without_commitment(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="2f",
            text="Fikk dere sett pa rapport for live-sending?",
            created_at="2026-04-02T10:00:00+00:00",
        )
        task = Task(
            title="Se pa rapport for live-sending",
            due_date=None,
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "low")

    def test_assess_low_for_delegation_signal_without_commitment(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="2g",
            text="Si ifra hvis du trenger bistand. For lead ad-skjema kan du duplisere det som finnes og lage nytt med oppdaterte tekster.",
            created_at="2026-04-02T10:00:00+00:00",
        )
        task = Task(
            title="Duplisere lead ad-skjema med oppdaterte tekster",
            due_date=None,
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "low")

    def test_assess_low_for_implicit_action_without_owner_or_due(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="2h",
            text="Ma nok lage en ny kampanje. Ta utgangspunkt i at den skal ga i 6 uker.",
            created_at="2026-04-02T10:00:00+00:00",
        )
        task = Task(
            title="Lage ny kampanje med varighet 6 uker",
            due_date=None,
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "low")

    def test_assess_low_for_third_party_owner_with_due(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="2i",
            text="Aron kan sikkert fikse det pa torsdag.",
            created_at="2026-04-02T10:00:00+00:00",
        )
        task = Task(
            title="Fikse dette",
            due_date="2026-04-09",
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "low")

    def test_assess_low_for_outgoing_request_without_commitment(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="2j",
            text="Kan du sette opp en ny kampanje for ARK?",
            created_at="2026-04-02T10:00:00+00:00",
            is_from_me=True,
        )
        task = Task(
            title="Sette opp ny kampanje for ARK",
            due_date=None,
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "low")

    def test_assess_low_for_outgoing_request_with_due_without_commitment(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="2k",
            text="Kan du fikse dette pa Nabofryd forst. Frist sondag.",
            created_at="2026-04-02T10:00:00+00:00",
            is_from_me=True,
        )
        task = Task(
            title="Fikse Nabofryd-post",
            due_date="2026-04-05",
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "low")

    def test_assess_low_for_outgoing_har_du_tid_til(self) -> None:
        item = IngestItem(
            source="slack_dm",
            item_id="2l",
            text="Uke 15 er ikke satt opp, har du tid til a fikse?",
            created_at="2026-04-02T10:00:00+00:00",
            is_from_me=True,
        )
        task = Task(
            title="Sette opp uke 15",
            due_date="2026-04-06",
            source_excerpt=item.text,
        )
        assessed = assess_task(item, task)
        self.assertEqual(assessed.confidence, "low")

    def test_dedupe_assessments_by_title_due(self) -> None:
        item = IngestItem(
            source="outlook_email",
            item_id="3",
            text="Oppdater rapporten innen fredag",
            created_at="2026-04-02T10:00:00+00:00",
        )
        t1 = Task(title="Oppdatere rapporten", due_date="2026-04-03", source_excerpt=item.text)
        t2 = Task(title="Oppdatere rapporten", due_date="2026-04-03", source_excerpt=item.text)
        a1 = assess_task(item, t1)
        a2 = assess_task(item, t2)
        deduped = dedupe_assessments([a1, a2])
        self.assertEqual(len(deduped), 1)

    def test_select_for_upsert_strict(self) -> None:
        item = IngestItem(
            source="outlook_calendar",
            item_id="4",
            text="Booke strategimote med Obs neste uke",
            created_at="2026-04-02T10:00:00+00:00",
        )
        high = assess_task(item, Task("Booke strategimote med Obs", "2026-04-10", item.text))
        medium = assess_task(item, Task("Følge opp videre", None, "Jakob skal folge opp videre"))
        low = assess_task(item, Task("God helg", None, "God helg"))

        high.confidence = "high"
        medium.confidence = "medium"
        low.confidence = "low"
        auto, suggestions = select_for_upsert("strict", [high, medium, low])
        self.assertEqual(len(auto), 1)
        self.assertEqual(len(suggestions), 1)

    def test_load_events_from_items_object(self) -> None:
        payload = {
            "items": [
                {
                    "source": "slack_dm",
                    "id": "x1",
                    "text": "Sett opp ny kampanje for ARK innen mandag",
                    "created_at": "2026-04-02T12:00:00+00:00",
                    "direction": "outgoing",
                }
            ]
        }
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".json") as fh:
            json.dump(payload, fh)
            fh.flush()
            items = load_events(fh.name, max_items=10)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "slack_dm")
        self.assertIs(items[0].is_from_me, True)


if __name__ == "__main__":
    unittest.main()
