"""Unit tests for V3/ops/backup.py.

We don't hit a real S3 endpoint — a mock Backend captures uploads in memory.
We don't require `pg_dump` on the test host — we pass `database_url=""` to get
the placeholder SQL path, which exercises the same code structure.
"""

from __future__ import annotations

import json
import os
import sys
import tarfile
from pathlib import Path

import pytest


# Make V3/ importable regardless of where pytest runs from.
V3_ROOT = Path(__file__).resolve().parents[2]
if str(V3_ROOT) not in sys.path:
    sys.path.insert(0, str(V3_ROOT))

from ops import backup as backup_mod
from ops.backup import (
    Backend,
    BackupConfig,
    BackupResult,
    LocalBackend,
    S3Backend,
    copy_pdf_tree,
    dump_postgres,
    make_backend,
    run_backup,
    write_manifest,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


class CapturingBackend(Backend):
    """In-memory backend that remembers every upload."""

    def __init__(self):
        self.uploads: list[tuple[str, str, int]] = []  # (local_path, remote_key, size)

    def upload(self, local_path: Path, remote_key: str) -> str:
        self.uploads.append((str(local_path), remote_key, local_path.stat().st_size))
        return f"mock://bucket/{remote_key}"


@pytest.fixture
def pdf_dir(tmp_path: Path) -> Path:
    """Seed a fake PDF_OUTPUT_DIR with a couple of files."""
    d = tmp_path / "pdf_src"
    d.mkdir()
    (d / "DEVIS-20260414-ABCDEF.pdf").write_bytes(b"%PDF-fake-one")
    (d / "nested").mkdir()
    (d / "nested" / "DEVIS-20260414-123456.pdf").write_bytes(b"%PDF-fake-two")
    return d


# ── BackupConfig ─────────────────────────────────────────────────────────────


class TestBackupConfig:
    def test_defaults_to_dryrun(self, monkeypatch):
        for var in (
            "BACKUP_PROVIDER",
            "BACKUP_LOCAL_DIR",
            "BACKUP_ENDPOINT_URL",
            "BACKUP_BUCKET",
            "BACKUP_ACCESS_KEY_ID",
            "BACKUP_SECRET_ACCESS_KEY",
            "BACKUP_REGION",
            "DATABASE_URL",
            "PDF_OUTPUT_DIR",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = BackupConfig.from_env()
        assert cfg.provider == "dryrun"
        assert cfg.local_dir == "./data/backups"
        assert cfg.database_url is None
        assert cfg.validate_for_upload() == []

    def test_live_provider_requires_bucket_and_keys(self, monkeypatch):
        monkeypatch.setenv("BACKUP_PROVIDER", "r2")
        monkeypatch.delenv("BACKUP_BUCKET", raising=False)
        monkeypatch.delenv("BACKUP_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("BACKUP_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.delenv("BACKUP_ENDPOINT_URL", raising=False)
        cfg = BackupConfig.from_env()
        errors = cfg.validate_for_upload()
        # Bucket, keys, and endpoint URL all required.
        assert any("BUCKET" in e for e in errors)
        assert any("ACCESS_KEY_ID" in e for e in errors)
        assert any("SECRET_ACCESS_KEY" in e for e in errors)
        assert any("ENDPOINT_URL" in e for e in errors)

    def test_unknown_provider_reports_error(self):
        cfg = BackupConfig(
            provider="definitely-not-s3",
            bucket="b",
            access_key_id="k",
            secret_access_key="s",
            endpoint_url="https://example.com",
        )
        errors = cfg.validate_for_upload()
        assert any("Unknown provider" in e for e in errors)


# ── Artifact producers ───────────────────────────────────────────────────────


class TestDumpPostgres:
    def test_empty_url_writes_placeholder(self, tmp_path):
        dest = tmp_path / "dump.sql"
        result = dump_postgres("", dest)
        assert result == dest
        assert dest.exists()
        body = dest.read_text(encoding="utf-8")
        assert "skipped" in body.lower() or "not set" in body.lower()

    def test_failure_raises_informative_error(self, tmp_path, monkeypatch):
        """If pg_dump exits non-zero, we raise with the stderr summary."""

        class FakeProc:
            returncode = 2
            stderr = b"FATAL: no database"

        def fake_run(cmd, stdout=None, stderr=None, check=False, timeout=None):
            # Simulate pg_dump writing nothing and failing.
            return FakeProc()

        monkeypatch.setattr(backup_mod.subprocess, "run", fake_run)
        dest = tmp_path / "dump.sql"
        with pytest.raises(RuntimeError) as ei:
            dump_postgres("postgres://x/y", dest)
        assert "pg_dump" in str(ei.value)
        assert "2" in str(ei.value)
        assert "no database" in str(ei.value)


class TestCopyPdfTree:
    def test_copies_all_files(self, tmp_path, pdf_dir):
        dest = tmp_path / "stage"
        count, size = copy_pdf_tree(str(pdf_dir), dest)
        assert count == 2
        assert size == len(b"%PDF-fake-one") + len(b"%PDF-fake-two")
        assert (dest / "pdf" / "DEVIS-20260414-ABCDEF.pdf").exists()
        assert (dest / "pdf" / "nested" / "DEVIS-20260414-123456.pdf").exists()

    def test_missing_source_returns_zero(self, tmp_path):
        dest = tmp_path / "stage"
        count, size = copy_pdf_tree(str(tmp_path / "does-not-exist"), dest)
        assert count == 0
        assert size == 0


class TestWriteManifest:
    def test_contains_expected_keys(self, tmp_path):
        cfg = BackupConfig(provider="dryrun", database_url="postgres://x")
        write_manifest(tmp_path, cfg, pdf_count=3, pdf_bytes=1234)
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert manifest["provider"] == "dryrun"
        assert manifest["pdf_count"] == 3
        assert manifest["pdf_bytes"] == 1234
        assert manifest["postgres_dump_included"] is True
        assert "timestamp_utc" in manifest
        assert "module_version" in manifest


# ── Backend factory ──────────────────────────────────────────────────────────


class TestMakeBackend:
    def test_dryrun_returns_local_backend(self):
        b = make_backend(BackupConfig(provider="dryrun"))
        assert isinstance(b, LocalBackend)

    def test_live_with_missing_config_raises(self):
        cfg = BackupConfig(provider="r2")  # no bucket / keys / endpoint
        with pytest.raises(ValueError) as ei:
            make_backend(cfg)
        assert "incomplete" in str(ei.value).lower()

    def test_s3_backend_with_injected_client(self):
        cfg = BackupConfig(
            provider="s3",
            bucket="bkt",
            access_key_id="k",
            secret_access_key="s",
            region="eu-west-3",
        )
        # Inject a fake client so we never touch boto3.
        fake_client = _FakeS3Client()
        backend = S3Backend(cfg, client=fake_client)
        assert backend._client is fake_client


class _FakeS3Client:
    def __init__(self):
        self.calls = []

    def upload_file(self, local_path, bucket, key):
        self.calls.append((local_path, bucket, key))


class TestS3BackendUpload:
    def test_upload_returns_s3_url(self, tmp_path):
        tarball = tmp_path / "archive.tar.gz"
        tarball.write_bytes(b"fake")
        cfg = BackupConfig(
            provider="s3",
            bucket="soufflai-backups",
            access_key_id="k",
            secret_access_key="s",
        )
        fake = _FakeS3Client()
        backend = S3Backend(cfg, client=fake)
        url = backend.upload(tarball, "2026/04/x.tar.gz")
        assert url == "s3://soufflai-backups/2026/04/x.tar.gz"
        assert fake.calls == [(str(tarball), "soufflai-backups", "2026/04/x.tar.gz")]


# ── End-to-end dry-run via run_backup ────────────────────────────────────────


class TestRunBackupDryRun:
    def test_produces_tarball_and_manifest(self, tmp_path, pdf_dir):
        local_out = tmp_path / "out"
        cfg = BackupConfig(
            provider="dryrun",
            local_dir=str(local_out),
            database_url="",  # placeholder mode
            pdf_output_dir=str(pdf_dir),
        )
        backend = LocalBackend(cfg.local_dir)
        result = run_backup(cfg, backend=backend, now="20260414T120000Z")
        assert isinstance(result, BackupResult)
        assert result.provider == "dryrun"
        assert result.pdf_count == 2
        assert result.pdf_bytes > 0
        # The archive landed under BACKUP_LOCAL_DIR keyed by YYYY/MM.
        expected = local_out / result.key
        assert expected.exists()
        # The tarball should contain postgres_dump.sql, pdf/, manifest.json.
        with tarfile.open(expected, "r:gz") as tar:
            names = set(tar.getnames())
        assert any(n.endswith("soufflai-backup/postgres_dump.sql") for n in names)
        assert any(n.endswith("soufflai-backup/manifest.json") for n in names)
        assert any("soufflai-backup/pdf/" in n for n in names)

    def test_captures_uploads_via_injected_backend(self, tmp_path, pdf_dir):
        cfg = BackupConfig(
            provider="dryrun",
            local_dir=str(tmp_path / "unused"),
            database_url="",
            pdf_output_dir=str(pdf_dir),
        )
        cap = CapturingBackend()
        result = run_backup(cfg, backend=cap, now="20260414T120000Z")
        assert len(cap.uploads) == 1
        local_path, remote_key, size = cap.uploads[0]
        assert remote_key.endswith("soufflai-v3-20260414T120000Z.tar.gz")
        assert size > 0
        assert result.remote_location.startswith("mock://bucket/")
