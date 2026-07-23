import time
from unittest.mock import MagicMock, patch

from config.bot_config import BotConfig
from services.serp_bot import SerpBot
from utils.user_log import build_pre_delete_log_line, format_user_log


def _make_bot() -> SerpBot:
  return SerpBot(BotConfig(capsolver_api_key="", target_domain="kingdomanma.com"), lambda _msg: None)


def test_is_google_connect_ready_rejects_chrome_error():
  bot = _make_bot()
  page = MagicMock()
  page.is_closed.return_value = False
  page.url = "chrome-error://chromewebdata/"
  with patch.object(bot, "_is_google_search_box_visible", return_value=False):
    with patch.object(bot, "_is_on_google_serp", return_value=False):
      assert bot._is_google_connect_ready(page) is False


def test_is_google_connect_ready_accepts_google_serp():
  bot = _make_bot()
  page = MagicMock()
  page.is_closed.return_value = False
  page.url = "https://www.google.co.kr/search?q=test"
  with patch.object(bot, "_is_on_google_serp", return_value=True):
    assert bot._is_google_connect_ready(page) is True


def test_check_google_proxy_connect_waits_before_deadline():
  bot = _make_bot()
  bot._google_connect_deadline = time.time() + 60.0
  page = MagicMock()
  page.is_closed.return_value = False
  page.url = "chrome-error://chromewebdata/"
  logs: list[str] = []

  with patch.object(bot, "_is_google_connect_ready", return_value=False):
    _, outcome = bot._check_google_proxy_connect(page, logs.append)

  assert outcome is None
  assert logs == []


def test_check_google_proxy_connect_fails_after_deadline():
  bot = _make_bot()
  bot._google_connect_deadline = time.time() - 1.0
  page = MagicMock()
  page.is_closed.return_value = False
  page.url = "chrome-error://chromewebdata/"
  logs: list[str] = []

  with patch.object(bot, "_is_google_connect_ready", return_value=False):
    _, outcome = bot._check_google_proxy_connect(page, logs.append)

  assert outcome == "proxy_connect_failed"
  assert len(logs) == 1
  assert "Failed connect proxy" in logs[0]


def test_user_log_proxy_connect_failed():
  internal = "[s-002] Failed connect proxy (Google not reachable within 90s)"
  assert format_user_log(internal) == "[s-002] Failed connect proxy"

  pre_delete = build_pre_delete_log_line(
    "s-002",
    "proxy_connect_failed",
    "속초출장안마",
    "kingdomanma.com",
  )
  assert pre_delete == "[s-002] Failed connect proxy"
  assert format_user_log(pre_delete) is None
