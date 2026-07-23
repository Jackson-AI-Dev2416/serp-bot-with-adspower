from utils.page_search_order import build_desktop_page_order, pending_desktop_scan_pages


def test_z_greater_than_y():
  assert build_desktop_page_order(4, 6, 8) == [4, 3, 5, 2, 6]


def test_z_less_than_y():
  assert build_desktop_page_order(8, 5, 8) == [5, 4, 3, 2]


def test_no_history():
  assert build_desktop_page_order(None, 6, 8) == [2, 3, 4, 5, 6]


def test_cap_at_one():
  assert build_desktop_page_order(4, 1, 8) == []


def test_pending_desktop_scan_pages_after_last_google_page():
  order = [1, 5, 4, 3, 2]
  visited = {1, 5}
  assert pending_desktop_scan_pages(order, visited, 5) == [4, 3, 2]


def test_pending_desktop_scan_pages_empty_when_complete():
  order = [1, 5, 4, 3, 2]
  visited = {1, 2, 3, 4, 5}
  assert pending_desktop_scan_pages(order, visited, 5) == []
