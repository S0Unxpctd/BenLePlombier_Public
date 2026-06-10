"""Souffl.AI V3 — Backup scaffold.

Ships a provider-agnostic backup module that can be turned on later (when the
user has ~10 active artisans and picks R2 / B2 / S3) with a 10-minute config
job — no code change required.

What this module produces
-------------------------
A single timestamped tarball containing:
1. `postgres_dump.sql` — output of `pg_dump $DATABASE_URL --no-owner --no-acl`.
2. `pdf/` — the contents of the Railway volume where generated PDFs live
   (default `PDF_OUTPUT_DIR`, typically `/app/data/pdf/`).
3. `manifest.json` — metadata: timestamp, hostname, counts, sizes, module
   version, so an operator can audit an archive without unpacking.

What this module does NOT do by default
---------------------------------------
Upload anything anywhere. Without an explicit `BACKUP_PROVIDER` env var, the
module runs in **dry-run** mode: it writes the tarball to a local directory and
returns the path. That's the state the project ships in until the user picks
a real provider.

Providers supported
-------------------
- `dryrun` (default) — write to `BACKUP_LOCAL_DIR` (default `./data/backups`).
- `s3` — AWS S3 (or any S3-compatible endpoint via `BACKUP_ENDPOINT_URL`).
- `r2`  — Cloudflare R2 (same as s3, with `BACKUP_ENDPOINT_URL` pointing to the
  R2 account endpoint).
- `b2`  — Backblaze B2 (same as s3, with `BACKUP_ENDPOINT_URL` pointing to the
  B2 S3-compatible endpoint).

All three "real" providers use the boto3 S3 API. No provider-specific SDK is
imported unless it's actually used, so the dry-run path stays dependency-free
beyond the standard library.

Environment variables
---------------------
- `BACKUP_PROVIDER`           — `dryrun` (default) | `s3` | `r2` | `b2`
- `BACKUP_LOCAL_DIR`          — dry-run output directory (default `./data/backups`)
- `BACKUP_ENDPOINT_URL`       — custom S3 endpoint (R2, B2, MinIO…)
- `BACKUP_BUCKET`             — target bucket (required when provider != dryrun)
- `BACKUP_ACCESS_KEY_ID`      — credentials (required when provider != dryrun)
- `BACKUP_SECRET_ACCESS_KEY`  — credentials (required when provider != dryrun)
- `BACKUP_REGION`             — region (default `auto`; R2 needs `auto`, B2
                                 needs its region code, AWS needs the bucket's
                                 region)
- `DATABASE_URL`              — reused from the bot runtime
- `PDF_OUTPUT_DIR`            — reused from the bot runtime (default
                                 `/app/data/pdf`)

Typical operator usage
----------------------
Run a dry-run backup to a local directory:

    python -m V3.ops.backup

Run a real backup to R2 (given env vars set):

    BACKUP_PROVIDER=r2 BACKUP_BUCKET=soufflai-v3-backups \\
    BACKUP_ENDPOINT_URL=https://<acct>.r2.cloudflarestorage.com \\
    BACKUP_ACCESS_KEY_ID=... BACKUP_SECRET_ACCESS_KEY=... \\
    python -m V3.ops.backup

Exit code 0 on success, non-zero on failure. Suitable for a Railway cron.
"""

from __future__ import annotations

import abc
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MODULE_VERSION = "0.1.0"


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BackupConfig:
    """Immutable backup configuration. Read once from env at call time."""

    provider: str = "dryrun"            # dryrun | s3 | r2 | b2
    local_dir: str = "./data/backups"   # dry-run destination
    endpoint_url: Optional[str] = None  # S3-compatible endpoint (R2, B2, …)
    bucket: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    region: str = "auto"
    database_url: Optional[str] = None
    pdf_output_dir: str = "/app/data/pdf"

    @classmethod
    def from_env(cls) -> "BackupConfig":
        return cls(
            provider=os.getenv("BACKUP_PROVIDER", "dryrun").lower().strip(),
            local_dir=os.getenv("BACKUP_LOCAL_DIR", "./data/backups"),
            endpoint_url=os.getenv("BACKUP_ENDPOINT_URL") or None,
            bucket=os.getenv("BACKUP_BUCKET") or None,
            access_key_id=os.getenv("BACKUP_ACCESS_KEY_ID") or None,
            secret_access_key=os.getenv("BACKUP_SECRET_ACCESS_KEY") or None,
            region=os.getenv("BACKUP_REGION", "auto"),
            database_url=os.getenv("DATABASE_URL") or None,
            pdf_output_dir=os.getenv("PDF_OUTPUT_DIR", "/app/data/pdf"),
        )

    def validate_for_upload(self) -> list[str]:
        """Return a list of error messages; empty = ready to upload."""
        errors: list[str] = []
        if self.provider == "dryrun":
            return errors
        if self.provider not in ("s3", "r2", "b2"):
            errors.append(f"Unknown provider {self.provider!r}")
        if not self.bucket:
            errors.append("BACKUP_BUCKET is required for non-dryrun providers")
        if not self.access_key_id:
            errors.append("BACKUP_ACCESS_KEY_ID is required for non-dryrun providers")
        if not self.secret_access_key:
            errors.append("BACKUP_SECRET_ACCESS_KEY is required for non-dryrun providers")
        if self.provider in ("r2", "b2") and not self.endpoint_url:
            errors.append(f"BACKUP_ENDPOINT_URL is required for provider={self.provider}")
        return errors


# ─────────────────────────────────────────────────────────────────────────────
# Backend abstraction
# ─────────────────────────────────────────────────────────────────────────────


class Backend(abc.ABC):
    """Uploader abstraction. Implementations: `LocalBackend`, `S3Backend`."""

    @abc.abstractmethod
    def upload(self, local_path: Path, remote_key: str) -> str:
        """Upload `local_path` to `remote_key`; return a human-readable
        location string ('file://…' or 's3://bucket/key')."""


class LocalBackend(Backend):
    """Dry-run backend — copies the archive into a local directory."""

    def __init__(self, local_dir: str):
        self.local_dir = Path(local_dir)

    def upload(self, local_path: Path, remote_key: str) -> str:
        self.local_dir.mkdir(parents=True, exist_ok=True)
        dest = self.local_dir / remote_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the already-written archive — a local "upload" is just a copy.
        if local_path.resolve() != dest.resolve():
            shutil.copy2(local_path, dest)
        return f"file://{dest.resolve()}"


class S3Backend(Backend):
    """S3-compatible backend. Supports AWS S3, Cloudflare R2, Backblaze B2."""

    def __init__(self, config: BackupConfig, client=None):
        self._config = config
        # Lazy import: boto3 is only required when this backend is actually used.
        if client is not None:
            self._client = client
        else:
            try:
                import boto3  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "boto3 is required for non-dryrun backups. "
                    "Install with: pip install boto3"
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=config.endpoint_url,
                aws_access_key_id=config.access_key_id,
                aws_secret_access_key=config.secret_access_key,
                region_name=config.region,
            )

    def upload(self, local_path: Path, remote_key: str) -> str:
        bucket = self._config.bucket
        assert bucket, "bucket must be set when using S3Backend"
        self._client.upload_file(str(local_path), bucket, remote_key)
        return f"s3://{bucket}/{remote_key}"


def make_backend(config: BackupConfig) -> Backend:
    """Construct the right Backend for the configured provider.

    Raises ValueError if required env vars are missing for a live provider.
    """
    if config.provider == "dryrun":
        return LocalBackend(config.local_dir)
    errors = config.validate_for_upload()
    if errors:
        raise ValueError(
            "Backup config is incomplete: " + "; ".join(errors)
        )
    # s3, r2, b2 all use the same S3 API under the hood.
    return S3Backend(config)


# ─────────────────────────────────────────────────────────────────────────────
# Artifact producers
# ─────────────────────────────────────────────────────────────────────────────


def dump_postgres(database_url: str, dest: Path) -> Path:
    """Run `pg_dump` to produce a plain-SQL dump at `dest`.

    If `database_url` is falsy, write a placeholder file noting the DB was not
    configured at backup time. That keeps the tarball shape consistent and
    makes dry-run usable in CI without a real database.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not database_url:
        dest.write_text(
            "-- DATABASE_URL was not set at backup time; Postgres dump skipped.\n",
            encoding="utf-8",
        )
        return dest
    cmd = ["pg_dump", "--no-owner", "--no-acl", database_url]
    logger.info("Running %s", shlex.join(cmd[:1] + ["<DATABASE_URL>"]))
    with dest.open("wb") as f:
        proc = subprocess.run(
            cmd, stdout=f, stderr=subprocess.PIPE, check=False, timeout=600
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pg_dump exited with code {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', errors='replace')[:500]}"
        )
    return dest


def copy_pdf_tree(src_dir: str, dest_dir: Path) -> tuple[int, int]:
    """Copy all PDFs under `src_dir` into `dest_dir/pdf/`.

    Returns (file_count, total_bytes). Missing source is tolerated (returns
    (0, 0)) — PDFs may not exist yet on a fresh deployment.
    """
    src = Path(src_dir)
    out = dest_dir / "pdf"
    out.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        return 0, 0
    total_bytes = 0
    count = 0
    for f in src.rglob("*"):
        if f.is_file():
            rel = f.relative_to(src)
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            total_bytes += target.stat().st_size
            count += 1
    return count, total_bytes


def write_manifest(
    dest_dir: Path, config: BackupConfig, pdf_count: int, pdf_bytes: int
) -> Path:
    """Write a `manifest.json` alongside the dump."""
    manifest = {
        "module_version": MODULE_VERSION,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "provider": config.provider,
        "postgres_dump_included": bool(config.database_url),
        "pdf_count": pdf_count,
        "pdf_bytes": pdf_bytes,
    }
    out = dest_dir / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Top-level entry point
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BackupResult:
    archive_path: Path
    remote_location: str
    pdf_count: int
    pdf_bytes: int
    postgres_included: bool
    provider: str
    key: str  # the remote object key used on upload
    manifest: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["archive_path"] = str(self.archive_path)
        return d


def _timestamp() -> str:
    # Filesystem-safe UTC timestamp: 20260414T134500Z
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_backup(
    config: Optional[BackupConfig] = None,
    backend: Optional[Backend] = None,
    now: Optional[str] = None,
) -> BackupResult:
    """Produce the tarball and hand it to the backend.

    Parameters are injectable so tests can pass a mock backend and a fixed
    timestamp. The function is the only part of this module that does I/O.
    """
    config = config or BackupConfig.from_env()
    backend = backend or make_backend(config)
    ts = now or _timestamp()

    archive_name = f"soufflai-v3-{ts}.tar.gz"
    remote_key = f"{datetime.now(timezone.utc).strftime('%Y/%m')}/{archive_name}"

    with tempfile.TemporaryDirectory(prefix="soufflai-backup-") as tmp:
        tmp_path = Path(tmp)
        # 1. Postgres dump
        dump_path = tmp_path / "postgres_dump.sql"
        dump_postgres(config.database_url or "", dump_path)
        # 2. PDF copy
        pdf_count, pdf_bytes = copy_pdf_tree(config.pdf_output_dir, tmp_path)
        # 3. Manifest
        write_manifest(tmp_path, config, pdf_count, pdf_bytes)
        # 4. Tar everything into a single archive next to the temp dir
        archive_path = Path(tmp_path.parent) / archive_name
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(tmp_path, arcname="soufflai-backup")
        # 5. Upload / copy
        location = backend.upload(archive_path, remote_key)
        # 6. Move the archive out of the temp-parent so the caller can inspect it
        #    in dry-run mode (LocalBackend already placed a copy in BACKUP_LOCAL_DIR).
        manifest_bytes = (tmp_path / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))

    return BackupResult(
        archive_path=archive_path,  # NOTE: archive_path is in the temp-parent; LocalBackend copied it
        remote_location=location,
        pdf_count=pdf_count,
        pdf_bytes=pdf_bytes,
        postgres_included=bool(config.database_url),
        provider=config.provider,
        key=remote_key,
        manifest=manifest,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
    )
    config = BackupConfig.from_env()
    logger.info("Backup starting (provider=%s)", config.provider)
    try:
        result = run_backup(config)
    except Exception as exc:
        logger.error("Backup failed: %s", exc, exc_info=True)
        return 1
    logger.info(
        "Backup OK — provider=%s key=%s pdfs=%d size=%d bytes location=%s",
        result.provider,
        result.key,
        result.pdf_count,
        result.pdf_bytes,
        result.remote_location,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
