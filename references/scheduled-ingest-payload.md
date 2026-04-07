# Scheduled Ingest Payload

`scripts/scheduled_ingest.py` expects a JSON file that combines Slack and Outlook items.

## Accepted shape

Use either:

- A top-level list of items
- Or an object with an `items` array

## Required fields per item

- `source`: one of:
  - `slack_dm`
  - `slack_mention`
  - `outlook_email`
  - `outlook_calendar`
- `id` or `external_id`: unique source ID
- Text content from one of: `text`, `message`, or (`subject` + `body` / `body_preview`)

## Optional fields

- `created_at` (ISO datetime)
- `url`
- `summary`
- `is_from_me` / `from_me` (bool or `"true"/"false"`)
- `direction` (`"outgoing"` / `"incoming"`)

`is_from_me` and `direction` are used by strict filtering to avoid treating your own delegated requests as your TODOs.

## Example

```json
{
  "items": [
    {
      "source": "slack_dm",
      "id": "slack-123",
      "text": "Vi ma sende april-rapport til Obs innen torsdag.",
      "created_at": "2026-04-03T09:10:00+02:00",
      "url": "https://slack.com/archives/..."
    },
    {
      "source": "outlook_email",
      "id": "mail-abc",
      "subject": "Action items fra mote",
      "body_preview": "Jakob skal lage utkast til nye formater innen onsdag.",
      "created_at": "2026-04-03T10:01:00+02:00"
    },
    {
      "source": "outlook_calendar",
      "id": "event-789",
      "subject": "Q2 planlegging med Obs",
      "summary": "Booke oppfolging neste uke.",
      "created_at": "2026-04-03T10:20:00+02:00"
    }
  ]
}
```

## Notes

- State-based dedupe is stored in `--state-file` (default in `~/.codex/state/...`).
- Default mode is strict: only high-confidence tasks are auto-upserted.
