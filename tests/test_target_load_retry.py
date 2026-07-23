from unittest.mock import MagicMock, patch

from config.bot_config import BotConfig
from services.adspower_manager import ProfileSpec
from services.serp_bot import SerpBot


def test_is_transient_browser_tab_url():
  assert SerpBot._is_transient_browser_tab_url("chrome-error://chromewebdata/") is True
  assert SerpBot._is_transient_browser_tab_url("about:blank") is True
  assert SerpBot._is_transient_browser_tab_url("https://kingdomanma.com/") is False


def test_target_click_navigation_started():
  page = MagicMock()
  page.url = "chrome-error://chromewebdata/"
  assert SerpBot._target_click_navigation_started(page) is True

  page.url = "https://www.google.co.kr/search?q=test"
  assert SerpBot._target_click_navigation_started(page) is False

  page.url = "https://www.kingdomanma.com/samcheok"
  assert SerpBot._target_click_navigation_started(page) is True


def _make_bot(target_domain: str = "kingdomanma.com") -> SerpBot:
  config = BotConfig(
    capsolver_api_key="",
    target_domain=target_domain,
  )
  bot = SerpBot(config, lambda _msg: None)
  return bot


def _profile(os_type: str) -> ProfileSpec:
  return ProfileSpec(
    profile_id="p1",
    name="p1",
    proxy_host="127.0.0.1",
    proxy_port=8080,
    proxy_user="u",
    proxy_pass="p",
    os_type=os_type,
  )


def test_retry_target_load_skips_when_still_on_google():
  bot = _make_bot()
  page = MagicMock()
  page.url = "https://www.google.co.kr/search?q=test"
  profile = _profile("windows")

  ok, returned = bot._retry_target_load_after_click(
    page,
    "https://www.kingdomanma.com/samcheok",
    profile,
    "삼척출장안마",
  )

  assert ok is False
  assert returned is page
  page.reload.assert_not_called()


def test_retry_target_load_uses_goto_then_reload():
  bot = _make_bot()
  page = MagicMock()
  page.url = "chrome-error://chromewebdata/"
  profile = _profile("windows")
  href = "https://www.kingdomanma.com/samcheok"
  reloaded = {"done": False}

  def reload_side_effect(*_args, **_kwargs):
    reloaded["done"] = True

  page.reload.side_effect = reload_side_effect

  def safe_goto_side_effect(*_args, **_kwargs):
    page.url = "https://httpstat.us/200"
    return page

  with patch.object(
    bot,
    "_target_page_load_confirmed",
    side_effect=lambda _page: reloaded["done"],
  ):
    with patch.object(bot, "_safe_goto", side_effect=safe_goto_side_effect) as safe_goto:
      with patch("services.serp_bot.random.uniform", return_value=0.01):
        with patch("services.serp_bot.time.sleep"):
          ok, returned = bot._retry_target_load_after_click(
            page,
            href,
            profile,
            "삼척출장안마",
          )

  assert ok is True
  assert returned is page
  safe_goto.assert_called_once()
  page.reload.assert_called_once()
  page.bring_to_front.assert_called()


def test_retry_target_load_mobile_skips_bring_to_front():
  bot = _make_bot()
  page = MagicMock()
  page.url = "chrome-error://chromewebdata/"
  profile = _profile("android")

  with patch.object(bot, "_target_page_load_confirmed", return_value=True):
    with patch.object(bot, "_safe_goto", return_value=page):
      ok, _ = bot._retry_target_load_after_click(
        page,
        "https://www.kingdomanma.com/samcheok",
        profile,
        "삼척출장안마",
      )

  assert ok is True
  page.bring_to_front.assert_not_called()
