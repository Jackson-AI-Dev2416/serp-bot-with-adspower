from utils.user_log import build_pre_delete_log_line, format_user_log


def test_build_pre_delete_log_not_found():
  line = build_pre_delete_log_line("s-001", "not_found", "영월출장안마", "kingdomanma.com")
  assert line == "[s-001] not found target site: '영월출장안마' → kingdomanma.com"


def test_build_pre_delete_log_failed_open():
  line = build_pre_delete_log_line("s-004", "failed", "춘천출장마사지", "kingdomanma.com")
  assert line == "[s-004] failed open target site: '춘천출장마사지' → kingdomanma.com"


def test_build_pre_delete_log_error_blocked():
  line = build_pre_delete_log_line("s-002", "blocked", "강릉출장안마", "cocoanma.net")
  assert line == "[s-002] error-blocked: '강릉출장안마' → cocoanma.net"


def test_build_pre_delete_log_skips_success():
  assert build_pre_delete_log_line("s-003", "success", "kw", "site.com") is None


def test_format_user_log_not_found_target_site():
  internal = "[s-006] not found target site: '영월출장안마' → kingdomanma.com"
  assert format_user_log(internal) == (
    "[s-006] Not found target site (영월출장안마 : kingdomanma.com)"
  )


def test_format_user_log_error_blocked():
  internal = "[s-007] error-blocked: '강릉출장안마' → cocoanma.net"
  assert format_user_log(internal) == (
    "[s-007] Error-blocked (강릉출장안마 : cocoanma.net)"
  )


def test_format_user_log_failed_open_target_site():
  internal = "[s-008] failed open target site: '춘천출장마사지' → kingdomanma.com"
  assert format_user_log(internal) == (
    "[s-008] Failed open target site (춘천출장마사지 : kingdomanma.com)"
  )


def test_format_user_log_failed_open_delete_flow():
  target = format_user_log(
    "[s-008] [Target] Failed open target site: '춘천출장마사지' → kingdomanma.com"
  )
  pre_delete = format_user_log(
    "[s-008] failed open target site: '춘천출장마사지' → kingdomanma.com"
  )
  controller = format_user_log(
    "[Controller] s-008 finished (failed), proxy cooldown started"
  )
  deleted = format_user_log(
    "[Worker] Deleted profile s-008 after run (failed); proxy entry kept."
  )
  assert target is None
  assert pre_delete == "[s-008] Failed open target site (춘천출장마사지 : kingdomanma.com)"
  assert controller is None
  assert deleted == "[s-008] Finished - profile removed"


def test_format_user_log_target_click_failure_message():
  internal = "[s-009] [Target] Failed open target site: '춘천출장마사지' → kingdomanma.com"
  assert format_user_log(internal) is None


def test_format_user_log_delete_follows_not_found():
  not_found = format_user_log(
    "[s-006] not found target site: '영월출장안마' → kingdomanma.com"
  )
  controller = format_user_log(
    "[Controller] s-006 finished (not_found), proxy cooldown started"
  )
  deleted = format_user_log("[Worker] Deleted profile s-006 after run (not_found); proxy entry kept.")
  assert not_found == "[s-006] Not found target site (영월출장안마 : kingdomanma.com)"
  assert controller is None
  assert deleted == "[s-006] Finished - profile removed"


def test_format_user_log_hides_checked_all_pages_worker_line():
  worker_line = (
    "[Worker] s-006 checked all available SERP pages for '영월출장안마' "
    "without finding the target. Keyword is kept in the list for future runs."
  )
  assert format_user_log(worker_line) is None


def test_build_pre_delete_log_proxy_connect_failed():
  line = build_pre_delete_log_line(
    "s-002", "proxy_connect_failed", "속초출장안마", "kingdomanma.com",
  )
  assert line == "[s-002] Failed connect proxy"


def test_format_user_log_proxy_connect_delete_flow():
  failure = format_user_log(
    "[s-010] Failed connect proxy (Google not reachable within 90s (chrome-error://chromewebdata/))"
  )
  pre_delete = format_user_log("[s-010] Failed connect proxy")
  deleted = format_user_log(
    "[Worker] Deleted profile s-010 after run (proxy_connect_failed); proxy entry kept."
  )
  assert failure == "[s-010] Failed connect proxy"
  assert pre_delete is None
  assert deleted == "[s-010] Finished - profile removed"


def test_format_user_log_warmup_shows_query_only_once():
  summary = (
    "[s-001] [Warmup] Running 1 warm-up query (configured 1-1, human idle text select)"
  )
  query = "[s-001] [Warmup] Desktop Google entry search box: 부가세 신고기한"
  assert format_user_log(summary) is None
  assert format_user_log(query) == "[s-001] WarmingUp(부가세 신고기한)"


def test_format_user_log_searching_attempt_once():
  internal = (
    "[s-001] Searching attempt 1/1: '속초출장안마' → kingdomanma.com (SERP search box)"
  )
  assert format_user_log(internal) == (
    "[s-001] Searching (속초출장안마 : kingdomanma.com)"
  )
