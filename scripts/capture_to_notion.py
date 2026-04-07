#!/usr/bin/env python3
"""Extract TODO items from text and upsert them to a Notion database."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any


API_BASE = "https://api.notion.com/v1"
DEFAULT_NOTION_VERSION = "2022-06-28"
OPENAI_API_BASE = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


@dataclasses.dataclass
class Task:
    title: str
    due_date: str | None
    source_excerpt: str
    due_phrase: str | None = None


class NotionApiError(RuntimeError):
    """Raised when Notion returns an error response."""


class NotionClient:
    def __init__(self, token: str, notion_version: str = DEFAULT_NOTION_VERSION) -> None:
        self.token = token
        self.notion_version = notion_version

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url=f"{API_BASE}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.notion_version,
                "Content-Type": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise NotionApiError(f"Notion API error {exc.code} at {path}: {details}") from exc
        except urllib.error.URLError as exc:
            raise NotionApiError(f"Network error calling Notion API: {exc.reason}") from exc

    def get_database(self, database_id: str) -> dict[str, Any]:
        return self._request("GET", f"/databases/{database_id}")

    def search_databases(self, query: str, page_size: int = 25) -> list[dict[str, Any]]:
        payload = {
            "query": query,
            "filter": {"property": "object", "value": "database"},
            "page_size": page_size,
        }
        result = self._request("POST", "/search", payload)
        return result.get("results", [])

    def query_database(self, database_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        result = self._request("POST", f"/databases/{database_id}/query", payload)
        return result.get("results", [])

    def query_by_title(self, database_id: str, title_property: str, title: str) -> list[dict[str, Any]]:
        payload = {
            "filter": {
                "property": title_property,
                "title": {"equals": title},
            },
            "page_size": 10,
        }
        return self.query_database(database_id, payload)

    def query_by_title_contains(self, database_id: str, title_property: str, value: str) -> list[dict[str, Any]]:
        payload = {
            "filter": {
                "property": title_property,
                "title": {"contains": value},
            },
            "page_size": 25,
        }
        return self.query_database(database_id, payload)

    def create_page(self, database_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        payload = {"parent": {"database_id": database_id}, "properties": properties}
        return self._request("POST", "/pages", payload)

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/pages/{page_id}", {"properties": properties})


@dataclasses.dataclass
class SyncRow:
    title: str
    due_date: str | None
    customer_project: str | None
    task_type: str | None
    priority: str | None
    result: str


@dataclasses.dataclass
class SyncResult:
    created: int
    updated: int
    skipped: int
    summary_rows: list[SyncRow]
    unmapped_client_project: list[tuple[str, str]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract tasks from text and sync them to Notion.")
    parser.add_argument("--text", help="Direct text input.")
    parser.add_argument("--input-file", help="Path to a text file with notes.")
    parser.add_argument("--database-id", help="Notion database ID. Defaults to NOTION_DATABASE_ID.")
    parser.add_argument(
        "--database-name",
        default="Oppgaver",
        help="Database name for auto-lookup when database ID is not provided (default: Oppgaver).",
    )
    parser.add_argument("--token", help="Notion API token. Defaults to NOTION_API_TOKEN.")
    parser.add_argument("--title-property", default="Oppgave", help="Title property name in the database.")
    parser.add_argument("--status-property", default="Status", help="Status/select property name.")
    parser.add_argument("--due-property", default="Frist", help="Due date property name.")
    parser.add_argument("--source-property", default="Source", help="Source note property name.")
    parser.add_argument(
        "--client-project-property",
        default="Kunde/Prosjekt",
        help="Relation property name for customer/project mapping.",
    )
    parser.add_argument("--type-property", default="🏷️ Type", help="Type property name (select or multi_select).")
    parser.add_argument("--priority-property", default="🔥 Prioritet", help="Priority property name.")
    parser.add_argument("--status-value", default="Ikke startet", help="Status for newly created tasks.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip existing matching titles.")
    parser.add_argument("--max-tasks", type=int, default=25, help="Maximum tasks to extract.")
    parser.add_argument(
        "--extractor",
        choices=("rules", "ai", "hybrid"),
        default="rules",
        help="Task extraction mode. Local AI modes are disabled by workspace policy.",
    )
    parser.add_argument(
        "--openai-api-key",
        help="OpenAI API key. Defaults to OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--openai-model",
        default=DEFAULT_OPENAI_MODEL,
        help=f"OpenAI model for AI extraction (default: {DEFAULT_OPENAI_MODEL}).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only print extracted tasks.")
    parser.add_argument("--verbose", action="store_true", help="Print debug details.")
    return parser.parse_args()


def read_input_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text.strip()
    if args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    if not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if data:
            return data
    raise SystemExit("Provide input via --text, --input-file, or stdin.")


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


ACTION_HINTS = (
    "todo",
    "to do",
    "task",
    "oppgave",
    "action",
    "follow up",
    "folges opp",
    "ma ",
    "må ",
    "skal ",
    "bør ",
    "setter av tid til å",
    "tar ansvar for å",
    "ansvar for å",
    "need to",
    "must ",
    "deadline",
    "frist",
)


def looks_actionable(line: str) -> bool:
    low = line.lower()
    if any(hint in low for hint in ACTION_HINTS):
        return True
    if re.search(r"\b(?:jacob|vi|jeg|teamet|wal)\s+(?:skal|må|ma|bør)\b", low):
        return True
    if re.search(r"\b(?:jacob|vi|jeg|teamet|wal)\s+(?:tar ansvar for å|setter av tid til å)\b", low):
        return True
    if re.match(r"^(?:\d+[.)]|[-*])\s+", low):
        return True
    if re.match(r"^(?:todo|oppgave|action)\s*[:\-]", low):
        return True
    return False


def clean_bullet_prefix(line: str) -> str:
    line = re.sub(r"^\s*(?:[-*])\s+", "", line)
    line = re.sub(r"^\s*\d+[.)]\s+", "", line)
    return normalize_space(line)


def split_candidates(text: str) -> list[str]:
    lines = [clean_bullet_prefix(l) for l in text.splitlines()]
    lines = [l for l in lines if l]
    line_candidates: list[str] = []
    sentence_candidates: list[str] = []

    for line in lines:
        # Handle "Name: action item..." meeting-style notation.
        if ":" in line:
            left, right = line.split(":", 1)
            if len(left.split()) <= 3 and looks_actionable(right.strip()):
                line_candidates.append(normalize_space(right))
                continue
        if looks_actionable(line):
            line_candidates.append(line)

    # Sentence-level scan catches multiple actions packed into one long line/paragraph.
    sentence_chunks = re.split(r"(?<=[.!?])\s+|[;\n]+|(?:\s+[—–-]\s+)", text)
    for chunk in sentence_chunks:
        chunk = normalize_space(clean_bullet_prefix(chunk))
        if not chunk:
            continue
        if looks_actionable(chunk):
            sentence_candidates.append(chunk)

    if line_candidates:
        long_line_mode = any(len(candidate) > 220 for candidate in line_candidates)
        if long_line_mode and len(sentence_candidates) > len(line_candidates):
            candidates = sentence_candidates
        else:
            candidates = line_candidates
    else:
        candidates = sentence_candidates

    if candidates:
        return candidates

    fallback = normalize_space(text)
    return [fallback] if fallback else []


WEEKDAY_ALIASES = {
    "mandag": 0,
    "monday": 0,
    "tirsdag": 1,
    "tuesday": 1,
    "onsdag": 2,
    "wednesday": 2,
    "torsdag": 3,
    "thursday": 3,
    "fredag": 4,
    "friday": 4,
    "lordag": 5,
    "loredag": 5,
    "saturday": 5,
    "sondag": 6,
    "sunday": 6,
}


def parse_explicit_date(fragment: str, today: dt.date) -> dt.date | None:
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", fragment)
    if m:
        year, month, day = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return safe_date(year, month, day)

    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", fragment)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year_part = m.group(3)
        if year_part is None:
            year = today.year
        elif len(year_part) == 2:
            year = 2000 + int(year_part)
        else:
            year = int(year_part)
        parsed = safe_date(year, month, day)
        if parsed and parsed < today and year_part is None:
            parsed = safe_date(year + 1, month, day)
        return parsed

    return None


def safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def next_weekday(base: dt.date, target_weekday: int, include_today: bool = True) -> dt.date:
    days_ahead = target_weekday - base.weekday()
    if days_ahead < 0 or (days_ahead == 0 and not include_today):
        days_ahead += 7
    return base + dt.timedelta(days=days_ahead)


def parse_relative_date(fragment: str, today: dt.date) -> dt.date | None:
    low = fragment.lower()
    if "i morgen" in low or "imorgen" in low or "tomorrow" in low:
        return today + dt.timedelta(days=1)
    if "i dag" in low or "today" in low:
        return today
    if "neste uke" in low or "next week" in low:
        next_monday = next_weekday(today, 0, include_today=False)
        if next_monday <= today:
            next_monday += dt.timedelta(days=7)
        return next_monday

    for alias, weekday in WEEKDAY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            include_today = "i dag" in low or "today" in low
            result = next_weekday(today, weekday, include_today=include_today)
            if "neste uke" in low or "next week" in low:
                result += dt.timedelta(days=7)
            return result
    return None


def extract_due(text: str, today: dt.date) -> tuple[str | None, str | None]:
    low = text.lower()

    explicit = parse_explicit_date(low, today)
    if explicit:
        match = re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", low)
        phrase = match.group(0) if match else None
        return explicit.isoformat(), phrase

    deadline_patterns = (
        r"\b(?:deadline|frist)\s*(?:er|:)?\s*([^,.;\n]+)",
        r"\b(?:innen|by|before)\s+([^,.;\n]+)",
    )
    for pat in deadline_patterns:
        match = re.search(pat, low)
        if not match:
            continue
        phrase = normalize_space(match.group(0))
        parsed = parse_relative_date(match.group(1), today) or parse_explicit_date(match.group(1), today)
        if parsed:
            return parsed.isoformat(), phrase

    parsed_relative = parse_relative_date(low, today)
    if parsed_relative:
        return parsed_relative.isoformat(), None

    return None, None


def clean_task_title(text: str, due_phrase: str | None) -> str:
    title = normalize_space(text)

    # Prefer the actionable clause when a sentence has context + action joined by "og".
    actionable_tail = re.search(
        r",\s+og\s+(.+\b(?:skal|må|ma|bør|tar ansvar for å|setter av tid til å)\b.+)$",
        title,
        flags=re.IGNORECASE,
    )
    if actionable_tail:
        title = normalize_space(actionable_tail.group(1))

    if due_phrase:
        title = re.sub(re.escape(due_phrase), "", title, flags=re.IGNORECASE)

    title = re.sub(r"^(?:todo|action|oppgave)\s*[:\-]\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^(?:det ble enighet om at)\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(deadline|frist)\b\s*[:\-]?", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(i morgen|imorgen|tomorrow|i dag|today|neste uke|next week)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(innen|by|before)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(jeg|vi)\s+(ma|må|skal)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(?:jacob|vi|jeg|teamet|wal)\s+skal\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(?:jacob|vi|jeg|teamet|wal)\s+(?:må|ma|bør)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(?:jacob|vi|jeg|teamet|wal)\s+tar ansvar for å\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(?:jacob|vi|jeg|teamet|wal)\s+setter av tid til å\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^[,\-:\s]+|[,\-:\s]+$", "", title)
    title = normalize_space(title)

    if len(title) > 180:
        title = f"{title[:177].rstrip()}..."
    return title


def extract_tasks_rules(raw_text: str, max_tasks: int) -> list[Task]:
    today = dt.date.today()
    candidates = split_candidates(raw_text)
    tasks: list[Task] = []
    seen: set[str] = set()

    for candidate in candidates:
        due_date, due_phrase = extract_due(candidate, today)
        title = clean_task_title(candidate, due_phrase)
        if len(title) < 3:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        tasks.append(
            Task(
                title=title,
                due_date=due_date,
                source_excerpt=normalize_space(candidate)[:1800],
                due_phrase=due_phrase,
            )
        )
        if len(tasks) >= max_tasks:
            break

    return tasks


def is_iso_date(value: str | None) -> bool:
    if not value:
        return False
    try:
        dt.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def openai_extract_tasks(
    raw_text: str,
    max_tasks: int,
    api_key: str,
    model: str,
) -> list[Task]:
    today = dt.date.today()
    system_message = (
        "You extract actionable TODOs from meeting notes in Norwegian or English. "
        "Return strict JSON object with key 'tasks' (array). "
        "Each task object must contain: "
        "title (string), due_date (YYYY-MM-DD or null), source_excerpt (string). "
        "Only include concrete action items. No summaries."
    )
    user_message = (
        f"Today's date is {today.isoformat()}.\n"
        f"Maximum tasks: {max_tasks}.\n"
        "If a relative date appears (e.g. i morgen, neste torsdag, next Friday), "
        "resolve it to YYYY-MM-DD.\n"
        "Keep titles short and imperative.\n\n"
        "Text:\n"
        f"{raw_text}"
    )

    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
    }
    req = urllib.request.Request(
        url=f"{OPENAI_API_BASE}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling OpenAI API: {exc.reason}") from exc

    choices = parsed.get("choices", [])
    if not choices:
        return []
    content = choices[0].get("message", {}).get("content", "")
    if not content:
        return []

    try:
        content_json = json.loads(content)
    except json.JSONDecodeError:
        return []

    raw_tasks = content_json.get("tasks", [])
    if not isinstance(raw_tasks, list):
        return []

    tasks: list[Task] = []
    seen: set[str] = set()
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        raw_title = normalize_space(str(item.get("title", "")))
        if len(raw_title) < 3:
            continue

        source_excerpt = normalize_space(str(item.get("source_excerpt") or raw_title))[:1800]
        due_date = item.get("due_date")
        due_date_str = str(due_date) if due_date is not None else None
        if not is_iso_date(due_date_str):
            due_date_str = None
            fallback_due, _ = extract_due(source_excerpt, today)
            due_date_str = fallback_due

        title = clean_task_title(raw_title, due_phrase=None)
        if len(title) < 3:
            continue
        key = f"{title.lower()}|{due_date_str or ''}"
        if key in seen:
            continue
        seen.add(key)
        tasks.append(
            Task(
                title=title,
                due_date=due_date_str,
                source_excerpt=source_excerpt,
                due_phrase=None,
            )
        )
        if len(tasks) >= max_tasks:
            break
    return tasks


def merge_tasks(tasks: list[Task], max_tasks: int) -> list[Task]:
    merged: list[Task] = []
    seen: set[str] = set()
    for task in tasks:
        key = f"{task.title.lower()}|{task.due_date or ''}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(task)
        if len(merged) >= max_tasks:
            break
    return merged


def extract_tasks(
    raw_text: str,
    max_tasks: int,
    extractor: str = "rules",
    openai_api_key: str | None = None,
    openai_model: str = DEFAULT_OPENAI_MODEL,
    verbose: bool = False,
) -> list[Task]:
    extractor_mode = extractor.strip().lower()
    if extractor_mode not in {"rules", "ai", "hybrid"}:
        raise SystemExit(f"Unsupported extractor mode: {extractor}")

    if extractor_mode in {"ai", "hybrid"}:
        raise SystemExit(
            "Local AI extraction is disabled by workspace policy. "
            "Use agent-driven extraction instead."
        )

    rule_tasks: list[Task] = []

    if extractor_mode == "rules":
        rule_tasks = extract_tasks_rules(raw_text, max_tasks=max_tasks)
    return merge_tasks(rule_tasks, max_tasks=max_tasks)


def ensure_property_type(schema: dict[str, Any], name: str, allowed_types: set[str], required: bool = False) -> str | None:
    prop = schema.get(name)
    if prop is None:
        if required:
            raise SystemExit(f"Missing required property '{name}' in Notion database.")
        return None
    prop_type = prop.get("type")
    if prop_type not in allowed_types:
        allowed = ", ".join(sorted(allowed_types))
        raise SystemExit(f"Property '{name}' must be one of [{allowed}], got '{prop_type}'.")
    return prop_type


def normalized_name(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def find_property_by_alias(schema: dict[str, Any], aliases: tuple[str, ...], allowed_types: set[str]) -> str | None:
    normalized_to_name = {normalized_name(name): name for name in schema}
    for alias in aliases:
        name = normalized_to_name.get(normalized_name(alias))
        if not name:
            continue
        prop_type = schema[name].get("type")
        if prop_type in allowed_types:
            return name
    return None


def first_property_of_type(schema: dict[str, Any], allowed_types: set[str]) -> str | None:
    for name, prop in schema.items():
        if prop.get("type") in allowed_types:
            return name
    return None


def resolve_property_name(
    schema: dict[str, Any],
    requested: str,
    allowed_types: set[str],
    aliases: tuple[str, ...],
    required: bool = False,
    allow_type_fallback: bool = True,
) -> str | None:
    if requested in schema:
        ensure_property_type(schema, requested, allowed_types, required=required)
        return requested

    by_alias = find_property_by_alias(schema, (requested,) + aliases, allowed_types)
    if by_alias:
        return by_alias

    if allow_type_fallback:
        by_type = first_property_of_type(schema, allowed_types)
        if by_type:
            return by_type

    if required:
        allowed = ", ".join(sorted(allowed_types))
        raise SystemExit(
            f"Could not find required property for types [{allowed}] "
            f"(requested '{requested}', aliases: {', '.join(aliases)})."
        )
    return None


def get_status_options(schema: dict[str, Any], status_property: str | None) -> list[str]:
    if not status_property:
        return []
    prop = schema.get(status_property, {})
    prop_type = prop.get("type")
    if prop_type == "status":
        options = prop.get("status", {}).get("options", [])
        return [o.get("name") for o in options if o.get("name")]
    if prop_type == "select":
        options = prop.get("select", {}).get("options", [])
        return [o.get("name") for o in options if o.get("name")]
    return []


def get_property_options(schema: dict[str, Any], property_name: str | None) -> list[str]:
    if not property_name:
        return []
    prop = schema.get(property_name, {})
    prop_type = prop.get("type")
    if prop_type == "status":
        options = prop.get("status", {}).get("options", [])
        return [o.get("name") for o in options if o.get("name")]
    if prop_type == "select":
        options = prop.get("select", {}).get("options", [])
        return [o.get("name") for o in options if o.get("name")]
    if prop_type == "multi_select":
        options = prop.get("multi_select", {}).get("options", [])
        return [o.get("name") for o in options if o.get("name")]
    return []


def resolve_status_value(schema: dict[str, Any], status_property: str | None, requested_value: str) -> str | None:
    options = get_status_options(schema, status_property)
    if not status_property:
        return None
    if not options:
        return requested_value
    if requested_value in options:
        return requested_value

    normalized = {normalized_name(opt): opt for opt in options}
    preferred = (
        requested_value,
        "Ikke startet",
        "Not started",
        "To do",
        "Pågår",
        "In progress",
    )
    for candidate in preferred:
        match = normalized.get(normalized_name(candidate))
        if match:
            return match
    return options[0]


def infer_task_type_key(task: Task) -> str:
    text = f"{task.title} {task.source_excerpt}".casefold()

    if any(token in text for token in ("møte", "mote", "meeting", "booke", "book ", "strategimøte", "strategimote")):
        return "mote"
    if any(token in text for token in ("rapport", "report", "resultat", "innsikt", "kampanjeresultat")):
        return "rapportering"
    if any(token in text for token in ("instagram", "tiktok", "kreativ", "creative", "utkast", "format", "innhold")):
        return "kreativt"
    if any(token in text for token in ("meta", "annonse", "annonser", "ads", "kampanje", "kampanjer")):
        return "annonsering"
    if any(token in text for token in ("strategi", "strategy", "q1", "q2", "q3", "q4", "roadmap", "plan")):
        return "strategi"
    if any(token in text for token in ("admin", "kundelogg", "system", "crm", "opprydding")):
        return "admin"
    return "admin"


def infer_priority_key(task: Task) -> str:
    text = f"{task.title} {task.source_excerpt}".casefold()

    if any(token in text for token in ("lavere prioritet", "lav prioritet", "low priority", "når det passer", "i løpet av måneden")):
        return "low"
    if any(token in text for token in ("haster", "asap", "urgent", "kritisk", "så fort som mulig")):
        return "high"

    if task.due_date:
        try:
            due = dt.date.fromisoformat(task.due_date)
            delta_days = (due - dt.date.today()).days
            if delta_days <= 2:
                return "high"
            if delta_days <= 10:
                return "medium"
            return "low"
        except ValueError:
            pass

    return "medium"


def option_matches(option: str, keywords: tuple[str, ...]) -> bool:
    norm = normalized_name(option)
    return any(normalized_name(k) in norm for k in keywords)


def resolve_type_value(schema: dict[str, Any], type_property: str | None, task: Task) -> str | None:
    if not type_property:
        return None
    options = get_property_options(schema, type_property)
    if not options:
        return None

    key = infer_task_type_key(task)
    keyword_map: dict[str, tuple[str, ...]] = {
        "annonsering": ("annonsering", "annonse", "ads", "meta", "kampanje"),
        "rapportering": ("rapportering", "rapport", "report", "resultat"),
        "kreativt": ("kreativt", "kreativ", "creative", "design"),
        "strategi": ("strategi", "strategy", "plan"),
        "mote": ("møte", "mote", "meeting", "sync"),
        "admin": ("admin", "ops", "drift", "system"),
    }

    for option in options:
        if option_matches(option, keyword_map.get(key, ())):
            return option

    fallback_order = ("annonsering", "rapportering", "kreativt", "strategi", "mote", "admin")
    for fallback_key in fallback_order:
        for option in options:
            if option_matches(option, keyword_map[fallback_key]):
                return option
    return options[0]


def resolve_priority_value(schema: dict[str, Any], priority_property: str | None, task: Task) -> str | None:
    if not priority_property:
        return None
    options = get_property_options(schema, priority_property)
    if not options:
        return None

    key = infer_priority_key(task)
    keyword_map: dict[str, tuple[str, ...]] = {
        "high": ("høy", "hoy", "high", "urgent", "kritisk"),
        "medium": ("medium", "middels", "normal"),
        "low": ("lav", "low", "minor"),
    }

    for option in options:
        if option_matches(option, keyword_map[key]):
            return option

    for fallback_key in ("high", "medium", "low"):
        for option in options:
            if option_matches(option, keyword_map[fallback_key]):
                return option
    return options[0]


CLIENT_PROJECT_STOPWORDS = {
    "dag",
    "today",
    "tomorrow",
    "imorgen",
    "morgen",
    "deadline",
    "frist",
    "uke",
    "next",
    "neste",
    "mandag",
    "tirsdag",
    "onsdag",
    "torsdag",
    "fredag",
    "lordag",
    "sondag",
}


def clean_client_project_candidate(raw: str) -> str | None:
    value = normalize_space(raw.strip("()[]{}\"'"))
    value = re.split(r"[,.;\n]", value, maxsplit=1)[0]
    value = re.sub(r"^(?:kunde|prosjekt)\s*[:\-]?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\b(?:deadline|frist|innen|by|before|i morgen|imorgen|tomorrow|i dag|today|neste uke|next week)\b.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = value.strip(" -:")
    if len(value) < 2 or len(value) > 80:
        return None
    if normalized_name(value) in CLIENT_PROJECT_STOPWORDS:
        return None
    return value


def extract_client_project_candidates(task: Task) -> list[str]:
    text = f"{task.source_excerpt} {task.title}"
    candidates: list[str] = []

    patterns = (
        r"\(([^()]{2,80})\)",
        r"\bfor\s+([^,.;\n]+)",
        r"\btil\s+([^,.;\n]+)",
        r"\b(?:kunde|prosjekt)\s*[:\-]?\s*([^,.;\n]+)",
    )

    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            cleaned = clean_client_project_candidate(match.group(1))
            if cleaned:
                candidates.append(cleaned)

    tail_match = re.search(r"(?:-|/)\s*([A-Za-z0-9][A-Za-z0-9 &/_-]{1,60})$", task.title)
    if tail_match:
        cleaned = clean_client_project_candidate(tail_match.group(1))
        if cleaned:
            candidates.append(cleaned)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = normalized_name(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def extract_page_title(result: dict[str, Any], title_property: str) -> str:
    properties = result.get("properties", {})
    if not isinstance(properties, dict):
        return ""
    prop = properties.get(title_property, {})
    parts = prop.get("title", []) if isinstance(prop, dict) else []
    rendered: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("plain_text")
        if not text:
            text = part.get("text", {}).get("content", "")
        if text:
            rendered.append(str(text))
    return "".join(rendered).strip()


def choose_best_title_match(results: list[dict[str, Any]], title_property: str, candidate: str) -> tuple[str, str] | None:
    if not results:
        return None

    candidate_key = normalized_name(candidate)
    best = results[0]
    for result in results:
        title = extract_page_title(result, title_property)
        if normalized_name(title) == candidate_key:
            best = result
            break

    page_id = best.get("id")
    if not page_id:
        return None
    best_title = extract_page_title(best, title_property) or candidate
    return str(page_id), best_title


def resolve_relation_target(
    client: NotionClient,
    relation_database_id: str,
    relation_title_property: str,
    candidates: list[str],
    cache: dict[tuple[str, str], tuple[str, str] | None],
) -> tuple[str, str, str] | None:
    for candidate in candidates:
        key = (relation_database_id, normalized_name(candidate))
        if key in cache:
            cached = cache[key]
            if cached is None:
                continue
            return cached[0], cached[1], candidate

        exact_results = client.query_by_title(relation_database_id, relation_title_property, candidate)
        match = choose_best_title_match(exact_results, relation_title_property, candidate)
        if not match:
            contains_results = client.query_by_title_contains(relation_database_id, relation_title_property, candidate)
            match = choose_best_title_match(contains_results, relation_title_property, candidate)

        cache[key] = match
        if match:
            return match[0], match[1], candidate

    return None


def strip_customer_from_title(title: str, names: list[str]) -> str:
    updated = normalize_space(title)
    unique_names: list[str] = []
    seen: set[str] = set()
    for name in names:
        norm = normalized_name(name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        unique_names.append(name)

    for name in sorted(unique_names, key=len, reverse=True):
        escaped = re.escape(name)
        updated = re.sub(rf"\s*\(\s*{escaped}\s*\)", "", updated, flags=re.IGNORECASE)
        updated = re.sub(rf"\s*\[\s*{escaped}\s*\]", "", updated, flags=re.IGNORECASE)
        updated = re.sub(rf"\bfor\s+{escaped}\b", "", updated, flags=re.IGNORECASE)
        updated = re.sub(rf"\btil\s+{escaped}\b", "", updated, flags=re.IGNORECASE)
        updated = re.sub(rf"\s*-\s*{escaped}\s*$", "", updated, flags=re.IGNORECASE)

    updated = re.sub(r"\s{2,}", " ", updated)
    updated = re.sub(r"\s+([,.;:!?])", r"\1", updated)
    updated = re.sub(r"\(\s*\)", "", updated)
    updated = updated.strip(" -,:;")
    return normalize_space(updated)


def build_create_properties(
    task: Task,
    schema: dict[str, Any],
    title_property: str,
    status_property: str | None,
    due_property: str | None,
    source_property: str | None,
    client_project_property: str | None,
    client_project_page_id: str | None,
    type_property: str | None,
    type_value: str | None,
    priority_property: str | None,
    priority_value: str | None,
    status_value: str | None,
) -> dict[str, Any]:
    props: dict[str, Any] = {}

    title_type = ensure_property_type(schema, title_property, {"title"}, required=True)
    if title_type == "title":
        props[title_property] = {"title": [{"text": {"content": task.title}}]}

    if status_property and status_value:
        status_type = ensure_property_type(schema, status_property, {"status", "select"})
        if status_type == "status":
            props[status_property] = {"status": {"name": status_value}}
        elif status_type == "select":
            props[status_property] = {"select": {"name": status_value}}

    if due_property and task.due_date:
        due_type = ensure_property_type(schema, due_property, {"date"})
        if due_type == "date":
            props[due_property] = {"date": {"start": task.due_date}}

    if source_property:
        source_type = ensure_property_type(schema, source_property, {"rich_text", "title"})
        if source_type == "rich_text":
            props[source_property] = {"rich_text": [{"text": {"content": task.source_excerpt}}]}
        elif source_type == "title":
            props[source_property] = {"title": [{"text": {"content": task.source_excerpt}}]}

    if client_project_property and client_project_page_id:
        relation_type = ensure_property_type(schema, client_project_property, {"relation"})
        if relation_type == "relation":
            props[client_project_property] = {"relation": [{"id": client_project_page_id}]}

    if type_property and type_value:
        type_prop_type = ensure_property_type(schema, type_property, {"select", "multi_select"})
        if type_prop_type == "select":
            props[type_property] = {"select": {"name": type_value}}
        elif type_prop_type == "multi_select":
            props[type_property] = {"multi_select": [{"name": type_value}]}

    if priority_property and priority_value:
        priority_prop_type = ensure_property_type(schema, priority_property, {"select", "status"})
        if priority_prop_type == "select":
            props[priority_property] = {"select": {"name": priority_value}}
        elif priority_prop_type == "status":
            props[priority_property] = {"status": {"name": priority_value}}

    return props


def build_update_properties(
    task: Task,
    schema: dict[str, Any],
    due_property: str | None,
    source_property: str | None,
    client_project_property: str | None,
    client_project_page_id: str | None,
    type_property: str | None,
    type_value: str | None,
    priority_property: str | None,
    priority_value: str | None,
) -> dict[str, Any]:
    props: dict[str, Any] = {}

    if due_property and task.due_date:
        due_type = ensure_property_type(schema, due_property, {"date"})
        if due_type == "date":
            props[due_property] = {"date": {"start": task.due_date}}

    if source_property:
        source_type = ensure_property_type(schema, source_property, {"rich_text", "title"})
        if source_type == "rich_text":
            props[source_property] = {"rich_text": [{"text": {"content": task.source_excerpt}}]}
        elif source_type == "title":
            props[source_property] = {"title": [{"text": {"content": task.source_excerpt}}]}

    if client_project_property and client_project_page_id:
        relation_type = ensure_property_type(schema, client_project_property, {"relation"})
        if relation_type == "relation":
            props[client_project_property] = {"relation": [{"id": client_project_page_id}]}

    if type_property and type_value:
        type_prop_type = ensure_property_type(schema, type_property, {"select", "multi_select"})
        if type_prop_type == "select":
            props[type_property] = {"select": {"name": type_value}}
        elif type_prop_type == "multi_select":
            props[type_property] = {"multi_select": [{"name": type_value}]}

    if priority_property and priority_value:
        priority_prop_type = ensure_property_type(schema, priority_property, {"select", "status"})
        if priority_prop_type == "select":
            props[priority_property] = {"select": {"name": priority_value}}
        elif priority_prop_type == "status":
            props[priority_property] = {"status": {"name": priority_value}}

    return props


def print_preview(tasks: list[Task]) -> None:
    if not tasks:
        print("No tasks extracted.")
        return

    print(f"Extracted {len(tasks)} task(s):")
    for idx, task in enumerate(tasks, start=1):
        due = task.due_date or "-"
        print(f"{idx:02d}. {task.title}")
        print(f"    due: {due}")


def print_visual_summary(rows: list[SyncRow]) -> None:
    if not rows:
        return
    print("")
    print("Oppsummering:")
    print("| Oppgave | Frist | Type | Prioritet | Kunde/Prosjekt | Resultat |")
    print("| --- | --- | --- | --- | --- | --- |")
    for row in rows:
        due = row.due_date or "-"
        task_type = row.task_type or "-"
        priority = row.priority or "-"
        customer = row.customer_project or "-"
        print(f"| {row.title} | {due} | {task_type} | {priority} | {customer} | {row.result} |")


def page_property_has_value(page: dict[str, Any], property_name: str | None) -> bool:
    if not property_name:
        return False
    properties = page.get("properties", {})
    if not isinstance(properties, dict):
        return False
    prop = properties.get(property_name, {})
    if not isinstance(prop, dict):
        return False
    prop_type = prop.get("type")
    if prop_type == "select":
        return prop.get("select") is not None
    if prop_type == "status":
        return prop.get("status") is not None
    if prop_type == "multi_select":
        return bool(prop.get("multi_select"))
    if prop_type == "relation":
        return bool(prop.get("relation"))
    if prop_type == "date":
        return prop.get("date") is not None
    if prop_type == "rich_text":
        return bool(prop.get("rich_text"))
    if prop_type == "title":
        return bool(prop.get("title"))
    return False


def require_not_empty(value: str | None, env_name: str) -> str:
    if value:
        return value
    env_val = os.getenv(env_name)
    if env_val:
        return env_val
    raise SystemExit(f"Missing required value. Provide flag or set environment variable: {env_name}")


def rich_text_to_plain(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    rendered: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("plain_text")
        if not text:
            text = part.get("text", {}).get("content", "")
        if text:
            rendered.append(str(text))
    return "".join(rendered).strip()


def extract_database_title(database_obj: dict[str, Any]) -> str:
    return rich_text_to_plain(database_obj.get("title", []))


def resolve_database_id(args: argparse.Namespace, client: NotionClient) -> str:
    explicit_id = args.database_id or os.getenv("NOTION_DATABASE_ID")
    if explicit_id:
        return explicit_id

    lookup_name = args.database_name or os.getenv("NOTION_DATABASE_NAME") or "Oppgaver"
    search_results = client.search_databases(query=lookup_name, page_size=25)
    if not search_results:
        raise SystemExit(
            "Missing Notion database ID and no database found by auto-lookup. "
            f"Tried name '{lookup_name}'. Provide --database-id or set NOTION_DATABASE_ID."
        )

    exact_matches = [
        db for db in search_results if normalized_name(extract_database_title(db)) == normalized_name(lookup_name)
    ]
    if exact_matches:
        chosen = exact_matches[0]
    elif len(search_results) == 1:
        chosen = search_results[0]
    else:
        candidates = []
        for db in search_results[:8]:
            title = extract_database_title(db) or "(uten tittel)"
            db_id = str(db.get("id", ""))
            candidates.append(f"- {title} ({db_id})")
        joined_candidates = "\n".join(candidates)
        raise SystemExit(
            "Found multiple databases for auto-lookup name "
            f"'{lookup_name}'. Use --database-id or a more specific --database-name.\n"
            f"Candidates:\n{joined_candidates}"
        )

    db_id = str(chosen.get("id", "")).strip()
    if not db_id:
        raise SystemExit("Auto-lookup found a database but could not read its ID. Please provide --database-id.")
    if args.verbose:
        title = extract_database_title(chosen) or lookup_name
        print(f"Auto-selected database: '{title}' ({db_id})")
    return db_id


def sync_tasks(args: argparse.Namespace, tasks: list[Task], emit_output: bool = True) -> SyncResult:
    token = require_not_empty(args.token, "NOTION_API_TOKEN")
    client = NotionClient(token=token)
    database_id = resolve_database_id(args, client)
    db = client.get_database(database_id)
    schema = db.get("properties", {})
    if not isinstance(schema, dict):
        raise SystemExit("Could not read database properties from Notion response.")

    title_property = resolve_property_name(
        schema=schema,
        requested=args.title_property,
        allowed_types={"title"},
        aliases=("Oppgave", "Oppgavenavn", "Task", "Name", "Title"),
        required=True,
    )
    status_property = resolve_property_name(
        schema=schema,
        requested=args.status_property,
        allowed_types={"status", "select"},
        aliases=("Status", "State"),
        required=False,
    )
    due_property = resolve_property_name(
        schema=schema,
        requested=args.due_property,
        allowed_types={"date"},
        aliases=("Frist", "Forfaller", "Deadline", "Due", "Due Date"),
        required=False,
    )
    source_property = resolve_property_name(
        schema=schema,
        requested=args.source_property,
        allowed_types={"rich_text", "title"},
        aliases=("Kilde", "Source", "Source Note", "Notat"),
        required=False,
        allow_type_fallback=False,
    )
    if source_property == title_property:
        source_property = None
    client_project_property = resolve_property_name(
        schema=schema,
        requested=args.client_project_property,
        allowed_types={"relation"},
        aliases=("Kunde/Prosjekt", "KundeProsjekt", "Client/Project", "Client Project"),
        required=False,
    )
    status_value = resolve_status_value(schema, status_property, args.status_value)
    type_property = resolve_property_name(
        schema=schema,
        requested=args.type_property,
        allowed_types={"select", "multi_select"},
        aliases=("🏷️ Type", "Type", "Kategori", "Category"),
        required=False,
    )
    priority_property = resolve_property_name(
        schema=schema,
        requested=args.priority_property,
        allowed_types={"select", "status"},
        aliases=("🔥 Prioritet", "Prioritet", "Priority"),
        required=False,
    )

    relation_database_id: str | None = None
    relation_title_property: str | None = None
    if client_project_property:
        relation_config = schema.get(client_project_property, {}).get("relation", {})
        if isinstance(relation_config, dict):
            relation_database_id = relation_config.get("database_id")
        if relation_database_id:
            relation_db = client.get_database(relation_database_id)
            relation_schema = relation_db.get("properties", {})
            if isinstance(relation_schema, dict):
                relation_title_property = first_property_of_type(relation_schema, {"title"})

    if args.verbose:
        print(
            "Using properties: "
            f"title='{title_property}', status='{status_property}', due='{due_property}', "
            f"source='{source_property}', client_project='{client_project_property}', "
            f"type='{type_property}', priority='{priority_property}'"
        )
        if status_property:
            print(f"Using status value: {status_value}")
        if relation_database_id:
            print(
                "Using relation database: "
                f"id='{relation_database_id}', title_property='{relation_title_property}'"
            )

    created = 0
    updated = 0
    skipped = 0
    relation_cache: dict[tuple[str, str], tuple[str, str] | None] = {}
    unmapped_client_project: list[tuple[str, str]] = []
    summary_rows: list[SyncRow] = []

    for task in tasks:
        mapped_client_project_id: str | None = None
        mapped_client_project_title: str | None = None
        guessed_type_value = resolve_type_value(schema, type_property, task)
        guessed_priority_value = resolve_priority_value(schema, priority_property, task)
        client_candidates: list[str] = []
        if client_project_property and relation_database_id and relation_title_property:
            client_candidates = extract_client_project_candidates(task)
            resolved = resolve_relation_target(
                client=client,
                relation_database_id=relation_database_id,
                relation_title_property=relation_title_property,
                candidates=client_candidates,
                cache=relation_cache,
            )
            if resolved:
                mapped_client_project_id, mapped_client_project_title, _ = resolved
            else:
                hint = client_candidates[0] if client_candidates else "ingen kandidat funnet i teksten"
                unmapped_client_project.append((task.title, hint))

        adjusted_title = task.title
        if mapped_client_project_title:
            adjusted_title = strip_customer_from_title(
                adjusted_title,
                [mapped_client_project_title, *client_candidates],
            )
            if len(adjusted_title) >= 3:
                task.title = adjusted_title

        existing = client.query_by_title(database_id, title_property, task.title)
        if existing:
            if args.skip_existing:
                skipped += 1
                if args.verbose:
                    print(f"Skipped existing task: {task.title}")
                summary_rows.append(
                    SyncRow(
                        title=task.title,
                        due_date=task.due_date,
                        customer_project=mapped_client_project_title,
                        task_type=guessed_type_value,
                        priority=guessed_priority_value,
                        result="Skippet",
                    )
                )
                continue
            existing_page = existing[0]
            update_type_value = guessed_type_value if not page_property_has_value(existing_page, type_property) else None
            update_priority_value = guessed_priority_value if not page_property_has_value(existing_page, priority_property) else None
            update_props = build_update_properties(
                task,
                schema,
                due_property,
                source_property,
                client_project_property,
                mapped_client_project_id,
                type_property,
                update_type_value,
                priority_property,
                update_priority_value,
            )
            if update_props:
                client.update_page(existing[0]["id"], update_props)
                updated += 1
                if args.verbose:
                    print(
                        f"Updated existing task: {task.title}"
                        + (
                            f" (kunde/prosjekt: {mapped_client_project_title})"
                            if mapped_client_project_title
                            else ""
                        )
                    )
                summary_rows.append(
                    SyncRow(
                        title=task.title,
                        due_date=task.due_date,
                        customer_project=mapped_client_project_title,
                        task_type=guessed_type_value,
                        priority=guessed_priority_value,
                        result="Oppdatert",
                    )
                )
            else:
                skipped += 1
                summary_rows.append(
                    SyncRow(
                        title=task.title,
                        due_date=task.due_date,
                        customer_project=mapped_client_project_title,
                        task_type=guessed_type_value,
                        priority=guessed_priority_value,
                        result="Skippet",
                    )
                )
            continue

        create_props = build_create_properties(
            task=task,
            schema=schema,
            title_property=title_property,
            status_property=status_property,
            due_property=due_property,
            source_property=source_property,
            client_project_property=client_project_property,
            client_project_page_id=mapped_client_project_id,
            type_property=type_property,
            type_value=guessed_type_value,
            priority_property=priority_property,
            priority_value=guessed_priority_value,
            status_value=status_value,
        )
        client.create_page(database_id, create_props)
        created += 1
        if args.verbose:
            print(
                f"Created task: {task.title}"
                + (
                    f" (kunde/prosjekt: {mapped_client_project_title})"
                    if mapped_client_project_title
                    else ""
                )
            )
        summary_rows.append(
            SyncRow(
                title=task.title,
                due_date=task.due_date,
                customer_project=mapped_client_project_title,
                task_type=guessed_type_value,
                priority=guessed_priority_value,
                result="Opprettet",
            )
        )

    result = SyncResult(
        created=created,
        updated=updated,
        skipped=skipped,
        summary_rows=summary_rows,
        unmapped_client_project=unmapped_client_project,
    )

    if emit_output:
        print(f"Notion sync complete. Created: {created}, Updated: {updated}, Skipped: {skipped}.")
        print_visual_summary(summary_rows)
        if client_project_property:
            if unmapped_client_project:
                print("")
                print("Oppgaver lagt opp uten Kunde/Prosjekt-mapping:")
                for title, hint in unmapped_client_project:
                    print(f"- {title} (forsokte: {hint})")
                print("Vil du opprette manglende kunde/prosjekt i Notion, og koble disse oppgavene?")
        elif args.verbose:
            print("Kunde/Prosjekt-relasjon ble ikke funnet i databasen, mapping ble hoppet over.")

    return result


def main() -> None:
    args = parse_args()
    raw_text = read_input_text(args)
    tasks = extract_tasks(
        raw_text,
        max_tasks=args.max_tasks,
        extractor=args.extractor,
        openai_api_key=args.openai_api_key,
        openai_model=args.openai_model,
        verbose=args.verbose,
    )

    print_preview(tasks)
    if args.dry_run:
        print("Dry run complete.")
        return

    if not tasks:
        print("No tasks to sync.")
        return

    sync_tasks(args, tasks, emit_output=True)


if __name__ == "__main__":
    try:
        main()
    except NotionApiError as exc:
        raise SystemExit(str(exc)) from exc
