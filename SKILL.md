---
name: notion-todo-capture
description: Extract action items and deadlines from free text or meeting notes, then create or update tasks in a Notion database via the Notion API. Use when users ask to turn natural-language input into TODOs, capture meeting follow-ups, or sync quick notes like "jeg ma sette opp en kampanje, deadline i morgen" into structured Notion tasks.
---

# Notion Todo Capture

## Overview

Convert unstructured notes into clean task titles with optional due dates.

Default behavior:
- Write tasks directly to Notion (no dry-run) when the Notion connector is available.
- Use `--dry-run` only when the user explicitly asks for a preview.
- Prefer Notion connector tools over local CLI scripts to avoid `NOTION_API_TOKEN`/`NOTION_DATABASE_ID` dependency in chat runs.

Workspace defaults (use these unless user says otherwise):
- Oppgaver database: `https://www.notion.so/43c652ddc7bd46baab600da530187ddb`
- Oppgaver data source: `collection://a989beee-7cfb-4ce2-a3f8-d94e1fb4fa36`
- Kunde/Prosjekt relation source: `collection://29420ba2-39d8-4f55-8400-22f3d45afb59`

## Workflow

1. Read [notion-db-setup.md](references/notion-db-setup.md) if database properties are not configured yet.
2. Provide text input using one of:
- `--text "jeg ma sette opp en kampanje for ARK, deadline i morgen"`
- `--input-file /absolute/path/to/meeting-notes.txt`
- free text directly in chat
3. If Notion connector is available: create/update tasks directly in Notion data source `collection://a989beee-7cfb-4ce2-a3f8-d94e1fb4fa36` (no dry-run).
4. If connector is unavailable: fall back to CLI script with `NOTION_API_TOKEN` and `NOTION_DATABASE_ID`.
5. Re-run with `--skip-existing` only if existing titles should never be changed.

## Commands

Direct sync on short structured input (CLI fallback mode):
```bash
python3 scripts/capture_to_notion.py \
  --text "jeg ma sette opp en kampanje for ARK, deadline i morgen"
```

Direct sync on meeting notes (CLI fallback mode):
```bash
python3 scripts/capture_to_notion.py \
  --input-file /absolute/path/to/meeting-notes.txt
```

Dry-run preview (only when user explicitly asks):
```bash
python3 scripts/capture_to_notion.py \
  --input-file /absolute/path/to/meeting-notes.txt \
  --dry-run
```

CLI fallback credentials:
```bash
export NOTION_API_TOKEN="secret_xxx"
export NOTION_DATABASE_ID="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

python3 scripts/capture_to_notion.py \
  --input-file /absolute/path/to/meeting-notes.txt \
  --status-value "Ikke startet"
```

Use custom property names if the database differs from defaults:
```bash
python3 scripts/capture_to_notion.py \
  --text "send oppdatering til teamet innen fredag" \
  --title-property "Task" \
  --due-property "Deadline" \
  --status-property "State" \
  --source-property "Source Note" \
  --type-property "Category" \
  --priority-property "Priority" \
  --client-project-property "Client/Project"
```

Scheduled ingest from combined Slack/Outlook payload:
```bash
python3 scripts/scheduled_ingest.py \
  --events-file /absolute/path/to/combined-events.json \
  --job-label "12:00 run (man-fre)" \
  --strict-mode strict
```

Generate approval queue + Slack DM message for medium suggestions:
```bash
python3 scripts/scheduled_ingest.py \
  --events-file /absolute/path/to/combined-events.json \
  --strict-mode strict \
  --pending-suggestions-file /absolute/path/to/pending_suggestions.json \
  --approval-message-file /absolute/path/to/slack_approval_message.txt
```

Apply Slack JA/NEI decision to medium suggestions:
```bash
python3 scripts/apply_suggestion_decision.py \
  --pending-suggestions-file /absolute/path/to/pending_suggestions.json \
  --decision-text "JA run-20260403T120000Z-12-00-run 1,3"
```

Realtime approval listener (event-driven Slack reply handling):
```bash
python3 scripts/approval_listener.py \
  --event-file /absolute/path/to/slack-event.json \
  --pending-suggestions-file /absolute/path/to/pending_suggestions.json \
  --cursor-file /absolute/path/to/approval_cursor.json \
  --confirmation-message-file /absolute/path/to/approval_confirmation.txt
```

Always-on Slack webhook service (production realtime approvals):
```bash
python3 scripts/slack_approval_webhook.py \
  --host 0.0.0.0 \
  --port 8787 \
  --pending-suggestions-file /absolute/path/to/pending_suggestions.json \
  --cursor-file /absolute/path/to/approval_cursor.json \
  --confirmation-message-file /absolute/path/to/approval_confirmation_live.txt
```

Push local pending state to hosted webhook (small sync step):
```bash
python3 scripts/push_state_to_webhook.py \
  --webhook-base-url https://your-service.onrender.com \
  --sync-token "$STATE_SYNC_TOKEN" \
  --pending-suggestions-file /absolute/path/to/pending_suggestions.json \
  --include-cursor
```

## Behavior Notes

- Extract Norwegian and English relative dates (`i morgen`, `tomorrow`, weekday names, explicit dates like `2026-04-03` or `03.04.2026`).
- Normalize noisy phrasing like `jeg ma`, `vi skal`, and bullet prefixes into concise task titles.
- Never default to dry-run. Create/update in Notion directly unless the user asks for preview.
- Do not ask for `NOTION_API_TOKEN`/`NOTION_DATABASE_ID` if Notion connector is available in the current chat.
- Always attempt to map each task to `Kunde/Prosjekt` by reading hints like `for ARK` or `(ARK)` and matching relation entries.
- If customer/project mapping succeeds, remove customer/project name from task title before create/update (keep relation field as source of truth).
- Guess `Type` and `Prioritet` from content + due date and fill those fields automatically when available in the database.
- If no matching customer/project exists, still create the task and print a follow-up question asking whether the missing customer/project should be created.
- `scheduled_ingest.py` enforces strict filtering by default: only `high` confidence items are auto-upserted, `medium` are shown as suggestions, `low` are ignored.
- `scheduled_ingest.py` uses rules-based extraction in automation runs; AI classification is agent-driven in chat (no local API key flow).
- `scheduled_ingest.py` can persist `medium` suggestions in a queue file and generate a Slack-ready approval message with `JA/NEI` commands.
- `apply_suggestion_decision.py` applies `JA/NEI` decisions from Slack and syncs only approved suggestions to Notion.
- `approval_listener.py` is additive and processes thread replies in realtime (natural language approve/reject), then writes a confirmation message you can post back in the same Slack thread immediately.
- Upsert by exact title:
- If no match exists, create a new task.
- If a match exists, update due date/source unless `--skip-existing` is set.
- Keep timezone behavior local to the machine date when resolving relative dates.
- After sync, print a visual summary table with columns: `Oppgave`, `Frist`, `Type`, `Prioritet`, `Kunde/Prosjekt`, `Resultat`.

## Response Style

- Present run results in a clean markdown table rather than a plain bullet list.
- Use short, readable task titles and avoid repeating customer/project names in title when relation mapping exists.
- Show guessed `Type` and `Prioritet` in the result table so the user can quickly spot and correct misclassifications in Notion.
- For scheduled runs, render three sections: `Opprettet/Oppdatert`, `Forslag (trenger bekreftelse)`, `Uten kunde/prosjekt`.

## Resources

- `scripts/capture_to_notion.py`: Parse text and upsert tasks into Notion.
- `scripts/scheduled_ingest.py`: Multi-source scheduled ingestion with strict qualification and dedupe state.
- `scripts/apply_suggestion_decision.py`: Handle JA/NEI approvals for queued medium suggestions.
- `scripts/approval_listener.py`: Event-driven approval handler for immediate Slack thread replies.
- `scripts/slack_approval_webhook.py`: Always-on Slack Events webhook service for immediate thread replies.
- `scripts/push_state_to_webhook.py`: Sync local pending suggestions/cursor to hosted webhook state.
- `scripts/test_scheduled_ingest.py`: Unit tests for filtering and confidence logic.
- `scripts/test_apply_suggestion_decision.py`: Unit tests for decision parsing.
- `scripts/test_approval_listener.py`: Unit tests for realtime approval listener behavior.
- `scripts/test_slack_approval_webhook.py`: Unit tests for webhook signature/event handling.
- `references/notion-db-setup.md`: Minimal Notion database property setup and troubleshooting.
- `references/scheduled-ingest-payload.md`: JSON payload format for Slack/Outlook combined runs.
- `references/wal-oppgaver-schema-example.md`: Known-good property mapping snapshot for WAL Oppgaver.
- `references/approval-listener-workflow.md`: Realtime listener architecture and safety model.
- `references/slack-workflow-instant-approval.md`: Slack workflow steps for immediate in-thread confirmations.
- `references/slack-approval-webhook-service.md`: Production setup for always-on realtime approvals.
- `references/com.wal.notion-approval-webhook.plist.example`: macOS launchd template for always-on webhook service.
