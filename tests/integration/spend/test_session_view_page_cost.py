"""The session-grouped /spend/logs/ui page must not scan the whole window.

Regression coverage for the session view reading every LiteLLM_SpendLogs row
in the date window to serve one 50-row page. Measures the backend's tuple
reads through pg_stat; the ordering and representative assertions pin the
behavior that must survive the optimization.
"""

import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import psycopg
import pytest
from integration._support.client import Gateway, string_value
from litellm.proxy.spend_tracking.spend_management_endpoints import SPEND_LOGS_PAGINATION_COUNT_CAP
from pydantic import JsonValue, TypeAdapter

SEED_ROWS: Final = 40_000
LONG_SESSION_ROWS: Final = 5_000
SESSION_LEN: Final = 5
BULK_ROWS: Final = SEED_ROWS - LONG_SESSION_ROWS
PAGE_SIZE: Final = 50
MAX_TUPLES_PER_PAGE: Final = SEED_ROWS * 2
_BASE: Final = datetime(2001, 1, 1)
M1_NEWEST_SECOND: Final = SEED_ROWS + 8
M2_NEWEST_SECOND: Final = SEED_ROWS + 5
BLOCKER_NEWEST_SECOND: Final = SEED_ROWS + 3
WINDOW_START: Final = _BASE.strftime("%Y-%m-%d %H:%M:%S")
WINDOW_END: Final = (_BASE + timedelta(seconds=M1_NEWEST_SECOND)).strftime("%Y-%m-%d %H:%M:%S")
_BULK_SESSIONS: Final = len({n // SESSION_LEN for n in range(1, BULK_ROWS + 1)})
EXPECTED_SESSIONS: Final = _BULK_SESSIONS + 5

_STATS_SETTLE_S: Final = 4.0
_STATS_STABLE_POLLS: Final = 4
_STATS_TIMEOUT_S: Final = 30.0
_QUIET_SETTLE_S: Final = 12.0
_QUIET_STABLE_POLLS: Final = 5
_QUIET_TIMEOUT_S: Final = 60.0


def _at(seconds: int) -> str:
    return (_BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True, slots=True)
class _SeedRow:
    request_id: str
    session_id: str | None
    start_time: str


def _hand_placed_rows(marker: str) -> tuple[_SeedRow, ...]:
    prefix: Final = f"intg-sesswin-{marker}-"
    blocker_request_id: Final = f"{prefix}x"
    return (
        _SeedRow(blocker_request_id, None, _at(SEED_ROWS + 1)),
        _SeedRow(f"{prefix}x1", blocker_request_id, _at(SEED_ROWS + 2)),
        _SeedRow(f"{prefix}x2", blocker_request_id, _at(BLOCKER_NEWEST_SECOND)),
        _SeedRow(f"{prefix}m2a", f"{marker}-m2", _at(SEED_ROWS + 4)),
        _SeedRow(f"{prefix}m2b", f"{marker}-m2", _at(M2_NEWEST_SECOND)),
        _SeedRow(f"{prefix}m1a", f"{marker}-m1", _at(SEED_ROWS + 6)),
        _SeedRow(f"{prefix}m1b", f"{marker}-m1", _at(SEED_ROWS + 7)),
        _SeedRow(f"{prefix}m1c", f"{marker}-m1", _at(M1_NEWEST_SECOND)),
        _SeedRow(f"intg-straddle-{marker}-in", f"{marker}-straddle", _at(1000)),
        _SeedRow(f"intg-straddle-{marker}-out", f"{marker}-straddle", _at(SEED_ROWS + 2_000_000)),
    )


def _seed(marker: str) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
        connection.execute(
            'INSERT INTO "LiteLLM_SpendLogs" ('
            'request_id, call_type, api_key, session_id, "startTime", "endTime", model, metadata, messages, response'
            ") "
            "SELECT %s || n, 'acompletion', 'K', "
            "CASE WHEN n > %s THEN %s || '-long' ELSE %s || '-s-' || (n / %s) END, "
            "ts, ts, 'e2e-sesswin', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb "
            "FROM generate_series(1, %s) AS g(n) "
            "CROSS JOIN LATERAL (SELECT %s::timestamp + (n || ' seconds')::interval AS ts) AS t",
            (f"intg-sesswin-{marker}-", BULK_ROWS, marker, marker, SESSION_LEN, SEED_ROWS, WINDOW_START),
        )
        connection.cursor().executemany(
            'INSERT INTO "LiteLLM_SpendLogs" ('
            'request_id, call_type, api_key, session_id, "startTime", "endTime", model, metadata, messages, response'
            ") VALUES (%s, 'acompletion', 'K', %s, %s, %s, 'm', '{}', '{}', '{}')",
            [(row.request_id, row.session_id, row.start_time, row.start_time) for row in _hand_placed_rows(marker)],
        )
        connection.execute('VACUUM ANALYZE "LiteLLM_SpendLogs"')


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
    _wait_until_stable(
        _tuples_read,
        settle_s=_STATS_SETTLE_S,
        stable_polls=_STATS_STABLE_POLLS,
        timeout_s=_STATS_TIMEOUT_S,
    )


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


def _wait_until_stable(
    read: Callable[[], int], *, settle_s: float, stable_polls: int, timeout_s: float
) -> int:
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


def _tuples_read_quiet() -> int:
    return _wait_until_stable(
        _tuples_read,
        settle_s=_QUIET_SETTLE_S,
        stable_polls=_QUIET_STABLE_POLLS,
        timeout_s=_QUIET_TIMEOUT_S,
    )


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
        _settle_autovacuum()
        _seed(marker)
        _wait_for_stats_quiet()
        yield marker


def test_session_view_first_page_reads_a_bounded_slice(gateway: Gateway, seeded: str) -> None:
    prefix: Final = f"intg-sesswin-{seeded}-"
    before: Final = _tuples_read_quiet()
    page: Final = _session_page(gateway, page="1")
    tuples_read: Final = _tuples_read_quiet() - before

    assert page["has_more"] is True
    assert page["total"] == EXPECTED_SESSIONS
    assert EXPECTED_SESSIONS < SPEND_LOGS_PAGINATION_COUNT_CAP
    assert page["total_is_capped"] is False
    data: Final = _data_rows(page)
    assert len(data) == PAGE_SIZE
    assert data[0]["request_id"] == f"{prefix}m1c", (
        "multi-row session must be represented by its newest row"
    )
    assert data[1]["request_id"] == f"{prefix}m2b"
    assert data[2]["request_id"] == f"{prefix}x2", (
        "a NULL session_id row and the rows adopting its request_id as session_id form one session"
    )
    blocker_keys: Final = {f"{prefix}x", f"{prefix}x1", f"{prefix}x2"}
    assert sum(1 for row in data if row["request_id"] in blocker_keys) == 1
    assert data[3]["request_id"] == f"{prefix}{SEED_ROWS}", (
        "the long session must be represented by its newest row"
    )
    expected_fivers: Final = [BULK_ROWS, BULK_ROWS - 1, BULK_ROWS - SESSION_LEN - 1]
    assert [row["request_id"] for row in data[4:7]] == [f"{prefix}{n}" for n in expected_fivers], (
        "each 5-row session must be represented by its newest row, newest session first"
    )
    start_times: Final = [string_value(row["startTime"]) for row in data]
    assert start_times == sorted(start_times, reverse=True)
    assert tuples_read < MAX_TUPLES_PER_PAGE, (
        f"first page read {tuples_read} tuples for a window of {SEED_ROWS} rows and page_size {PAGE_SIZE}; "
        "one GROUP BY pass for the count plus a bounded page, main reads three passes"
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
        f"cursor page read {tuples_read} tuples for a window of {SEED_ROWS} rows and page_size {PAGE_SIZE}; "
        "one GROUP BY pass for the count plus a bounded page, main reads three passes"
    )


def test_session_view_repeated_page_stays_bounded_under_the_generic_plan(gateway: Gateway, seeded: str) -> None:
    """The proxy runs these as prepared statements and Postgres switches to a generic plan after five executions, so a plan that only looks good with literal values shows up here."""
    for _ in range(19):
        _session_page(gateway, page="1")
    before: Final = _tuples_read_quiet()
    _session_page(gateway, page="1")
    tuples_read: Final = _tuples_read_quiet() - before
    assert tuples_read < MAX_TUPLES_PER_PAGE, (
        f"the 20th page read {tuples_read} tuples for a window of {SEED_ROWS} rows and page_size {PAGE_SIZE}"
    )


def test_session_view_uses_newest_row_inside_the_window(gateway: Gateway, seeded: str) -> None:
    page: Final = _session_page(gateway, page="1", session_id=f"{seeded}-straddle")
    assert page["total"] == 1
    data: Final = _data_rows(page)
    assert [row["request_id"] for row in data] == [f"intg-straddle-{seeded}-in"]
    assert len(data) == 1
