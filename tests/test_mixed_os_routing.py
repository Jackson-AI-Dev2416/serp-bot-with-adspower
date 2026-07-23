import csv
from pathlib import Path

from utils.serp_result_store import SerpResultStore


def _write_result_csv(path: Path, rows: list[dict]) -> None:
  with path.open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(
      handle,
      fieldnames=["keyword", "site", "device", "page", "rank", "updated_at"],
    )
    writer.writeheader()
    for row in rows:
      writer.writerow(row)


def test_no_mobile_history_is_windows_even_first_run(tmp_path):
  path = tmp_path / "result.csv"
  _write_result_csv(path, [{
    "keyword": "삼척출장마사지",
    "site": "kingdomanma.com",
    "device": "windows",
    "page": "2",
    "rank": "3",
    "updated_at": "2026-07-20 00:00:00",
  }])
  store = SerpResultStore(path)
  assert store.resolve_mixed_profile_os("삼척출장마사지", "kingdomanma.com") == "Windows"
  assert store.mixed_profile_os_reason("삼척출장마사지", "kingdomanma.com") == "windows history page 2"


def test_empty_csv_is_windows(tmp_path):
  path = tmp_path / "result.csv"
  path.write_text("keyword,site,device,page,rank,updated_at\n", encoding="utf-8-sig")
  store = SerpResultStore(path)
  assert store.resolve_mixed_profile_os("새키워드", "kingdomanma.com") == "Windows"
  assert store.mixed_profile_os_reason("새키워드", "kingdomanma.com") == "no history (windows)"


def test_windows_page_one_no_mobile_is_android(tmp_path):
  path = tmp_path / "result.csv"
  _write_result_csv(path, [{
    "keyword": "삼척출장마사지",
    "site": "kingdomanma.com",
    "device": "windows",
    "page": "1",
    "rank": "4",
    "updated_at": "2026-07-20 00:00:00",
  }])
  store = SerpResultStore(path)
  assert store.resolve_mixed_profile_os("삼척출장마사지", "kingdomanma.com") == "Android"
  assert (
    store.mixed_profile_os_reason("삼척출장마사지", "kingdomanma.com")
    == "windows history page 1 (no mobile — try android)"
  )


def test_mobile_page_one_is_android(tmp_path):
  path = tmp_path / "result.csv"
  _write_result_csv(path, [{
    "keyword": "양양출장마사지",
    "site": "kingdomanma.com",
    "device": "mobile",
    "page": "1",
    "rank": "3",
    "updated_at": "2026-07-20 00:00:00",
  }])
  store = SerpResultStore(path)
  assert store.resolve_mixed_profile_os("양양출장마사지", "kingdomanma.com") == "Android"
  assert store.mixed_profile_os_reason("양양출장마사지", "kingdomanma.com") == "mobile history page 1"


def test_mobile_page_two_is_android(tmp_path):
  path = tmp_path / "result.csv"
  _write_result_csv(path, [{
    "keyword": "태백출장안마",
    "site": "kingdomanma.com",
    "device": "mobile",
    "page": "2",
    "rank": "5",
    "updated_at": "2026-07-20 00:00:00",
  }])
  store = SerpResultStore(path)
  assert store.resolve_mixed_profile_os("태백출장안마", "kingdomanma.com") == "Android"
  assert store.mixed_profile_os_reason("태백출장안마", "kingdomanma.com") == "mobile history page 2"


def test_mobile_page_three_plus_is_windows(tmp_path):
  path = tmp_path / "result.csv"
  _write_result_csv(path, [{
    "keyword": "평창출장마사지",
    "site": "kingdomanma.com",
    "device": "mobile",
    "page": "3",
    "rank": "2",
    "updated_at": "2026-07-20 00:00:00",
  }])
  store = SerpResultStore(path)
  assert store.resolve_mixed_profile_os("평창출장마사지", "kingdomanma.com") == "Windows"
  assert store.mixed_profile_os_reason("평창출장마사지", "kingdomanma.com") == "mobile history page 3"
