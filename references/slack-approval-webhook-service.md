# Slack Approval Webhook Service (Always-On)

Dette er den produksjonsklare måten å svare umiddelbart i tråd når du svarer på:

`Skal jeg legge til disse i Notion?`

Løsningen er additiv. Den endrer ikke 12:00/17:00-ingest-jobbene.

## Hva den gjør

- Lytter kontinuerlig på Slack Events API.
- Verifiserer Slack-signatur på alle requests.
- Prosesserer kun trådsvar som matcher approval-flyten.
- Kjører `approval_listener.py` for JA/NEI/naturlig språk.
- Poster bekreftelse med en gang i samme tråd.

## 1) Slack App-oppsett

I Slack app settings:

1. **OAuth scopes (Bot token scopes)**
- `chat:write`
- `channels:history`
- `groups:history`
- `im:history`
- `mpim:history`

2. **Event Subscriptions**
- Enable Events
- Request URL: `https://<din-host>/slack/events`
- Subscribe to bot events:
  - `message.channels`
  - `message.groups`
  - `message.im`
  - `message.mpim`

3. Installer/oppdater appen i workspace etter scope-endringer.

## 2) Miljøvariabler

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_SIGNING_SECRET="..."
export STATE_SYNC_TOKEN="choose-a-long-random-token"
export NOTION_API_TOKEN="secret_..."          # hvis du bruker lokal Notion API fallback
export NOTION_DATABASE_ID="..."               # hvis du bruker lokal Notion API fallback
```

Notion connector-flyt i Codex kan fortsatt brukes separat. Webhook-scriptet trenger kun Slack-token/signing-secret for å lytte og svare.

## 3) Start tjenesten

Fra prosjektmappen:

```bash
python3 scripts/slack_approval_webhook.py \
  --host 0.0.0.0 \
  --port 8787 \
  --pending-suggestions-file state/pending_suggestions.json \
  --cursor-file state/approval_cursor.json \
  --confirmation-message-file state/approval_confirmation_live.txt
```

Health check:

```bash
curl -s http://127.0.0.1:8787/healthz
```

## 3b) Sync pending state fra lokal ingest til Render

Hvis `scheduled_ingest.py` fortsatt kjøres et annet sted (f.eks. lokalt), push state etter hver run:

```bash
python3 scripts/push_state_to_webhook.py \
  --webhook-base-url https://<din-render-url> \
  --sync-token "$STATE_SYNC_TOKEN" \
  --pending-suggestions-file state/pending_suggestions.json \
  --include-cursor
```

Dette løser akkurat problemet der `pending_suggestions.json` ellers bare finnes lokalt.

## 4) Kjør den som bakgrunnstjeneste

Anbefaling: kjør på en alltid-på host (VPS/VM/container). Hvis hosten er nede, stopper umiddelbare svar.

macOS `launchd`-mal finnes her:

- `references/com.wal.notion-approval-webhook.plist.example`

## 5) Test i produksjon

1. Kjør en vanlig 12:00/17:00-runde som lager et approval-spørsmål i Slack.
2. Svar i tråden: `Ja, legg til denne i Notion`.
3. Verifiser at bot svarer i samme tråd innen få sekunder.
4. Verifiser at oppgaven blir lagt i Notion.

## Feilsøking

- `401 invalid_signature`: feil `SLACK_SIGNING_SECRET` eller request går via feil proxy.
- `401 invalid_auth` på `/internal/state/sync`: feil/manglende `STATE_SYNC_TOKEN`.
- Ingen svar i tråd:
  - sjekk at Event Subscriptions er enabled
  - sjekk at request URL er offentlig tilgjengelig
  - sjekk service-logger
- Svar kommer, men ingen Notion-opprettelse:
  - sjekk `pending_suggestions.json` og `approval_cursor.json`
  - sjekk at beslutningen faktisk matcher pending forslag i riktig tråd
