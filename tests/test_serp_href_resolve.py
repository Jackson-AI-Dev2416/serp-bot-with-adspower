from services.serp_bot import SerpBot


def test_resolve_result_href_google_url_q_param():
  href = (
    "https://www.google.co.kr/url?q=https%3A%2F%2Fwww.kingdomanma.com%2Fsamcheok&sa=U"
  )
  assert SerpBot._resolve_result_href(href) == "https://www.kingdomanma.com/samcheok"


def test_resolve_result_href_google_url_url_param():
  href = (
    "https://www.google.co.kr/url?sa=t&url=https%3A%2F%2Fwww.kingdomanma.com%2Fsamcheok"
  )
  assert SerpBot._resolve_result_href(href) == "https://www.kingdomanma.com/samcheok"


def test_resolve_result_href_relative_google_url():
  href = "/url?q=https%3A%2F%2Fwww.kingdomanma.com%2Fyangyang"
  assert SerpBot._resolve_result_href(href) == "https://www.kingdomanma.com/yangyang"


def test_href_matches_target_after_url_param_resolve():
  href = (
    "https://www.google.co.kr/url?sa=t&url=https%3A%2F%2Fwww.kingdomanma.com%2Fsamcheok"
  )
  resolved = SerpBot._resolve_result_href(href)
  assert SerpBot._href_matches_target(resolved, "kingdomanma.com")
