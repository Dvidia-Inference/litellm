"""Direct Postgres helpers for the session-view window-scan test.

The endpoint under test reads LiteLLM_SpendLogs, so the fixture writes rows
straight to the table (one INSERT over generate_series) and measures the
backend's work through the cumulative pg_stat_user_tables counters.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Final

import psycopg

DEFAULT_DATABASE_URL: Final = "postgresql://llmproxy:dbpassword9090@localhost:5432/litellm"

SESSION_VIEW_SEED_BASE: Final = datetime(2001, 1, 1, tzinfo=timezone.utc)
SESSION_VIEW_SEED_ROWS: Final = 40_000
SESSION_VIEW_REQUEST_PREFIX: Final = "e2e-sesswin"


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def seed_session_view_rows(marker: str, api_key_marker: str) -> tuple[datetime, datetime]:
    """Insert SESSION_VIEW_SEED_ROWS rows, one second apart starting at
    SESSION_VIEW_SEED_BASE, each its own session (session_id NULL, so the
    session key falls back to request_id). Returns the [start, end] window that
    covers exactly the seeded rows."""
    start: Final = SESSION_VIEW_SEED_BASE
    end: Final = start + timedelta(seconds=SESSION_VIEW_SEED_ROWS - 1)
    with psycopg.connect(_database_url()) as conn:
        _ = conn.execute(
            'INSERT INTO "LiteLLM_SpendLogs" ('
            'request_id, call_type, api_key, session_id, "startTime", "endTime", '
            "model, metadata, messages, response"
            ") "
            "SELECT %s || n, 'acompletion', %s, NULL, "
            "ts, ts, 'e2e-sesswin', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb "
            "FROM generate_series(1, %s) AS g(n) "
            "CROSS JOIN LATERAL (SELECT %s::timestamp + (n || ' seconds')::interval AS ts) AS t",
            (
                f"{SESSION_VIEW_REQUEST_PREFIX}-{marker}-",
                api_key_marker,
                SESSION_VIEW_SEED_ROWS,
                start.replace(tzinfo=None),
            ),
        )
    with psycopg.connect(_database_url(), autocommit=True) as conn:
        _ = conn.execute('ALTER TABLE "LiteLLM_SpendLogs" SET (autovacuum_enabled = false)')
        _ = conn.execute('VACUUM ANALYZE "LiteLLM_SpendLogs"')
    return start, end


def delete_session_view_rows(marker: str) -> None:
    with psycopg.connect(_database_url(), autocommit=True) as conn:
        _ = conn.execute(
            'DELETE FROM "LiteLLM_SpendLogs" WHERE request_id LIKE %s',
            (f"{SESSION_VIEW_REQUEST_PREFIX}-{marker}-%",),
        )
        _ = conn.execute('VACUUM "LiteLLM_SpendLogs"')
        _ = conn.execute('ALTER TABLE "LiteLLM_SpendLogs" RESET (autovacuum_enabled)')
    _wait_for_stats_quiet()


def _wait_for_stats_quiet(timeout_s: float = 30.0) -> None:
    deadline: Final = time.monotonic() + timeout_s
    min_wait: Final = time.monotonic() + 4.0
    stable: int = 0
    last: int = -1
    while time.monotonic() < deadline and not (stable >= 4 and time.monotonic() > min_wait):
        current: int = spend_logs_tuples_read()
        stable = stable + 1 if current == last else 0
        last = current
        time.sleep(0.5)


def spend_logs_tuples_read_quiet(timeout_s: float = 60.0) -> int:
    """pg_stat reading on LiteLLM_SpendLogs once the collector has gone
    quiet. Each pooled backend flushes its stats independently, so the counter
    climbs in delayed jumps: wait at least twelve seconds, then take the reading
    once it is unchanged for five consecutive polls."""
    deadline: Final = time.monotonic() + timeout_s
    min_wait: Final = time.monotonic() + 12.0
    stable: int = 0
    last: int = spend_logs_tuples_read()
    while time.monotonic() < deadline:
        current: int = spend_logs_tuples_read()
        stable = stable + 1 if current == last else 0
        last = current
        if stable >= 5 and time.monotonic() > min_wait:
            return current
        time.sleep(0.5)
    raise AssertionError("pg_stat counters never settled")


def spend_logs_tuples_read() -> int:
    """Heap tuples Postgres reports read on LiteLLM_SpendLogs: seq_tup_read
    plus idx_tup_fetch (heap rows fetched through index scans). Index entries
    traversed do not count, so bulk INSERTs never inflate the reading."""
    with psycopg.connect(_database_url()) as conn:
        row = conn.execute(
            "SELECT t.seq_tup_read, t.idx_tup_fetch FROM pg_stat_user_tables t WHERE t.relname = 'LiteLLM_SpendLogs'",
        ).fetchone()
    if row is None:
        raise RuntimeError("pg_stat_user_tables has no LiteLLM_SpendLogs row")
    return int(row[0] or 0) + int(row[1] or 0)
