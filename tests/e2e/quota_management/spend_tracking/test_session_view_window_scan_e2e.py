"""Session view must not read the whole time window to serve one page.

The Admin UI session view hits /spend/logs/ui?group_by_session=true. On main
the page query aggregates every row in the start/end window before LIMIT, so
page 1 reads the whole window. These tests seed 40000 one-row sessions and
count the LiteLLM_SpendLogs tuples Postgres reports read for a single page
request; a bounded implementation reads only the page's share.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import pytest
from lifecycle import ResourceManager
from session_view_db import (
    SESSION_VIEW_SEED_ROWS,
    delete_session_view_rows,
    seed_session_view_rows,
    spend_logs_tuples_read_quiet,
)
from spend_e2e_client import SpendClient, unique_marker

MAX_TUPLES_PER_PAGE: Final = SESSION_VIEW_SEED_ROWS // 2
PAGE_SIZE: Final = 50


@dataclass(frozen=True, slots=True)
class SeededWindow:
    start: datetime
    end: datetime


@pytest.fixture
def seeded_window(resources: ResourceManager) -> SeededWindow:
    marker: Final = unique_marker()
    start, end = seed_session_view_rows(marker, api_key_marker=f"e2e-sesswin-key-{marker}")
    resources.defer(lambda: delete_session_view_rows(marker))
    return SeededWindow(start=start, end=end + timedelta(seconds=1))


@pytest.mark.e2e
@pytest.mark.quiet_stack
@pytest.mark.covers("quota_management.spend_tracking.pagination.first_page_bounded")
class TestSessionViewFirstPageCost:
    def test_first_page_reads_a_bounded_slice_of_the_window(
        self, client: SpendClient, seeded_window: SeededWindow
    ) -> None:
        before: Final = spend_logs_tuples_read_quiet()
        page: Final = client.session_view_page(
            start=seeded_window.start, end=seeded_window.end, page=1, page_size=PAGE_SIZE
        )
        tuples_read: Final = spend_logs_tuples_read_quiet() - before

        assert len(page.data) == PAGE_SIZE
        assert page.has_more is True
        start_times: Final = tuple(t for row in page.data if (t := row.start_time) is not None)
        assert len(start_times) == PAGE_SIZE, "a row is missing startTime"
        assert start_times == tuple(sorted(start_times, reverse=True)), "rows not newest-first"
        assert page.data[0].start_time is not None and page.data[0].start_time >= seeded_window.end - timedelta(
            seconds=1
        ), "newest seeded row is not first on the page"
        assert tuples_read < MAX_TUPLES_PER_PAGE, (
            f"first page read {tuples_read} LiteLLM_SpendLogs tuples for a window of "
            f"{SESSION_VIEW_SEED_ROWS} rows and page_size {PAGE_SIZE}"
        )

    def test_cursor_page_reads_a_bounded_slice_of_the_window(
        self, client: SpendClient, seeded_window: SeededWindow
    ) -> None:
        first: Final = client.session_view_page(
            start=seeded_window.start, end=seeded_window.end, page=1, page_size=PAGE_SIZE
        )
        assert first.next_session_cursor is not None

        before: Final = spend_logs_tuples_read_quiet()
        second: Final = client.session_view_page(
            start=seeded_window.start,
            end=seeded_window.end,
            page=2,
            page_size=PAGE_SIZE,
            session_cursor=first.next_session_cursor,
        )
        tuples_read: Final = spend_logs_tuples_read_quiet() - before

        assert len(second.data) == PAGE_SIZE
        oldest_first: Final = first.data[-1].start_time
        assert oldest_first is not None
        assert all(row.start_time is not None and row.start_time < oldest_first for row in second.data), (
            "cursor page returned rows not older than the first page's oldest row"
        )
        assert tuples_read < MAX_TUPLES_PER_PAGE, (
            f"cursor page read {tuples_read} LiteLLM_SpendLogs tuples for a window of "
            f"{SESSION_VIEW_SEED_ROWS} rows and page_size {PAGE_SIZE}"
        )
