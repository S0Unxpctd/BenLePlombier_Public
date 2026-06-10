"""Souffl.AI V3 — Operations package.

Phase 0 scaffolding for:
- Database backups (Postgres dumps)
- Runtime PDF archive backups (data/pdf volume)
- S3-compatible storage abstraction (ready for Backblaze B2, Cloudflare R2, AWS S3)
- Sentry error monitoring integration

The code in this package is transport-agnostic and import-safe: importing
`V3.ops.backup` does NOT open any network connection or require credentials.
Everything is driven by environment variables at call time.
"""

__all__ = ["backup", "sentry"]
