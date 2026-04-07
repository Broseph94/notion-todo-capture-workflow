# Slack Workflow: Instant Approval Replies

Use this to make approvals happen immediately when the user replies in thread.
For true always-on behavior, run the service in `references/slack-approval-webhook-service.md`.

## Trigger

- Slack trigger: new message in thread where bot prompt contains:
  - `Skal jeg legge til disse i Notion?`

## Step 1: Build Listener Payload

Build JSON with:

- `event_id`: Slack event id or message id
- `event.text`: user reply text
- `event.channel`: channel id
- `event.ts`: message ts
- `event.thread_ts`: thread ts
- `event.user`: replying user id
- `event.thread_root_text`: root bot prompt text

## Step 2: Run Listener

Run:

```bash
python3 scripts/approval_listener.py \
  --event-json '<payload-json>' \
  --pending-suggestions-file state/pending_suggestions.json \
  --cursor-file state/approval_cursor.json \
  --confirmation-message-file state/approval_confirmation_live.txt
```

## Step 3: Read Result

Listener returns JSON:

- `status=processed`: decision applied
- `status=ignored`: message not a decision / nothing matched
- `status=duplicate`: already processed
- `status=error`: missing run or invalid state

## Step 4: Reply Immediately In Thread

If status is `processed`, post content from:

- `state/approval_confirmation_live.txt`

to the same `thread_ts`.

If status is `ignored` or `duplicate`, skip thread reply.

## Notes

- Existing scheduled jobs at 12:00 and 17:00 remain unchanged and still work as fallback.
- Listener uses idempotency via `state/approval_cursor.json`.
- For production-grade realtime behavior 24/7, prefer `scripts/slack_approval_webhook.py` over cron-only polling.
- Natural language is supported:
  - Approve: `ja`, `det stemmer`, `legg til`, `ja, legg til denne`
  - Reject: `nei`, `ikke legg til`, `ikke gjør det`, `dropp`
