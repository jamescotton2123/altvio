#!/usr/bin/env python3
"""
Apply supabase/migrations/20260540_intake_automation.sql to the linked Postgres database.

Usage:
  export DATABASE_URL='postgresql://postgres.[ref]:[PASSWORD]@aws-0-[region].pooler.supabase.com:6543/postgres'
  python scripts/apply_intake_automation_migration.py

Find DATABASE_URL in Supabase Dashboard → Project Settings → Database → Connection string (URI).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260540_intake_automation.sql"


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print(
            "Set DATABASE_URL to your Supabase Postgres connection string, then re-run.\n"
            "Dashboard → Project Settings → Database → Connection string (URI).",
            file=sys.stderr,
        )
        return 1

    if not MIGRATION.is_file():
        print(f"Migration file not found: {MIGRATION}", file=sys.stderr)
        return 1

    sql = MIGRATION.read_text(encoding="utf-8")

    try:
        import psycopg2
    except ImportError:
        print("Install psycopg2-binary: pip install psycopg2-binary", file=sys.stderr)
        return 1

    print(f"Applying {MIGRATION.name} …")
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()

    print("Done. Reload PostgREST schema cache in Supabase if tables still 404.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
