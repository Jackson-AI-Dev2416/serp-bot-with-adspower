"""Google web-search context detection (no Images/Video/Maps tabs)."""

from services.serp_bot import SerpBot


def test_special_tab_detects_images_and_video():
  assert SerpBot._url_is_google_special_search_tab(
    "https://www.google.co.kr/search?q=test&tbm=isch"
  )
  assert SerpBot._url_is_google_special_search_tab(
    "https://www.google.co.kr/search?q=test&tbm=vid"
  )


def test_special_tab_detects_maps():
  assert SerpBot._url_is_google_special_search_tab(
    "https://www.google.co.kr/maps/search/massage"
  )


def test_plain_web_serp_is_not_special():
  assert not SerpBot._url_is_google_special_search_tab(
    "https://www.google.co.kr/search?q=양양출장마사지"
  )
  assert not SerpBot._url_is_google_special_search_tab(
    "https://www.google.co.kr/search?q=test&udm=14"
  )


def test_google_home_is_not_special():
  assert not SerpBot._url_is_google_special_search_tab(
    "https://www.google.co.kr/"
  )
