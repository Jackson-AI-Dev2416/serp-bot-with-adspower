from utils.csv_logger import outcome_to_failure_url


def test_outcome_to_failure_url_failed():
  assert outcome_to_failure_url("failed") == "failed"


def test_outcome_to_failure_url_not_found():
  assert outcome_to_failure_url("not_found") == "not found"


def test_outcome_to_failure_url_success_is_none():
  assert outcome_to_failure_url("success") is None
