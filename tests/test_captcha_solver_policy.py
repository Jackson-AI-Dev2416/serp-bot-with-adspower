from unittest.mock import MagicMock, patch

from services.captcha_solver import CaptchaSolver


def _make_solver(api_key: str = "CAP-TEST-KEY") -> CaptchaSolver:
  return CaptchaSolver(api_key, logger=lambda _msg: None)


def test_no_api_key_captcha_returns_blocked():
  solver = _make_solver("")
  solver.api_key = ""
  solver.automated_mode = False
  page = MagicMock()
  page.is_closed.return_value = False

  with patch.object(solver, "_is_captcha_present", return_value=True):
    with patch.object(solver, "_detect_captcha_type", return_value="recaptcha_enterprise"):
      with patch.object(solver, "_log_captcha_event") as log_event:
        result = solver.handle_before_action(page)

  assert result == "blocked"
  log_event.assert_called_once()


def test_automated_captcha_retries_until_success():
  solver = _make_solver()
  page = MagicMock()
  page.is_closed.return_value = False
  attempts = {"count": 0}

  def solve_side_effect(*_args, **_kwargs):
    attempts["count"] += 1
    return "ok" if attempts["count"] >= 2 else "blocked"

  blocking = {"active": True}

  def still_blocking(_page):
    if not blocking["active"]:
      return False
    if attempts["count"] >= 2:
      blocking["active"] = False
      return False
    return True

  with patch.object(solver, "_is_captcha_present", return_value=True):
    with patch.object(solver, "_detect_captcha_type", return_value="recaptcha_enterprise"):
      with patch.object(solver, "_captcha_still_blocking", side_effect=still_blocking):
        with patch.object(solver, "_solve_automated", side_effect=solve_side_effect):
          with patch.object(solver, "_refresh_page_after_captcha", return_value=page):
            with patch.object(solver, "_log_captcha_event"):
              with patch("services.captcha_solver.time.sleep"):
                result = solver.handle_before_action(page)

  assert result == "ok"
  assert solver._automated_attempts == 2


def test_automated_captcha_exhausts_three_attempts_then_blocked():
  solver = _make_solver()
  page = MagicMock()
  page.is_closed.return_value = False

  with patch.object(solver, "_is_captcha_present", return_value=True):
    with patch.object(solver, "_captcha_still_blocking", return_value=True):
      with patch.object(solver, "_detect_captcha_type", return_value="recaptcha_enterprise"):
        with patch.object(solver, "_solve_automated", return_value="blocked"):
          with patch.object(solver, "_log_captcha_event"):
            with patch("services.captcha_solver.time.sleep"):
              result = solver.handle_before_action(page)

  assert result == "blocked"
  assert solver._automated_attempts == CaptchaSolver.MAX_AUTOMATED_SOLVE_ATTEMPTS


def test_reset_session_state_clears_attempt_counter():
  solver = _make_solver()
  solver._automated_attempts = 2
  solver.reset_session_state()
  assert solver._automated_attempts == 0
