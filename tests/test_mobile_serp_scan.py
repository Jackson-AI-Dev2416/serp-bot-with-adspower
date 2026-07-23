from services.serp_bot import SerpBot


def test_slice_mobile_page_two_from_cumulative_dom():
  hrefs = [f"https://example.com/{index}" for index in range(24)]
  window = SerpBot._slice_hrefs_for_mobile_serp_page(hrefs, 2)
  assert window == hrefs[10:20]


def test_slice_mobile_page_two_partial_tail():
  hrefs = [f"https://example.com/{index}" for index in range(15)]
  window = SerpBot._slice_hrefs_for_mobile_serp_page(hrefs, 2)
  assert window == hrefs[10:15]


def test_slice_mobile_page_one_keeps_short_first_page():
  hrefs = [f"https://example.com/{index}" for index in range(12)]
  window = SerpBot._slice_hrefs_for_mobile_serp_page(hrefs, 1)
  assert window == hrefs


def test_page_local_rank_within_window():
  page_hrefs = [
    "https://other.com/a",
    "https://cocoanma.net/pyeongchang",
    "https://other.com/b",
  ]
  rank = SerpBot._page_local_rank_for_href(
    "https://www.cocoanma.net/pyeongchang",
    page_hrefs,
  )
  assert rank == 2


def test_apply_serp_cap_floor_respects_config_max():
  cap = SerpBot._apply_serp_cap_floor(12, history_page=5, max_pages=8)
  assert cap == 8


def test_mobile_effective_cap_while_pagination_available():
  cap = SerpBot._mobile_effective_page_cap(
    served_page_num=1,
    configured_max=5,
    pagination_available=True,
  )
  assert cap == 5


def test_mobile_effective_cap_when_google_ends_on_current_page():
  cap = SerpBot._mobile_effective_page_cap(
    served_page_num=3,
    configured_max=5,
    pagination_available=False,
  )
  assert cap == 3


def test_mobile_effective_cap_page_one_without_pagination():
  cap = SerpBot._mobile_effective_page_cap(
    served_page_num=1,
    configured_max=5,
    pagination_available=False,
  )
  assert cap == 1


def test_pagination_advanced_accepts_start_param_change():
  before = {"url": "https://www.google.co.kr/search?q=test&start=10", "start": 10, "ip_index": 0, "organic_count": 9}
  after = {"url": "https://www.google.co.kr/search?q=test&start=20", "start": 20, "ip_index": 0, "organic_count": 9}
  assert SerpBot._pagination_advanced(before, after, 20) is True


def test_pagination_advanced_rejects_ai_overview_single_link_growth():
  before = {
    "url": "https://www.google.co.kr/search?q=test",
    "start": 0,
    "ip_index": 0,
    "organic_count": 10,
    "scroll_y": 100,
    "doc_height": 2000,
  }
  after = {
    "url": "https://www.google.co.kr/search?q=test",
    "start": 0,
    "ip_index": 0,
    "organic_count": 11,
    "scroll_y": 120,
    "doc_height": 2100,
  }
  assert SerpBot._pagination_advanced(before, after, 10) is False


def test_pagination_advanced_accepts_infinite_scroll_batch():
  before = {
    "url": "https://www.google.co.kr/search?q=test",
    "start": 0,
    "ip_index": 0,
    "organic_count": 10,
    "scroll_y": 100,
    "doc_height": 2000,
  }
  after = {
    "url": "https://www.google.co.kr/search?q=test",
    "start": 0,
    "ip_index": 0,
    "organic_count": 15,
    "scroll_y": 500,
    "doc_height": 2600,
  }
  assert SerpBot._pagination_advanced(before, after, 10) is True


def test_pagination_advanced_accepts_in_place_result_batch_replacement():
  before = {
    "url": "https://www.google.com/search?q=test#ip=1",
    "start": 0,
    "ip_index": 1,
    "organic_count": 10,
    "organic_keys": [f"old.example/{index}" for index in range(10)],
    "scroll_y": 2000,
    "doc_height": 4000,
  }
  after = {
    "url": "https://www.google.com/search?q=test#ip=1",
    "start": 0,
    "ip_index": 1,
    "organic_count": 10,
    "organic_keys": [f"new.example/{index}" for index in range(10)],
    "scroll_y": 2000,
    "doc_height": 4000,
  }
  assert SerpBot._pagination_advanced(before, after, 20) is True


def test_pagination_advanced_rejects_same_result_batch():
  keys = [f"same.example/{index}" for index in range(10)]
  before = {
    "url": "https://www.google.com/search?q=test#ip=1",
    "start": 0,
    "ip_index": 1,
    "organic_count": 10,
    "organic_keys": keys,
    "scroll_y": 2000,
    "doc_height": 4000,
  }
  after = dict(before)
  assert SerpBot._pagination_advanced(before, after, 20) is False


def test_mobile_effective_cap_strict_no_false_pagination():
  cap = SerpBot._mobile_effective_page_cap(
    served_page_num=2,
    configured_max=5,
    pagination_available=False,
  )
  assert cap == 2


def test_global_rank_to_page_local_rank_32():
  page, rank = SerpBot._global_organic_rank_to_page_local(32)
  assert page == 4
  assert rank == 2


def test_global_rank_to_page_local_rank_30():
  page, rank = SerpBot._global_organic_rank_to_page_local(30)
  assert page == 3
  assert rank == 10


def test_overall_rank_from_page_local():
  assert SerpBot._overall_rank_from_page_local(4, 2) == 32
  assert SerpBot._overall_rank_from_page_local(3, 10) == 30


def test_slice_mobile_page_three_window():
  hrefs = [f"https://example.com/{index}" for index in range(30)]
  window = SerpBot._slice_hrefs_for_mobile_serp_page(hrefs, 3)
  assert window == hrefs[20:30]


def test_mobile_hrefs_after_baseline_returns_only_appended_batch():
  baseline = [f"https://example.com/{index}" for index in range(10)]
  appended = [f"https://next.example/{index}" for index in range(10)]
  current, cumulative = SerpBot._mobile_hrefs_after_baseline(
    baseline + appended,
    baseline,
  )
  assert cumulative is True
  assert current == appended


def test_mobile_hrefs_after_baseline_accepts_replaced_page_dom():
  baseline = [f"https://old.example/{index}" for index in range(10)]
  replacement = [f"https://new.example/{index}" for index in range(10)]
  current, cumulative = SerpBot._mobile_hrefs_after_baseline(
    replacement,
    baseline,
  )
  assert cumulative is False
  assert current == replacement


def test_mobile_hrefs_after_baseline_dedupes_www_equivalent_old_results():
  baseline = [
    "https://www.example.com/a",
    "https://www.example.com/b",
    "https://www.example.com/c",
  ]
  appended = ["https://target.example/new"]
  current, cumulative = SerpBot._mobile_hrefs_after_baseline(
    [
      "https://example.com/a",
      "https://example.com/b",
      "https://example.com/c",
      *appended,
    ],
    baseline,
  )
  assert cumulative is True
  assert current == appended


def test_merge_organic_href_lists_keeps_early_results():
  first = [f"https://early.example/{index}" for index in range(5)]
  later = [f"https://later.example/{index}" for index in range(5)]
  merged = SerpBot._merge_organic_href_lists(first, later, first)
  assert merged[:5] == first
  assert merged[5:] == later
  assert len(merged) == 10


def test_mobile_pagination_href_rejects_stale_previous_start():
  stale = "/search?q=test&start=10&sa=N"
  assert SerpBot._is_expected_mobile_pagination_href(stale, 20) is False


def test_mobile_usable_more_href_accepts_soft_stale_footer_control():
  stale = "/search?q=test&start=10&sa=N"
  assert SerpBot._is_usable_mobile_more_href(
    stale, 20, min_start=10, allow_soft=True,
  ) is True
  assert SerpBot._is_usable_mobile_more_href(
    stale, 20, min_start=10, allow_soft=False,
  ) is False


def test_mobile_usable_more_href_accepts_exact_and_newer_start():
  exact = "/search?q=test&start=20&sa=N"
  newer = "/search?q=test&start=30&sa=N"
  assert SerpBot._is_usable_mobile_more_href(exact, 20, min_start=10) is True
  assert SerpBot._is_usable_mobile_more_href(newer, 20, min_start=10) is True


def test_mobile_pagination_href_accepts_exact_next_start():
  current = "/search?q=test&start=20&sa=N"
  assert SerpBot._is_expected_mobile_pagination_href(current, 20) is True


def test_mobile_pagination_href_rejects_non_google_target():
  target = "https://www.cocoanma.net/yeongwol?start=20"
  assert SerpBot._is_expected_mobile_pagination_href(target, 20) is False


def test_pagination_advanced_accepts_ip_mode_small_key_growth():
  before = {
    "url": "https://www.google.com/search?q=test#ip=1",
    "start": 0,
    "ip_index": 1,
    "organic_count": 10,
    "organic_keys": [f"a.example/{index}" for index in range(10)],
  }
  after = {
    "url": "https://www.google.com/search?q=test#ip=1",
    "start": 0,
    "ip_index": 1,
    "organic_count": 12,
    "organic_keys": (
      [f"a.example/{index}" for index in range(10)]
      + ["new1.example/x", "new2.example/y"]
    ),
  }
  assert SerpBot._pagination_advanced(before, after, 20) is True


def test_next_serp_start_offset_uses_visible_stale_footer_on_ip_serp():
  class _Page:
    url = "https://www.google.com/search?q=test#ip=1"

  class _Profile:
    device_label = "Android"

  bot = SerpBot.__new__(SerpBot)
  bot._mobile_serp_page = 2
  bot._is_mobile_profile = lambda profile: True  # type: ignore[method-assign]
  bot._current_serp_start_offset = lambda page: 0  # type: ignore[method-assign]
  bot._visible_mobile_more_href_start = lambda page: 10  # type: ignore[method-assign]
  assert bot._next_serp_start_offset(_Page(), _Profile()) == 10


def test_mobile_next_href_accepts_js_control_without_href():
  assert SerpBot._is_safe_mobile_next_href("", 20) is True


def test_mobile_next_href_accepts_google_search_without_start():
  assert SerpBot._is_safe_mobile_next_href("/search?q=test", 20) is True


def test_mobile_next_href_accepts_soft_previous_start_on_footer():
  assert SerpBot._is_safe_mobile_next_href("/search?q=test&start=10", 20) is True


def test_mobile_next_href_rejects_too_old_start():
  assert SerpBot._is_safe_mobile_next_href("/search?q=test&start=0", 20) is False
  assert SerpBot._is_safe_mobile_next_href("/search?q=test&start=10", 40) is False

def test_mobile_next_href_rejects_local_and_external_controls():
  assert SerpBot._is_safe_mobile_next_href("/search?q=test&udm=1", 20) is False
  assert SerpBot._is_safe_mobile_next_href("https://example.com/next", 20) is False


def test_mobile_next_control_accepts_footer_arrow_and_semantics():
  assert SerpBot._is_mobile_next_control_semantics(">", "", "", "") is True
  assert SerpBot._is_mobile_next_control_semantics("", "다음", "", "") is True
  assert SerpBot._is_mobile_next_control_semantics("", "", "next", "") is True
  assert SerpBot._is_mobile_next_control_semantics("", "", "", "pnnext") is True


def test_mobile_next_control_rejects_unrelated_more_button():
  assert SerpBot._is_mobile_next_control_semantics("장소 더보기", "", "", "") is False


def test_mobile_more_label_rejects_business_pack():
  assert not SerpBot._is_mobile_serp_footer_more_label("비즈니스 더보기 >")
  assert not SerpBot._is_mobile_search_results_more_label("비즈니스 더보기")
  assert not SerpBot._is_mobile_serp_footer_more_label("비지니스 더보기")
  assert SerpBot._is_mobile_serp_footer_more_label("검색결과 더보기")


def test_primary_mobile_footer_more_accepts_no_href():
  assert SerpBot._is_primary_mobile_footer_more_control(aria="검색결과 더보기", jsname="")
  assert SerpBot._is_primary_mobile_footer_more_control(aria="", jsname="oHxHid")
  assert not SerpBot._is_primary_mobile_footer_more_control(aria="비즈니스 더보기", jsname="")
  assert not SerpBot._is_primary_mobile_footer_more_control(aria="장소 더보기", jsname="")

