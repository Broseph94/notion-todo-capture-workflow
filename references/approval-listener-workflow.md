# Approval Listener Workflow (Slack -> Notion)

This workflow adds realtime approval handling without changing the existing 12:00 / 17:00 ingest jobs.

## Goal

- User replies in the Slack thread with natural language:
  - `Ja, legg til denne`
  - `Ja, legg til alle`
  - `Nei, ikke legg til denne`
- Suggestion is processed immediately.
- Bot posts a confirmation back in the same thread.

## Components

- Existing (unchanged): `scripts/scheduled_ingest.py` and `scripts/apply_suggestion_decision.py`
- New additive component: `scripts/approval_listener.py`

## Required State Files

- `state/pending_suggestions.json`
- `state/approval_cursor.json` (idempotency: avoid duplicate handling)
- `state/approval_confirmation_live.txt` (outgoing thread confirmation)

## Slack Event Payload (minimum fields)

```json
{
  "event_id": "Ev123",
  "event": {
    "text": "Ja, legg til denne i notion to do databasen",
    "channel": "D123",
    "ts": "1775547000.000100",
    "thread_ts": "1775546900.000100",
    "user": "U123",
    "thread_root_text": "*Skal jeg legge til disse i Notion?*\\n_Run: run-..._"
  }
}
```

`thread_root_text` is recommended because it lets the listener resolve `run_id` even when the user does not include it.

## Listener Invocation

```bash
python3 scripts/approval_listener.py \
  --event-file /tmp/slack-event.json \
  --pending-suggestions-file state/pending_suggestions.json \
  --cursor-file state/approval_cursor.json \
  --confirmation-message-file state/approval_confirmation_live.txt
```

## Slack Workflow Integration

1. Trigger on new thread replies to bot suggestion messages.
2. Build JSON event payload including `thread_root_text`.
3. Invoke `approval_listener.py` with payload.
4. If listener result status is `processed`, post `approval_confirmation_live.txt` in the same thread.
5. If status is `duplicate` or `ignored`, do nothing.

## Safety

- Existing scheduled jobs continue to run as before (fallback).
- Listener only handles thread replies or messages that include `run-...`.
- Duplicate events are ignored via cursor state.
