from services.serp_bot import SerpBot


def test_extract_sorry_continue_url():
  url = (
    "https://www.google.com/sorry/index?continue="
    "https%3A%2F%2Fwww.google.com%2Fsearch%3Fq%3Dtest"
  )
  assert SerpBot._extract_sorry_continue_url(url) == "https://www.google.com/search?q=test"


def test_extract_sorry_continue_url_empty():
  assert SerpBot._extract_sorry_continue_url("https://www.google.com/sorry/index") == ""
