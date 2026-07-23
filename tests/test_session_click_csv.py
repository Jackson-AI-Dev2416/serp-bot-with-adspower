from pathlib import Path

import pytest

from utils.csv_logger import (
    SessionClickCsvLogger,
    aggregate_keyword_clicks_for_domain,
    count_session_click_outcomes,
    filter_click_rows_by_domain,
    is_session_failure_row,
    outcome_to_failure_url,
    read_session_click_file,
    should_auto_stop_on_failure_rate,
)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("not_found", "not found"),
        ("blocked", "blocked"),
        ("error", "error"),
        ("ip_changed", "error"),
        ("tunnel_error", "error"),
        ("proxy_connect_failed", "error"),
        ("success", None),
        ("stopped", None),
    ],
)
def test_outcome_to_failure_url(outcome, expected):
    assert outcome_to_failure_url(outcome) == expected


@pytest.mark.parametrize(
    ("successes", "failures", "threshold_percent", "min_attempts", "expected"),
    [
        (80, 20, 20, 20, False),
        (79, 21, 20, 20, True),
        (18, 2, 10, 20, False),
        (17, 3, 10, 20, True),
        (70, 30, 25, 20, True),
        (75, 25, 25, 20, False),
        (10, 10, 20, 20, True),
        (5, 5, 20, 1, True),
        (10, 0, 0, 20, False),
    ],
)
def test_should_auto_stop_on_failure_rate(
    successes, failures, threshold_percent, min_attempts, expected,
):
    assert should_auto_stop_on_failure_rate(
        successes,
        failures,
        threshold_percent=threshold_percent,
        min_attempts=min_attempts,
    ) is expected


def test_session_failure_and_success_rows(tmp_path: Path):
    path = tmp_path / "result_20260721_120000.csv"
    logger = SessionClickCsvLogger(path, target_domains=["kingdomanma.com"])
    logger.log(
        profile_name="s-001",
        device="Android",
        keyword="영월출장마사지",
        url="https://www.kingdomanma.com/yeongwol",
        page=2,
        rank=3,
        overall_rank=13,
        site="kingdomanma.com",
    )
    logger.log_failure(
        profile_name="s-002",
        device="Windows",
        keyword="속초출장안마",
        site="kingdomanma.com",
        failure_url="not found",
    )
    logger.log_failure(
        profile_name="s-003",
        device="Android",
        keyword="삼척출장마사지",
        site="kingdomanma.com",
        failure_url="error",
    )

    headers, rows, meta = read_session_click_file(path)
    assert len(rows) == 3
    assert count_session_click_outcomes(path) == (1, 2)
    assert is_session_failure_row(headers, rows[1])
    assert rows[1][headers.index("url")] == "not found"
    assert rows[1][headers.index("page")] == ""
    assert rows[2][headers.index("url")] == "error"

    filtered = filter_click_rows_by_domain(headers, rows, "kingdomanma.com", meta=meta)
    assert len(filtered) == 3

    summaries = aggregate_keyword_clicks_for_domain(headers, rows, "kingdomanma.com", meta=meta)
    by_kw = {item["keyword"]: item for item in summaries}
    assert by_kw["영월출장마사지"]["total"] == 1
    assert by_kw["속초출장안마"]["not_found"] == 1
    assert by_kw["삼척출장마사지"]["not_found"] == 1
