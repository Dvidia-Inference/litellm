"""The session-grouped /spend/logs/ui page must not scan the whole window.

Regression coverage for the session view reading every LiteLLM_SpendLogs row
in the date window to serve one 50-row page. Measures the backend's tuple
reads through pg_stat; the ordering and representative assertions pin the
behavior that must survive the optimization.
"""

import os
import time
import uuid
from collections.abc import Iterator, Mapping
from typing import Final

import psycopg
import pytest
from integration._support.client import Gateway, string_value
from pydantic import JsonValue, TypeAdapter

SEED_ROWS: Final = 40_000
PAGE_SIZE: Final = 50
MAX_TUPLES_PER_PAGE: Final = SEED_ROWS // 2
WINDOW_START: Final = "2001-01-01 00:00:00"
WINDOW_END: Final = "2001-01-01 11:06:46"
MULTI_NEWEST_A: Final = "2001-01-01 11:06:45"
MULTI_NEWEST_B: Final = "2001-01-01 11:06:44"


def _seed(marker: str) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
        connection.execute(
            'INSERT INTO "LiteLLM_SpendLogs" ('
            'request_id, call_type, api_key, session_id, "startTime", "endTime", model, metadata, messages, response'
            ") "
            "SELECT %s || n, 'acompletion', 'K', NULL, ts, ts, 'e2e-sesswin', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb "
            "FROM generate_series(1, %s) AS g(n) "
            "CROSS JOIN LATERAL (SELECT %s::timestamp + (n || ' seconds')::interval AS ts) AS t",
            (f"intg-sesswin-{marker}-", SEED_ROWS, WINDOW_START),
        )
        connection.execute(
            'INSERT INTO "LiteLLM_SpendLogs" ('
            'request_id, call_type, api_key, session_id, "startTime", "endTime", model, metadata, messages, response'
            ") VALUES "
            "(%s || 'm1a', 'acompletion', 'K', %s || 'm1', '2001-01-01 11:06:41', '2001-01-01 11:06:41', 'm', '{}', '{}', '{}'),"
            "(%s || 'm1b', 'acompletion', 'K', %s || 'm1', '2001-01-01 11:06:42', '2001-01-01 11:06:42', 'm', '{}', '{}', '{}'),"
            "(%s || 'm1c', 'acompletion', 'K', %s || 'm1', %s, %s, 'm', '{}', '{}', '{}'),"
            "(%s || 'm2a', 'acompletion', 'K', %s || 'm2', '2001-01-01 11:06:43', '2001-01-01 11:06:43', 'm', '{}', '{}', '{}'),"
            "(%s || 'm2b', 'acompletion', 'K', %s || 'm2', %s, %s, 'm', '{}', '{}', '{}')",
            (
                f"intg-sesswin-{marker}-",
                f"{marker}-",
                f"intg-sesswin-{marker}-",
                f"{marker}-",
                f"intg-sesswin-{marker}-",
                f"{marker}-",
                MULTI_NEWEST_A,
                MULTI_NEWEST_A,
                f"intg-sesswin-{marker}-",
                f"{marker}-",
                f"intg-sesswin-{marker}-",
                f"{marker}-",
                MULTI_NEWEST_B,
                MULTI_NEWEST_B,
            ),
        )


def _seed_straddler(marker: str) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
        connection.execute(
            'INSERT INTO "LiteLLM_SpendLogs" ('
            'request_id, call_type, api_key, session_id, "startTime", "endTime", model, metadata, messages, response'
            ") VALUES "
            "(%s, 'acompletion', 'K', %s, '2001-01-01 00:30:00', '2001-01-01 00:30:00', 'm', '{}', '{}', '{}'),"
            "(%s, 'acompletion', 'K', %s, '2001-02-01 00:00:00', '2001-02-01 00:00:00', 'm', '{}', '{}', '{}')",
            (
                f"intg-straddle-{marker}-in",
                f"{marker}-straddle",
                f"intg-straddle-{marker}-out",
                f"{marker}-straddle",
            ),
        )


def _settle_autovacuum() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
        connection.execute('VACUUM ANALYZE "LiteLLM_SpendLogs"')
        connection.execute('ALTER TABLE "LiteLLM_SpendLogs" SET (autovacuum_enabled = false)')


def _cleanup(marker: str) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
        try:
            connection.execute(
                'DELETE FROM "LiteLLM_SpendLogs" WHERE request_id LIKE %s OR request_id LIKE %s',
                (f"intg-sesswin-{marker}-%", f"intg-straddle-{marker}-%"),
            )
            connection.execute('VACUUM "LiteLLM_SpendLogs"')
        finally:
            connection.execute('ALTER TABLE "LiteLLM_SpendLogs" RESET (autovacuum_enabled)')
    _wait_for_stats_quiet()


def _wait_for_stats_quiet() -> None:
    deadline: Final = time.monotonic() + 30.0
    min_wait: Final = time.monotonic() + 4.0
    stable: int = 0
    last: int = -1
    while time.monotonic() < deadline and not (stable >= 4 and time.monotonic() > min_wait):
        current: int = _tuples_read()
        stable = stable + 1 if current == last else 0
        last = current
        time.sleep(0.5)


def _tuples_read() -> int:
    """Heap tuples Postgres reports read on LiteLLM_SpendLogs: seq_tup_read
    plus idx_tup_fetch (heap rows fetched through index scans). Index entries
    traversed do not count, so bulk INSERTs never inflate the reading."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        row = connection.execute(
            "SELECT t.seq_tup_read, t.idx_tup_fetch FROM pg_stat_user_tables t WHERE t.relname = 'LiteLLM_SpendLogs'",
        ).fetchone()
    assert row is not None
    return int(row[0] or 0) + int(row[1] or 0)


def _tuples_read_quiet() -> int:
    """pg_stat readings on LiteLLM_SpendLogs once the collector has gone
    quiet. Each pooled backend flushes its stats independently, so the counter
    climbs in delayed jumps: wait at least twelve seconds, then take the reading
    once it is unchanged for five consecutive polls."""
    deadline: Final = time.monotonic() + 60.0
    min_wait: Final = time.monotonic() + 12.0
    stable: int = 0
    last: int = _tuples_read()
    while time.monotonic() < deadline:
        current: int = _tuples_read()
        stable = stable + 1 if current == last else 0
        last = current
        if stable >= 5 and time.monotonic() > min_wait:
            return current
        time.sleep(0.5)
    raise AssertionError("pg_stat counters never settled")


_ROWS: Final = TypeAdapter(list[dict[str, JsonValue]])


def _data_rows(page: Mapping[str, JsonValue]) -> list[dict[str, JsonValue]]:
    return _ROWS.validate_python(page["data"])


def _session_page(gateway: Gateway, **extra: str) -> dict[str, JsonValue]:
    return gateway.get(
        "/spend/logs/ui",
        params={
            "group_by_session": "true",
            "start_date": WINDOW_START,
            "end_date": WINDOW_END,
            "sort_by": "startTime",
            "sort_order": "desc",
            "page_size": str(PAGE_SIZE),
            **extra,
        },
    )


@pytest.fixture
def seeded(gateway: Gateway) -> Iterator[str]:
    marker: Final = uuid.uuid4().hex
    with gateway.scenario() as scenario:
        scenario.cleanups.callback(_cleanup, marker)
        _seed(marker)
        _seed_straddler(marker)
        _settle_autovacuum()
        _wait_for_stats_quiet()
        yield marker


def test_session_view_first_page_reads_a_bounded_slice(gateway: Gateway, seeded: str) -> None:
    before: Final = _tuples_read_quiet()
    page: Final = _session_page(gateway, page="1")
    tuples_read: Final = _tuples_read_quiet() - before

    assert page["has_more"] is True
    assert page["total"] == 10000
    assert page["total_is_capped"] is True
    data: Final = _data_rows(page)
    assert len(data) == PAGE_SIZE
    assert data[0]["request_id"] == f"intg-sesswin-{seeded}-m1c", (
        "multi-row session must be represented by its newest row"
    )
    assert data[1]["request_id"] == f"intg-sesswin-{seeded}-m2b"
    assert all(row["session_id"] is None for row in data[2:]), "singleton rows must follow, newest first"
    assert data[2]["request_id"] == f"intg-sesswin-{seeded}-{SEED_ROWS}"
    start_times: Final = [string_value(row["startTime"]) for row in data]
    assert start_times == sorted(start_times, reverse=True)
    assert tuples_read < MAX_TUPLES_PER_PAGE, (
        f"first page read {tuples_read} tuples for a window of {SEED_ROWS} rows and page_size {PAGE_SIZE}"
    )


def test_session_view_cursor_page_reads_a_bounded_slice(gateway: Gateway, seeded: str) -> None:
    first: Final = _session_page(gateway, page="1")
    cursor: Final = first["next_session_cursor"]
    assert isinstance(cursor, str)

    before: Final = _tuples_read_quiet()
    second: Final = _session_page(gateway, page="2", session_cursor=cursor)
    tuples_read: Final = _tuples_read_quiet() - before

    second_data: Final = _data_rows(second)
    assert len(second_data) == PAGE_SIZE
    oldest_first: Final = string_value(_data_rows(first)[-1]["startTime"])
    assert all(string_value(row["startTime"]) < oldest_first for row in second_data)
    assert tuples_read < MAX_TUPLES_PER_PAGE, (
        f"cursor page read {tuples_read} tuples for a window of {SEED_ROWS} rows and page_size {PAGE_SIZE}"
    )


def test_session_view_uses_newest_row_inside_the_window(gateway: Gateway, seeded: str) -> None:
    page: Final = _session_page(gateway, page="1", session_id=f"{seeded}-straddle")
    assert page["total"] == 1
    data: Final = _data_rows(page)
    assert [row["request_id"] for row in data] == [f"intg-straddle-{seeded}-in"]
    assert string_value(data[0]["startTime"]).startswith("2001-01-01")
