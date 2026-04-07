#!/usr/bin/env python3
"""Unit tests for apply_suggestion_decision.py."""

from __future__ import annotations

import os
import sys
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from apply_suggestion_decision import parse_decision_text


class ApplySuggestionDecisionTests(unittest.TestCase):
    def test_parse_approve_with_run_and_indices(self) -> None:
        parsed = parse_decision_text("JA run-20260403T1200Z-12-00 1,3")
        self.assertEqual(parsed.action, "approve")
        self.assertEqual(parsed.run_id, "run-20260403T1200Z-12-00")
        self.assertEqual(parsed.indices, [1, 3])
        self.assertEqual(parsed.excluded_indices, [])
        self.assertFalse(parsed.all_items)

    def test_parse_reject_all(self) -> None:
        parsed = parse_decision_text("NEI run-abc123 ALLE")
        self.assertEqual(parsed.action, "reject")
        self.assertEqual(parsed.run_id, "run-abc123")
        self.assertTrue(parsed.all_items)

    def test_parse_approve_without_run(self) -> None:
        parsed = parse_decision_text("JA 2 4")
        self.assertEqual(parsed.action, "approve")
        self.assertIsNone(parsed.run_id)
        self.assertEqual(parsed.indices, [2, 4])
        self.assertEqual(parsed.excluded_indices, [])

    def test_parse_synonym_approve_with_index(self) -> None:
        parsed = parse_decision_text("Gjør det run-xyz 2")
        self.assertEqual(parsed.action, "approve")
        self.assertEqual(parsed.run_id, "run-xyz")
        self.assertEqual(parsed.indices, [2])
        self.assertEqual(parsed.excluded_indices, [])

    def test_parse_synonym_reject_all(self) -> None:
        parsed = parse_decision_text("Ikke gjør det run-xyz")
        self.assertEqual(parsed.action, "reject")
        self.assertEqual(parsed.run_id, "run-xyz")
        self.assertTrue(parsed.all_items)

    def test_parse_synonym_approve_defaults_to_all(self) -> None:
        parsed = parse_decision_text("Det stemmer run-xyz")
        self.assertEqual(parsed.action, "approve")
        self.assertEqual(parsed.run_id, "run-xyz")
        self.assertTrue(parsed.all_items)

    def test_parse_synonym_reject_with_index(self) -> None:
        parsed = parse_decision_text("Dropp run-xyz 1,3")
        self.assertEqual(parsed.action, "reject")
        self.assertEqual(parsed.run_id, "run-xyz")
        self.assertEqual(parsed.indices, [1, 3])
        self.assertEqual(parsed.excluded_indices, [])

    def test_parse_natural_language_with_include_and_exclude(self) -> None:
        parsed = parse_decision_text("Du kan legge til 1, 2 og 3, men ikke 4-6")
        self.assertEqual(parsed.action, "approve")
        self.assertIsNone(parsed.run_id)
        self.assertFalse(parsed.all_items)
        self.assertEqual(parsed.indices, [1, 2, 3])
        self.assertEqual(parsed.excluded_indices, [4, 5, 6])

    def test_parse_natural_language_all_except(self) -> None:
        parsed = parse_decision_text("Legg til alle unntatt 4-6")
        self.assertEqual(parsed.action, "approve")
        self.assertTrue(parsed.all_items)
        self.assertEqual(parsed.excluded_indices, [4, 5, 6])

    def test_parse_singular_reference_defaults_to_first_index(self) -> None:
        parsed = parse_decision_text("Ja, legg til denne i notion to do databasen")
        self.assertEqual(parsed.action, "approve")
        self.assertIsNone(parsed.run_id)
        self.assertFalse(parsed.all_items)
        self.assertEqual(parsed.indices, [1])
        self.assertEqual(parsed.excluded_indices, [])

    def test_parse_reject_singular_reference_defaults_to_first_index(self) -> None:
        parsed = parse_decision_text("Nei, ikke legg til denne")
        self.assertEqual(parsed.action, "reject")
        self.assertIsNone(parsed.run_id)
        self.assertFalse(parsed.all_items)
        self.assertEqual(parsed.indices, [1])
        self.assertEqual(parsed.excluded_indices, [])

    def test_parse_run_id_with_markdown_wrapping(self) -> None:
        parsed = parse_decision_text("JA _run-abc123_ 1")
        self.assertEqual(parsed.action, "approve")
        self.assertEqual(parsed.run_id, "run-abc123")
        self.assertEqual(parsed.indices, [1])


if __name__ == "__main__":
    unittest.main()
