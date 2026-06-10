# V3 Operations Guide

This guide covers the Phase 0 infrastructure work:

1. Persistent storage for generated PDFs (Railway Volume).
2. Backup strategy — currently scaffolded in dry-run, ready to flip on later.
3. Error monitoring via Sentry (optional).

Every procedure here is reproducible by an operator who has never seen the project before. If any step is unclear, that's a doc bug — file an issue.

---

## 1. Railway Volume for PDFs

### Why

The current Telegram bot writes generated PDFs under `PDF_OUTPUT_DIR` (default `/app/data/pdf/`). Without a Railway Volume mounted there, every redeploy wipes the directory and the PDFs vanish. The code still runs (PDFs are regenerated on demand from the JSON in Postgres), but the cached files are gone until users trigger a re-download.

A Railway Volume makes the directory persistent across deploys.

### How

1. In the Railway dashboard, open the V3 service.
2. Settings → **Volumes** → **Add Volume**.
3. Mount path: `/app/data/pdf`. Size: 1 GB (comfortable for the first ~10 active artisans; plan grows linearly).
4. Save. Railway restarts the service.
5. Verify: in the Railway shell, run `ls -la /app/data/pdf/`. The directory exists and is writable.

### Verify

After a redeploy (Settings → Redeploy), check that a PDF you've placed in the volume is still there:

```bash
# Before redeploy (Railway shell)
touch /app/data/pdf/marker.txt
# Trigger a redeploy in the UI
# After redeploy:
ls /app/data/pdf/marker.txt   # should exist
```

### Environment variable

The bot already reads `PDF_OUTPUT_DIR`, defaulting to `/app/data/pdf`. No change needed if you mount at the default path. If you mount somewhere else, set `PDF_OUTPUT_DIR` to match.

---

## 2. Backups

### Current state

**Phase 0 ships a dry-run-by-default backup module** (`V3/ops/backup.py`). It produces a tarball containing:

- `postgres_dump.sql` — output of `pg_dump` against `DATABASE_URL`.
- `pdf/` — all files under `PDF_OUTPUT_DIR`.
- `manifest.json` — timestamp, hostname, file count, module version.

In dry-run mode (the default), the tarball is written to `BACKUP_LOCAL_DIR` (default `./data/backups`). **No remote upload happens.** This is intentional: the user (So) deferred the provider choice until there are ~10 active artisans, at which point R2 / B2 / S3 costs and latency can be compared with real data.

### Running a dry-run backup

From the project root:

```bash
cd V3
DATABASE_URL=$RAILWAY_POSTGRES_URL python -m ops.backup
```

Output:

```
2026-04-14 14:22:11 | INFO | Backup starting (provider=dryrun)
2026-04-14 14:22:14 | INFO | Backup OK — provider=dryrun key=2026/04/soufflai-v3-20260414T142214Z.tar.gz pdfs=5 size=234567 bytes location=file:///.../data/backups/2026/04/soufflai-v3-20260414T142214Z.tar.gz
```

### Turning on real backups later

When the user is ready, pick a provider and set the env vars. No code change.

#### Cloudflare R2

Best for this use case: no egress fees, S3-compatible, Cloudflare account probably already exists.

```bash
# Railway → Variables
BACKUP_PROVIDER=r2
BACKUP_BUCKET=soufflai-v3-backups
BACKUP_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
BACKUP_ACCESS_KEY_ID=<r2 access key>
BACKUP_SECRET_ACCESS_KEY=<r2 secret>
BACKUP_REGION=auto
```

Steps to create the R2 credentials:
1. Cloudflare dashboard → **R2** → Create bucket named `soufflai-v3-backups`.
2. **Manage R2 API Tokens** → Create token with Object Read & Write on that bucket.
3. Copy the Access Key ID + Secret, and the jurisdiction-specific endpoint (`https://<acct>.r2.cloudflarestorage.com`).

#### Backblaze B2 (S3-compatible mode)

Similar to R2, slightly cheaper for cold storage, small egress fee.

```bash
BACKUP_PROVIDER=b2
BACKUP_BUCKET=soufflai-v3-backups
BACKUP_ENDPOINT_URL=https://s3.<REGION>.backblazeb2.com
BACKUP_ACCESS_KEY_ID=<b2 application keyID>
BACKUP_SECRET_ACCESS_KEY=<b2 application key>
BACKUP_REGION=<REGION>   # e.g. us-west-004
```

Steps:
1. Backblaze → create bucket, set lifecycle "keep last 30 versions".
2. Application Keys → create a key scoped to that bucket, Read & Write.
3. Take the S3-compatible endpoint from the bucket's detail page.

#### AWS S3

Use this only if the user already lives in the AWS console. Egress + request prices are the highest of the three.

```bash
BACKUP_PROVIDER=s3
BACKUP_BUCKET=soufflai-v3-backups
# No BACKUP_ENDPOINT_URL — default AWS endpoint is fine.
BACKUP_ACCESS_KEY_ID=<IAM access key>
BACKUP_SECRET_ACCESS_KEY=<IAM secret>
BACKUP_REGION=eu-west-3   # or whatever region the bucket lives in
```

Steps:
1. S3 → Create bucket in a region close to the Railway region.
2. Enable Versioning + Lifecycle (transition to Glacier after 30 days).
3. Create an IAM user with the `AmazonS3FullAccess` policy scoped to that bucket.

#### Install `boto3`

None of the "real" providers work until `boto3` is in `requirements.txt`. This keeps the dry-run path dependency-free. When you turn on a live provider:

```bash
echo "boto3>=1.34" >> requirements.txt
# Railway will reinstall on next deploy
```

### Scheduling

Until there's a clear SLA, run manually. Once the user is ready:

**Option A — Railway cron job** (recommended):

```yaml
# railway.toml (see Railway docs for current syntax)
[[services]]
name = "soufflai-v3-backup"
schedule = "0 3 * * *"         # daily at 03:00 UTC
command  = "python -m ops.backup"
```

**Option B — external cron / GitHub Actions**: trigger the same `python -m ops.backup` over SSH or via a scheduled Action. Same env vars required.

### Restoring from a backup

1. Download the tarball from the remote location shown in the log line.
2. `tar -xzf soufflai-v3-YYYYMMDDTHHMMSSZ.tar.gz` — unpacks into `soufflai-backup/`.
3. `psql "$DATABASE_URL" < soufflai-backup/postgres_dump.sql`
4. `rsync -a soufflai-backup/pdf/ /app/data/pdf/`

Validate by checking `SELECT COUNT(*) FROM devis;` matches the expected number and `ls /app/data/pdf/ | wc -l` matches the `pdf_count` in `manifest.json`.

---

## 3. Sentry (optional)

### Why

Centralized error reporting with stack traces and context. Free tier (5,000 events/month) is plenty for a pre-launch product.

### Setup

1. [sentry.io](https://sentry.io) → create project → Platform: Python.
2. Copy the DSN.
3. Railway → Variables → add `SENTRY_DSN=https://<hash>@o<id>.ingest.sentry.io/<proj>`.
4. Optional: set `ENVIRONMENT=production` (defaults to `production` if unset).
5. Install `sentry-sdk`: `echo "sentry-sdk>=2.0" >> requirements.txt`.
6. Redeploy.

### How the bot uses it

`V3/bot.py` calls `V3.ops.sentry.init_sentry()` early in `main()`. If `SENTRY_DSN` is empty or `sentry-sdk` isn't installed, the call is a no-op (a single `INFO` or `WARNING` log line, no failure).

Exceptions raised in bot handlers are captured automatically via the `LoggingIntegration` — any `logger.error(..., exc_info=True)` triggers a Sentry event. Handlers can also call `ops.sentry.capture_exception(exc)` explicitly.

### Verify the wiring

In a Python shell with `SENTRY_DSN` set:

```python
from V3.ops.sentry import init_sentry, capture_exception
init_sentry()
try:
    raise RuntimeError("Sentry wiring test")
except RuntimeError as e:
    capture_exception(e)
```

Check the Sentry project dashboard — the event should appear within a minute.

---

## 4. Verification checklist (Phase 0)

Run through this after any infra-only change:

- [ ] Volume mounted at `/app/data/pdf` — `ls` works, PDFs survive a redeploy.
- [ ] `python -m ops.backup` completes with exit code 0 in dry-run mode.
- [ ] The dry-run tarball exists under `BACKUP_LOCAL_DIR`, contains `postgres_dump.sql`, a `pdf/` tree, and `manifest.json`.
- [ ] Bot starts even with `SENTRY_DSN` unset — no tracebacks in logs.
- [ ] With `SENTRY_DSN` set, an explicit `capture_exception(...)` produces a visible event in Sentry.
- [ ] This README reproduces — a fresh developer can follow it end-to-end.
