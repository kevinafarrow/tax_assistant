# Tax Assistant

Email a photo of a business receipt to a secret address; it gets verified,
extracted with the Claude API, filed into a Schedule C–organized folder tree on
the host's disk, indexed in SQLite, and confirmed to your phone via Pushover.
At tax time you get per-category totals and a CSV that maps line-for-line onto
Schedule C Part II.

## How it works

```
phone photo ──email──▶ <uuid>@yourdomain (Purelymail)
                             │  IMAP poll every 5 min (outbound only — no open ports)
                             ▼
                       trust gate: allowlisted sender + SPF pass + bearer token
                        │ pass                        │ fail
                        ▼                             ▼
                  Claude extraction            Rejected folder + Pushover "BAD" alert
                (vendor/date/amount/
                 Schedule C category)
                        │
        ┌───────────────┼──────────────────┐
        ▼               ▼                  ▼
  data/receipts/   ledger.db          Pushover "✅" with
  YYYY/NN-Cat/     (rebuildable       vendor/amount/category
  date_vendor_amt   index)
  .jpg + .json sidecar
```

Failures (unreadable image, invalid extraction) land in `data/quarantine/`
with a JSON note and a Pushover alert. Every successful poll cycle pings
healthchecks.io; failures ping `…/fail`.

## Setup

### 1. Mailbox (Purelymail)

1. Generate the inbox UUID: `uuidgen | tr 'A-Z' 'a-z'`
2. In Purelymail, create a **new user** `<uuid>@yourdomain` with a strong
   unique password. (A dedicated user isolates the credential — this app never
   sees your real mailbox.)
3. Put the address and password in `.env` (`IMAP_USER`, `IMAP_PASSWORD`).

### 2. Trust gate

1. Generate the bearer token: `uuidgen | tr 'A-Z' 'a-z'` → `BEARER_TOKEN`.
2. List the addresses you'll send from in `ALLOWED_SENDERS`.
3. Make sure the domains you send *from* publish SPF (Gmail/Workspace and
   Purelymail both do) — the gate requires `spf=pass` in Purelymail's
   `Authentication-Results` header.

**Phone workflow tip:** save the inbox address as a contact ("Tax Receipts")
and add a keyboard text replacement (e.g. `;rcpt` → the bearer token) so
sending a receipt is: share photo → Mail → contact → `;rcpt` + any notes → send.

### 3. Services

- **Anthropic**: API key → `ANTHROPIC_API_KEY`. Extraction uses
  `claude-opus-4-8` (≈1.5¢/receipt); override with `ANTHROPIC_MODEL`.
- **Pushover**: your user key → `PUSHOVER_USER_KEY`; create an application
  ("Tax Assistant") → `PUSHOVER_APP_TOKEN`.
- **healthchecks.io**: create a check with **period 5 minutes, grace 10
  minutes**; paste its ping URL → `HEALTHCHECKS_URL`.

### 4. Run

```sh
mkdir -p data          # gitignored; bind-mounted into the container at /data
cp .env.example .env   # fill everything in
docker compose up -d --build
```

If the container logs permission errors on `/data`, make the directory owned by
uid 1000 (the container's `app` user): `sudo chown -R 1000:1000 data`.

Web UI: `http://<host>:1040` (password from `WEB_PASSWORD`). LAN only — do
**not** port-forward it.

### 5. Smoke test

```sh
docker compose exec tax-assistant python -m tax_assistant.cli test-push
docker compose exec tax-assistant python -m tax_assistant.cli test-healthcheck
docker compose exec tax-assistant python -m tax_assistant.cli poll-once
```

Then email yourself a real receipt (with the token) and watch for the Pushover
confirmation.

## Everyday use

- **Send receipts**: photo (JPEG/PNG) or PDF attached; anything you type in
  the subject/body is given to the extractor as context and can correct the
  category ("this was a client dinner").
- **Review/edit**: web UI → click a receipt → fix vendor/date/amount/category.
  Edits update the file location, sidecar, and ledger atomically and are
  audit-logged.
- **Tax time**: web UI "Download report-YYYY.csv", or:
  ```sh
  docker compose exec tax-assistant python -m tax_assistant.cli report --year 2026
  ```
- **Duplicates** (same image sent twice) are detected by hash and skipped.

## Operations

| Task | Command |
|---|---|
| Per-category totals + CSV | `… python -m tax_assistant.cli report [--year YYYY]` |
| Rebuild ledger from sidecars | `… python -m tax_assistant.cli rebuild-db` |
| One manual poll | `… python -m tax_assistant.cli poll-once` |
| Logs | `docker compose logs -f` |

(`…` = `docker compose exec tax-assistant`)

- **Backups & migration**: see the next section.
- **UUID rotation** (after a "BAD email" alert you didn't cause): create a new
  Purelymail user with a fresh UUID, update `IMAP_USER`/`IMAP_PASSWORD`,
  generate a new `BEARER_TOKEN`, update your phone contact + text replacement,
  `docker compose up -d`. Delete the old Purelymail user.

## Backups & migration

All application state lives in two places, both in this directory:

| What | Contains | Notes |
|---|---|---|
| `./data/` | Receipt images + JSON sidecars, `quarantine/`, `ledger.db` | The receipts tree with its sidecars is the **source of truth** — each sidecar carries every field including the dedup `sha256`. `ledger.db` is an index rebuildable from the sidecars, except two tables that exist only in the DB: `audit_log` (edit history) and `processed_emails` (processing log + daily-cap counter). |
| `.env` | All secrets: IMAP password, bearer token, Anthropic key, Pushover keys, web password | Gitignored — it is **not** in the repo, so a `git clone` alone won't reproduce the install. |

Nothing in either is tied to the host (no hostnames, IPs, or absolute paths —
sidecar paths are relative to `/data`), so both moving and restoring are plain
file copies.

### Backing up

Back up `./data` and `.env`.

- `./data` can be snapshotted live: if `ledger.db` is caught mid-write the
  copy may be torn, but the sidecars are authoritative — after restoring, run
  `docker compose exec tax-assistant python -m tax_assistant.cli rebuild-db`.
  (A torn snapshot loses `audit_log`/`processed_emails`; if you care about
  edit history, stop the container for the backup or add a
  `sqlite3 data/ledger.db ".backup ..."` step.)
- `.env` contains live credentials — only include it in a backup that is
  encrypted at rest. If your secrets are already in a password manager, you
  can skip backing it up and re-create it from `.env.example` on restore.

Not needed: the repo (it's on git), the Docker image (rebuilt by
`docker compose up --build`), and the Purelymail mailbox (mail is only a
transport; everything extracted from it is in the sidecars).

### Migrating to a new server

1. **Old server**: `docker compose down`. Don't run both servers at once —
   two pollers would race on the one mailbox. (healthchecks.io will alert
   during the gap; pause the check if you don't want the noise.)
2. **New server**: `git clone` the repo, then copy the state over:
   ```sh
   rsync -a old-host:tax_assistant/data/ ./data/
   scp old-host:tax_assistant/.env .env
   ```
3. Fix ownership for the container's non-root user:
   `sudo chown -R 1000:1000 data`
4. `docker compose up -d --build`, then smoke-test:
   ```sh
   docker compose exec tax-assistant python -m tax_assistant.cli test-push
   docker compose exec tax-assistant python -m tax_assistant.cli poll-once
   ```
5. The web UI moves to `http://<new-host>:1040`. Same rule as before: LAN
   only, never port-forward it. Decommission the old copy — `data/` and
   `.env` on the old server still hold receipts and live credentials, so
   delete them (or wipe the machine) once the new server is verified.

Polling picks up where it left off with no reconfiguration: the mailbox state
lives at Purelymail, and any message somehow processed twice is caught by the
sha256 dedup.

## Security model

- **No inbound path**: the app only makes outbound connections (IMAPS,
  Anthropic, Pushover, healthchecks). Spammers can at most land mail in a
  mailbox; they cannot reach your network.
- **Three-factor gate**: unguessable UUID address (discovery), UUID bearer
  token (submission), allowlisted sender with SPF pass (spoofing).
- **Minimal parsing of untrusted bytes**: attachments are hashed and stored,
  never decoded locally; only Python's stdlib MIME parser touches raw mail.
- **Prompt-injection containment**: the extraction call has no tools, must
  return strict-schema JSON, and every field is validated; worst case is a
  miscategorized receipt visible in the UI/alerts.
- **Bounded blast radius**: daily email cap, attachment size/count caps,
  hardened container (non-root, read-only rootfs, `cap_drop: ALL`, single
  writable mount).
- **Write surface**: the web UI can edit ledger data, so it requires the
  `WEB_PASSWORD` (session cookie, `SameSite=Strict`); edits are audit-logged.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest
DATA_DIR=./data DISABLE_POLLER=1 WEB_PASSWORD=dev .venv/bin/uvicorn tax_assistant.main:app --port 1040
```
