"""Direct Postgres helpers for the session-view window-scan test.

The endpoint under test reads LiteLLM_SpendLogs, so the fixture writes rows
straight to the table (one INSERT over generate_series) and measures the
backend's work through the cumulative pg_stat_user_tables counters.
"""

import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Final

import psycopg
from e2e_db import database_url

SESSION_VIEW_SEED_BASE: Final = datetime(2001, 1, 1, tzinfo=timezone.utc)
SESSION_VIEW_SEED_ROWS: Final = 40_000
SESSION_VIEW_LONG_ROWS: Final = 5_000
SESSION_VIEW_SESSION_LEN: Final = 5
SESSION_VIEW_REQUEST_PREFIX: Final = "e2e-sesswin"

_STATS_SETTLE_S: Final = 4.0
_STATS_STABLE_POLLS: Final = 4
_STATS_TIMEOUT_S: Final = 30.0
_QUIET_SETTLE_S: Final = 12.0
_QUIET_STABLE_POLLS: Final = 5
_QUIET_TIMEOUT_S: Final = 60.0


def seed_session_view_rows(marker: str, api_key_marker: str) -> tuple[datetime, datetime]:
    """Insert SESSION_VIEW_SEED_ROWS rows, one second apart starting at
    SESSION_VIEW_SEED_BASE: SESSION_VIEW_SESSION_LEN-row sessions for the bulk
    of the window and one SESSION_VIEW_LONG_ROWS-row session at its top, every
    session_id non-null. Returns the [start, end] window that covers exactly
    the seeded rows."""
    start: Final = SESSION_VIEW_SEED_BASE
    end: Final = start + timedelta(seconds=SESSION_VIEW_SEED_ROWS - 1)
    bulk_rows: Final = SESSION_VIEW_SEED_ROWS - SESSION_VIEW_LONG_ROWS
    with psycopg.connect(database_url()) as conn:
        _ = conn.execute(
            'INSERT INTO "LiteLLM_SpendLogs" ('
            'request_id, call_type, api_key, session_id, "startTime", "endTime", '
            "model, metadata, messages, response"
            ") "
            "SELECT %s || n, 'acompletion', %s, "
            "CASE WHEN n > %s THEN %s || '-long' ELSE %s || '-s-' || (n / %s) END, "
            "ts, ts, 'e2e-sesswin', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb "
            "FROM generate_series(1, %s) AS g(n) "
            "CROSS JOIN LATERAL (SELECT %s::timestamp + (n || ' seconds')::interval AS ts) AS t",
            (
                f"{SESSION_VIEW_REQUEST_PREFIX}-{marker}-",
                api_key_marker,
                bulk_rows,
                marker,
                marker,
                SESSION_VIEW_SESSION_LEN,
                SESSION_VIEW_SEED_ROWS,
                start.replace(tzinfo=None),
            ),
        )
    with psycopg.connect(database_url(), autocommit=True) as conn:
        _ = conn.execute('VACUUM ANALYZE "LiteLLM_SpendLogs"')
        _ = conn.execute('ALTER TABLE "LiteLLM_SpendLogs" SET (autovacuum_enabled = false)')
    return start, end


def delete_session_view_rows(marker: str) -> None:
    with psycopg.connect(database_url(), autocommit=True) as conn:
        try:
            _ = conn.execute(
                'DELETE FROM "LiteLLM_SpendLogs" WHERE request_id LIKE %s',
                (f"{SESSION_VIEW_REQUEST_PREFIX}-{marker}-%",),
            )
            _ = conn.execute('VACUUM "LiteLLM_SpendLogs"')
        finally:
            _ = conn.execute('ALTER TABLE "LiteLLM_SpendLogs" RESET (autovacuum_enabled)')
    _wait_for_stats_quiet()


def _wait_for_stats_quiet() -> None:
    wait_until_stable(
        spend_logs_tuples_read,
        settle_s=_STATS_SETTLE_S,
        stable_polls=_STATS_STABLE_POLLS,
        timeout_s=_STATS_TIMEOUT_S,
    )


def wait_until_stable(read: Callable[[], int], *, settle_s: float, stable_polls: int, timeout_s: float) -> int:
    """pg_stat readings once the collector has gone quiet. Each pooled backend
    flushes its stats independently, so the counter climbs in delayed jumps:
    wait at least ``settle_s`` seconds, then take the reading once it is
    unchanged for ``stable_polls`` consecutive polls."""
    deadline: Final = time.monotonic() + timeout_s
    min_wait: Final = time.monotonic() + settle_s
    stable: int = 0
    last: int = read()
    while time.monotonic() < deadline:
        current: int = read()
        stable = stable + 1 if current == last else 0
        last = current
        if stable >= stable_polls and time.monotonic() > min_wait:
            return current
        time.sleep(0.5)
    raise AssertionError("pg_stat counters never settled")


def spend_logs_tuples_read_quiet() -> int:
    return wait_until_stable(
        spend_logs_tuples_read,
        settle_s=_QUIET_SETTLE_S,
        stable_polls=_QUIET_STABLE_POLLS,
        timeout_s=_QUIET_TIMEOUT_S,
    )


def spend_logs_tuples_read() -> int:
    """Heap tuples Postgres reports read on LiteLLM_SpendLogs: seq_tup_read
    plus idx_tup_fetch (heap rows fetched through index scans). Index entries
    traversed do not count, so bulk INSERTs never inflate the reading."""
    with psycopg.connect(database_url()) as conn:
        row = conn.execute(
            "SELECT t.seq_tup_read, t.idx_tup_fetch FROM pg_stat_user_tables t WHERE t.relname = 'LiteLLM_SpendLogs'",
        ).fetchone()
    if row is None:
        raise RuntimeError("pg_stat_user_tables has no LiteLLM_SpendLogs row")
    return int(row[0] or 0) + int(row[1] or 0)
