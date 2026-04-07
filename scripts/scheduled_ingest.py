#!/usr/bin/env python3
"""Scheduled multi-source ingestion for Slack/Outlook -> Notion tasks."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
from typing import Any

from capture_to_notion import (
    SyncResult,
    Task,
    extract_tasks,
    normalized_name,
    normalize_space,
    sync_tasks,
)


SUPPORTED_SOURCES = {
    "slack_dm",
    "slack_mention",
    "outlook_email",
    "outlook_calendar",
}

DEFAULT_STATE_FILE = os.path.join(
    os.path.expanduser("~"),
    ".codex",
    "state",
    "notion-todo-capture",
    "scheduled_ingest_state.json",
)
DEFAULT_PENDING_SUGGESTIONS_FILE = os.path.join(
    os.path.expanduser("~"),
    ".codex",
    "state",
    "notion-todo-capture",
    "pending_suggestions.json",
)


@dataclasses.dataclass
class IngestItem:
    source: str
    item_id: str
    text: str
    created_at: str
    url: str | None = None
    is_from_me: bool | None = None


@dataclasses.dataclass
class TaskAssessment:
    task: Task
    source: str
    item_id: str
    confidence: str
    score: int
    reason: str
    snippet: str
    source_url: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scheduled ingest of Slack/Outlook items into Notion tasks.")
    parser.add_argument("--events-file", required=True, help="JSON file containing combined Slack/Outlook events.")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Path to dedupe state file.")
    parser.add_argument("--job-label", default="Scheduled ingest run", help="Label used in output.")
    parser.add_argument("--strict-mode", default="strict", choices=("strict", "balanced", "aggressive"))
    parser.add_argument("--run-id", help="Optional run id for approval flow. Defaults to generated id.")
    parser.add_argument(
        "--pending-suggestions-file",
        default=DEFAULT_PENDING_SUGGESTIONS_FILE,
        help="Path to JSON queue used for medium-suggestion approval flow.",
    )
    parser.add_argument(
        "--approval-message-file",
        help="Optional file path where Slack DM approval message will be written when suggestions exist.",
    )
    parser.add_argument("--database-id", help="Notion database ID. Defaults to NOTION_DATABASE_ID.")
    parser.add_argument(
        "--database-name",
        default="Oppgaver",
        help="Database name for auto-lookup when database ID is not provided (default: Oppgaver).",
    )
    parser.add_argument("--token", help="Notion API token. Defaults to NOTION_API_TOKEN.")
    parser.add_argument("--title-property", default="Oppgave")
    parser.add_argument("--status-property", default="Status")
    parser.add_argument("--due-property", default="Frist")
    parser.add_argument("--source-property", default="Source")
    parser.add_argument("--client-project-property", default="Kunde/Prosjekt")
    parser.add_argument("--type-property", default="🏷️ Type")
    parser.add_argument("--priority-property", default="🔥 Prioritet")
    parser.add_argument("--status-value", default="Ikke startet")
    parser.add_argument("--max-tasks-per-item", type=int, default=8)
    parser.add_argument("--max-items", type=int, default=300)
    parser.add_argument(
        "--extractor",
        choices=("rules",),
        default="rules",
        help="Task extraction mode for each event item (rules only in agent-policy mode).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def load_events(path: str, max_items: int) -> list[IngestItem]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    if isinstance(payload, dict):
        raw_items = payload.get("items", [])
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raise SystemExit("events-file must be a JSON list or an object with an 'items' list.")

    items: list[IngestItem] = []
    for idx, raw in enumerate(raw_items[:max_items]):
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source", "")).strip().lower()
        if source not in SUPPORTED_SOURCES:
            continue
        item_id = str(raw.get("id") or raw.get("external_id") or f"{source}-{idx}")
        created_at = str(raw.get("created_at") or raw.get("timestamp") or dt.datetime.now(dt.UTC).isoformat())
        url = raw.get("url")
        text = build_text(raw)
        if not text:
            continue
        items.append(
            IngestItem(
                source=source,
                item_id=item_id,
                text=text,
                created_at=created_at,
                url=url,
                is_from_me=parse_outgoing_flag(raw),
            )
        )
    return items


def parse_outgoing_flag(raw: dict[str, Any]) -> bool | None:
    direct = raw.get("is_from_me")
    if isinstance(direct, bool):
        return direct
    if isinstance(direct, str):
        low = direct.strip().casefold()
        if low in {"true", "1", "yes", "ja", "y"}:
            return True
        if low in {"false", "0", "no", "nei", "n"}:
            return False

    from_me = raw.get("from_me")
    if isinstance(from_me, bool):
        return from_me
    if isinstance(from_me, str):
        low = from_me.strip().casefold()
        if low in {"true", "1", "yes", "ja", "y"}:
            return True
        if low in {"false", "0", "no", "nei", "n"}:
            return False

    direction = str(raw.get("direction", "")).strip().casefold()
    if direction in {"outgoing", "sent", "from_me"}:
        return True
    if direction in {"incoming", "received", "to_me"}:
        return False
    return None


def build_text(raw: dict[str, Any]) -> str:
    if "text" in raw and raw["text"]:
        return normalize_space(str(raw["text"]))
    if "message" in raw and raw["message"]:
        return normalize_space(str(raw["message"]))

    subject = normalize_space(str(raw.get("subject", "")))
    body = normalize_space(str(raw.get("body", "") or raw.get("body_preview", "") or raw.get("content", "")))
    summary = normalize_space(str(raw.get("summary", "")))

    parts = [part for part in (subject, summary, body) if part]
    return normalize_space(" | ".join(parts))


def load_state(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {"version": 1, "seen": {}, "last_run_at": None}
    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    if not isinstance(state, dict):
        return {"version": 1, "seen": {}, "last_run_at": None}
    if "seen" not in state or not isinstance(state.get("seen"), dict):
        state["seen"] = {}
    return state


def save_state(path: str, state: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)


def prune_state(state: dict[str, Any], keep_days: int = 21, max_entries: int = 8000) -> None:
    seen = state.get("seen", {})
    if not isinstance(seen, dict):
        state["seen"] = {}
        return

    now = dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(days=keep_days)
    filtered: dict[str, str] = {}
    for fp, ts in seen.items():
        try:
            parsed = dt.datetime.fromisoformat(str(ts))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.UTC)
        except ValueError:
            continue
        if parsed >= cutoff:
            filtered[fp] = parsed.isoformat()

    if len(filtered) > max_entries:
        sorted_items = sorted(filtered.items(), key=lambda kv: kv[1], reverse=True)[:max_entries]
        filtered = dict(sorted_items)
    state["seen"] = filtered


def fingerprint_item(item: IngestItem) -> str:
    raw = f"{item.source}|{item.item_id}|{normalize_space(item.text)}|{item.created_at}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


ACTION_PATTERNS = (
    r"\b(sette opp|setter opp|opprette|oppretter|lage|lager|sende|sender|sendt|"
    r"booke|booker|oppdatere|oppdaterer|presentere|presenterer|"
    r"følge opp|folger opp|follow up|ferdigstille|ferdigstiller|"
    r"avklare|avklarer|fordele|fordeler|skalere|skalerer|kalle inn|kaller inn|"
    r"fikse|fikser|endre|endrer|justere|justerer|"
    r"legge til|legger til|svarer opp|notere|noterer)\b",
    r"\b(prepare|send|book|update|create|deliver|share|draft|finalize|"
    r"follow up|set up|check with|confirm|change|adjust)\b",
    r"\b(kan du|kan dere|skal jeg)\s+(sende|booke|oppdatere|kalle inn|legge til|avklare|fordele)\b",
    r"\b(legger du til|kan du legge til|kaller du inn|sender du)\b",
)
REQUEST_PATTERNS = (
    r"\b(kan du|kan dere|kan vi)\b",
    r"\b(har du tid til|har dere tid til)\b",
    r"\b(fikk dere sett|fikk du sett)\b",
    r"\b(trenger|trengs|need to)\b",
    r"\b(legg(?:er)? du til|kall(?:er)? inn|sette opp|refreshe?)\b",
)
COMMITMENT_PATTERNS = (
    r"\b(jeg|vi)\s+(fikser|ordner|tar|ser på|følger opp|folger opp|skal)\b",
    r"\b(jeg|vi)\s+(tar ansvar for å|setter av tid til å)\b",
    r"\b(fikser det|ser på det|tar det nå)\b",
    r"\b(yes|yes,|ok,?\s+jeg)\b",
)
REMINDER_PATTERNS = (
    r"\b(fyi|til info|husk|noter(?:e)?|påminnelse|paminnelse)\b",
    r"\b(fint om du noterer|skal prøve å huske)\b",
)
COMPLETED_PATTERNS = (
    r"\b(sendt|ferdigstilt|allerede gjort|lagt opp)\b",
)
DELIVERABLE_HINTS = (
    "rapport",
    "report",
    "utkast",
    "plan",
    "kampanje",
    "meeting",
    "møte",
    "presentasjon",
    "dokument",
    "oversikt",
    "invitasjon",
    "funksjon",
    "budsjett",
    "disclaimer",
)
OWNER_HINTS = (
    "jakob",
    "ansvarlig",
    "owner",
    "kundeansvarlig",
    "@",
)
NON_OWNER_SUBJECTS = {
    "jeg",
    "vi",
    "du",
    "dere",
    "han",
    "hun",
    "de",
    "den",
    "det",
    "man",
}
NEGATIVE_PATTERNS = (
    r"\b(takk|thanks|god morgen|god helg|haha|lol|for info|status update)\b",
    r"\b(hvordan gaar det|hvordan går det|smalltalk|prat)\b",
)
STATUS_CHECK_PATTERNS = (
    r"\b(fikk dere sett|fikk du sett|har dere sett|har du sett)\b",
    r"\b(status|oppdatering)\b.{0,40}\?",
)
DELEGATION_PATTERNS = (
    r"\b(si ifra|gi beskjed)\s+hvis du\b",
    r"\bfor [^.!?]{0,80}\bkan du\b",
    r"\bkan du\b.{0,60}\bdupliser\w*\b",
    r"\bhar du tid til\b.{0,60}\b(fikse|ordne|ta)\b",
)
THIRD_PARTY_OWNER_PATTERNS = (
    r"\b(?!jeg\b|vi\b|du\b|dere\b)([a-zæøå][a-zæøå0-9_-]{2,})\s+kan\b.{0,40}\b(fikse|ordne|ta|sende|lage|sette opp)\b",
)
DIRECTIVE_PATTERNS = (
    r"^\s*(endre|sett(?:e)? opp|oppdater(?:e)?|send(?:e)?|book(?:e)?|lag(?:e)?|legg(?:e)? til|"
    r"følg(?:e)? opp|fiks(?:e)?|avklar(?:e)?|fordel(?:e)?|noter(?:e)?)\b",
    r"^\s*husk\b",
)


def has_action_signal(text: str) -> bool:
    low = text.casefold()
    return any(re.search(pattern, low) for pattern in ACTION_PATTERNS)


def has_request_signal(text: str) -> bool:
    low = text.casefold()
    return any(re.search(pattern, low) for pattern in REQUEST_PATTERNS)


def has_commitment_signal(text: str) -> bool:
    low = text.casefold()
    return any(re.search(pattern, low) for pattern in COMMITMENT_PATTERNS)


def has_reminder_signal(text: str) -> bool:
    low = text.casefold()
    return any(re.search(pattern, low) for pattern in REMINDER_PATTERNS)


def has_completed_signal(text: str) -> bool:
    low = text.casefold()
    return any(re.search(pattern, low) for pattern in COMPLETED_PATTERNS)


def has_meaningful_object(title: str) -> bool:
    words = [w for w in re.split(r"\s+", title) if w]
    if len(words) < 3:
        return False
    stripped = normalized_name(title)
    return len(stripped) >= 8


def has_deliverable_signal(text: str) -> bool:
    low = text.casefold()
    return any(token in low for token in DELIVERABLE_HINTS)


def has_owner_signal(text: str) -> bool:
    low = text.casefold()
    if any(token in low for token in OWNER_HINTS):
        return True
    for match in re.finditer(r"\b([a-zæøå][a-zæøå0-9_-]{1,30})\s+skal\b", low):
        subject = match.group(1)
        if subject not in NON_OWNER_SUBJECTS:
            return True
    return bool(re.search(r"\bto\s+[A-Z][a-z]+\b", text))


def has_negative_signal(text: str) -> bool:
    low = text.casefold()
    return any(re.search(pattern, low) for pattern in NEGATIVE_PATTERNS)


def has_status_check_signal(text: str) -> bool:
    low = text.casefold()
    return any(re.search(pattern, low) for pattern in STATUS_CHECK_PATTERNS)


def has_delegation_signal(text: str) -> bool:
    low = text.casefold()
    return any(re.search(pattern, low) for pattern in DELEGATION_PATTERNS)


def has_third_party_owner_signal(text: str) -> bool:
    low = text.casefold()
    return any(re.search(pattern, low) for pattern in THIRD_PARTY_OWNER_PATTERNS)


def has_directive_signal(text: str) -> bool:
    low = text.casefold()
    return any(re.search(pattern, low) for pattern in DIRECTIVE_PATTERNS)


def assess_task(item: IngestItem, task: Task) -> TaskAssessment:
    text = f"{item.text} {task.title}"
    action = has_action_signal(text)
    request = has_request_signal(text)
    commitment = has_commitment_signal(text)
    reminder = has_reminder_signal(text)
    completed = has_completed_signal(text)
    obj = has_meaningful_object(task.title)
    due = task.due_date is not None
    deliverable = has_deliverable_signal(text)
    owner = has_owner_signal(text)
    negative = has_negative_signal(text)
    status_check = has_status_check_signal(text)
    delegation = has_delegation_signal(text)
    third_party_owner = has_third_party_owner_signal(text)
    directive = has_directive_signal(task.source_excerpt)

    score = 0
    reasons: list[str] = []

    if action:
        score += 35
        reasons.append("handling")
    if request:
        score += 15
        reasons.append("forespørsel")
    if directive:
        score += 15
        reasons.append("direktiv")
    if commitment:
        score += 20
        reasons.append("bekreftet ansvar")
    if reminder:
        score += 20
        reasons.append("påminnelse")
    if obj:
        score += 25
        reasons.append("objekt")
    if due:
        score += 25
        reasons.append("frist")
    if owner or deliverable:
        score += 20
        reasons.append("ansvar/leveranse")
    if completed and not due:
        score -= 20
        reasons.append("statusoppdatering")
    if negative and not due:
        score -= 40
        reasons.append("småprat-signal")
    if status_check and not commitment:
        score -= 35
        reasons.append("statusspørsmål uten ansvar")
    if delegation and not commitment:
        score -= 30
        reasons.append("delegasjonssignal")
    if third_party_owner and not commitment:
        score -= 30
        reasons.append("tredjepart peker ut eier")
    if item.is_from_me and request and not commitment:
        score -= 45
        reasons.append("utgående delegering")

    has_intent = action or request or commitment or reminder or directive
    required_ok = has_intent and obj and (
        due or owner or commitment or reminder or directive or (request and action)
    )
    if status_check and not (commitment or due or owner):
        required_ok = False
    if delegation and not (commitment or due or owner):
        required_ok = False
    if third_party_owner and not commitment:
        required_ok = False
    if item.is_from_me and request and not commitment:
        required_ok = False
    strong_high = required_ok and (commitment or (due and (owner or directive)) or (directive and owner))

    if (strong_high and score >= 70) or (commitment and required_ok and score >= 60):
        confidence = "high"
    elif required_ok and score >= 50:
        confidence = "medium"
    else:
        confidence = "low"

    reason = ", ".join(reasons) if reasons else "mangler tydelig oppgave-signal"
    return TaskAssessment(
        task=task,
        source=item.source,
        item_id=item.item_id,
        confidence=confidence,
        score=score,
        reason=reason,
        snippet=item.text[:220],
        source_url=item.url,
    )


def dedupe_assessments(items: list[TaskAssessment]) -> list[TaskAssessment]:
    deduped: list[TaskAssessment] = []
    seen: set[str] = set()
    for assessment in items:
        key = f"{normalized_name(assessment.task.title)}|{assessment.task.due_date or ''}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(assessment)
    return deduped


def select_for_upsert(mode: str, assessments: list[TaskAssessment]) -> tuple[list[TaskAssessment], list[TaskAssessment]]:
    if mode == "aggressive":
        auto = [a for a in assessments if a.confidence in {"high", "medium"}]
        suggestions: list[TaskAssessment] = []
    elif mode == "balanced":
        auto = [a for a in assessments if a.confidence == "high"]
        suggestions = [a for a in assessments if a.confidence == "medium"]
    else:
        auto = [a for a in assessments if a.confidence == "high"]
        suggestions = [a for a in assessments if a.confidence == "medium"]
    return auto, suggestions


def render_sync_table(sync_result: SyncResult) -> str:
    if not sync_result.summary_rows:
        return "_Ingen oppgaver opprettet/oppdatert._"
    lines = [
        "| Oppgave | Frist | Type | Prioritet | Kunde/Prosjekt | Resultat |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in sync_result.summary_rows:
        due = row.due_date or "-"
        task_type = row.task_type or "-"
        priority = row.priority or "-"
        customer = row.customer_project or "-"
        lines.append(f"| {row.title} | {due} | {task_type} | {priority} | {customer} | {row.result} |")
    return "\n".join(lines)


def render_suggestion_table(suggestions: list[TaskAssessment]) -> str:
    if not suggestions:
        return "_Ingen forslag trenger bekreftelse._"
    lines = [
        "| Oppgave (forslag) | Confidence | Begrunnelse | Kilde |",
        "| --- | --- | --- | --- |",
    ]
    for item in suggestions:
        due = f" (frist {item.task.due_date})" if item.task.due_date else ""
        source = f"{item.source}:{item.item_id}"
        lines.append(f"| {item.task.title}{due} | {item.confidence} ({item.score}) | {item.reason} | {source} |")
    return "\n".join(lines)


def render_unmapped_table(sync_result: SyncResult) -> str:
    if not sync_result.unmapped_client_project:
        return "_Alle oppgaver har Kunde/Prosjekt-mapping._"
    lines = [
        "| Oppgave | Forsokt mapping |",
        "| --- | --- |",
    ]
    for title, hint in sync_result.unmapped_client_project:
        lines.append(f"| {title} | {hint} |")
    return "\n".join(lines)


def build_run_id(now_iso: str, job_label: str) -> str:
    compact = now_iso.replace(":", "").replace("-", "").replace("+", "_plus_")
    safe_label = re.sub(r"[^a-z0-9]+", "-", job_label.casefold()).strip("-")
    return f"run-{compact}-{safe_label[:40] or 'scheduled'}"


def load_pending_suggestions(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {"version": 1, "runs": []}
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        return {"version": 1, "runs": []}
    runs = payload.get("runs")
    if not isinstance(runs, list):
        payload["runs"] = []
    return payload


def save_pending_suggestions(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)


def prune_pending_suggestions(payload: dict[str, Any], keep_days: int = 21, max_runs: int = 200) -> None:
    runs = payload.get("runs", [])
    if not isinstance(runs, list):
        payload["runs"] = []
        return

    now = dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(days=keep_days)
    kept: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        created_at = run.get("created_at")
        if not isinstance(created_at, str):
            continue
        try:
            parsed = dt.datetime.fromisoformat(created_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.UTC)
        except ValueError:
            continue
        if parsed >= cutoff:
            kept.append(run)

    kept = sorted(kept, key=lambda entry: str(entry.get("created_at")), reverse=True)[:max_runs]
    payload["runs"] = kept


def record_pending_suggestions(
    path: str,
    run_id: str,
    job_label: str,
    created_at: str,
    suggestions: list[TaskAssessment],
) -> None:
    payload = load_pending_suggestions(path)
    prune_pending_suggestions(payload)
    runs = payload.get("runs", [])
    if not isinstance(runs, list):
        runs = []

    entries: list[dict[str, Any]] = []
    for idx, item in enumerate(suggestions, start=1):
        entries.append(
            {
                "index": idx,
                "status": "pending",
                "confidence": item.confidence,
                "score": item.score,
                "reason": item.reason,
                "source": item.source,
                "item_id": item.item_id,
                "source_url": item.source_url,
                "task": {
                    "title": item.task.title,
                    "due_date": item.task.due_date,
                    "source_excerpt": item.task.source_excerpt,
                    "due_phrase": item.task.due_phrase,
                },
            }
        )

    runs = [run for run in runs if not (isinstance(run, dict) and run.get("run_id") == run_id)]
    runs.insert(
        0,
        {
            "run_id": run_id,
            "job_label": job_label,
            "created_at": created_at,
            "status": "pending_review",
            "suggestions": entries,
        },
    )
    payload["runs"] = runs
    save_pending_suggestions(path, payload)


def render_slack_approval_message(run_id: str, job_label: str, suggestions: list[TaskAssessment]) -> str:
    if not suggestions:
        return ""

    lines = [
        "*Skal jeg legge til disse i Notion?*",
        "",
    ]
    for idx, item in enumerate(suggestions, start=1):
        due = f" (frist {item.task.due_date})" if item.task.due_date else ""
        lines.append(f"{idx}. {item.task.title}{due}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    items = load_events(args.events_file, max_items=args.max_items)
    if args.verbose:
        print(f"Loaded {len(items)} source item(s) from {args.events_file}.")

    state = load_state(args.state_file)
    prune_state(state)
    seen = state.get("seen", {})

    fresh_items: list[IngestItem] = []
    now = dt.datetime.now(dt.UTC).isoformat()
    for item in items:
        fp = fingerprint_item(item)
        if fp in seen:
            continue
        seen[fp] = now
        fresh_items.append(item)
    state["seen"] = seen
    state["last_run_at"] = now

    all_assessments: list[TaskAssessment] = []
    for item in fresh_items:
        tasks = extract_tasks(item.text, max_tasks=args.max_tasks_per_item, extractor=args.extractor)
        for task in tasks:
            all_assessments.append(assess_task(item, task))

    all_assessments = dedupe_assessments(all_assessments)
    auto_candidates, suggestions = select_for_upsert(args.strict_mode, all_assessments)
    run_id = args.run_id or build_run_id(now, args.job_label)

    slack_approval_message = ""
    if suggestions:
        record_pending_suggestions(
            path=args.pending_suggestions_file,
            run_id=run_id,
            job_label=args.job_label,
            created_at=now,
            suggestions=suggestions,
        )
        slack_approval_message = render_slack_approval_message(run_id, args.job_label, suggestions)
        if args.approval_message_file:
            directory = os.path.dirname(args.approval_message_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(args.approval_message_file, "w", encoding="utf-8") as fh:
                fh.write(slack_approval_message)

    sync_result = SyncResult(created=0, updated=0, skipped=0, summary_rows=[], unmapped_client_project=[])
    if auto_candidates and not args.dry_run:
        sync_result = sync_tasks(
            args=args,
            tasks=[candidate.task for candidate in auto_candidates],
            emit_output=False,
        )

    print(f"# {args.job_label}")
    print("")
    print(f"- Tidspunkt: {now}")
    print(f"- Nye kildeelementer: {len(fresh_items)}")
    print(f"- Kandidater vurdert: {len(all_assessments)}")
    print(f"- Auto-upsert (modus {args.strict_mode}): {len(auto_candidates)}")
    print(f"- Forslag til bekreftelse: {len(suggestions)}")
    print(f"- Run ID: {run_id}")
    if args.dry_run:
        print("- Kjoring: dry-run (ingen Notion-endringer)")
    else:
        print(
            f"- Notion: Created {sync_result.created}, Updated {sync_result.updated}, Skipped {sync_result.skipped}"
        )

    print("")
    print("## Opprettet/Oppdatert")
    print(render_sync_table(sync_result))
    print("")
    print("## Forslag (trenger bekreftelse)")
    print(render_suggestion_table(suggestions))
    if slack_approval_message:
        print("")
        print("## Slack DM Forslag")
        print("```")
        print(slack_approval_message)
        print("```")
    print("")
    print("## Uten kunde/prosjekt")
    print(render_unmapped_table(sync_result))

    if sync_result.unmapped_client_project:
        print("")
        print("Vil du opprette manglende kunde/prosjekt i Notion, og koble disse oppgavene?")

    save_state(args.state_file, state)
    return 0


def main() -> None:
    args = parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
