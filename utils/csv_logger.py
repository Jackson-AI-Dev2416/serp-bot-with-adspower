import csv
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TypedDict
from urllib.parse import urlparse

_SESSION_META_MARKER = "#meta"


class KeywordClickSummary(TypedDict):
    keyword: str
    total: int
    windows: int
    mobile: int

_RESULT_FILENAME_RE = re.compile(r"^result_(\d{8})_(\d{6})\.csv$", re.IGNORECASE)

# Excel on Windows opens CSV as ANSI/CP949 unless a UTF-8 BOM is present.
_CSV_ENCODING = "utf-8-sig"


def _ensure_csv_utf8_bom(filepath: Path) -> None:
    if not filepath.exists() or filepath.stat().st_size == 0:
        return
    try:
        with filepath.open("rb") as handle:
            if handle.read(3) == b"\xef\xbb\xbf":
                return
        with filepath.open("r", encoding="utf-8", newline="") as handle:
            content = handle.read()
        with filepath.open("w", encoding=_CSV_ENCODING, newline="") as handle:
            handle.write(content)
    except PermissionError:
        return


def new_session_click_log_path(data_dir: str | Path = "data") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(data_dir) / f"result_{stamp}.csv"


class SessionClickCsvLogger:
    _HEADERS = ("datetime", "profile_name", "device", "keyword", "url", "page", "rank", "overall_rank")
    _PATH_LOCKS: dict[str, threading.Lock] = {}

    def __init__(self, filepath: str | Path, *, target_domains: list[str] | None = None):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.filepath.resolve())
        if key not in self._PATH_LOCKS:
            self._PATH_LOCKS[key] = threading.Lock()
        self._lock = self._PATH_LOCKS[key]
        with self._lock:
            if not self.filepath.exists() or self.filepath.stat().st_size == 0:
                with self.filepath.open("w", newline="", encoding=_CSV_ENCODING) as handle:
                    writer = csv.writer(handle)
                    writer.writerow(self._HEADERS)
                    if target_domains:
                        writer.writerow(self._meta_row("target_domains", self._format_domains(target_domains)))

    @staticmethod
    def _format_domains(domains: list[str]) -> str:
        seen: set[str] = set()
        ordered: list[str] = []
        for domain in domains:
            cleaned = (domain or "").strip()
            if not cleaned:
                continue
            key = _normalize_host(cleaned)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(cleaned)
        return "|".join(ordered)

    @staticmethod
    def _meta_row(key: str, value: str) -> tuple[str, str, str]:
        return (_SESSION_META_MARKER, key, value)

    @classmethod
    def write_target_domains_meta(cls, filepath: str | Path, domains: list[str]) -> None:
        path = Path(filepath)
        if not domains:
            return
        key = str(path.resolve())
        lock = cls._PATH_LOCKS.setdefault(key, threading.Lock())
        with lock:
            rows: list[list[str]] = []
            if path.exists() and path.stat().st_size > 0:
                with path.open("r", newline="", encoding=_CSV_ENCODING) as handle:
                    rows = list(csv.reader(handle))
            if not rows:
                with path.open("w", newline="", encoding=_CSV_ENCODING) as handle:
                    writer = csv.writer(handle)
                    writer.writerow(cls._HEADERS)
                    writer.writerow(cls._meta_row("target_domains", cls._format_domains(domains)))
                return
            kept = [rows[0]]
            for row in rows[1:]:
                if _is_session_meta_row(row) and len(row) >= 2 and str(row[1]).strip() == "target_domains":
                    continue
                kept.append(row)
            meta_inserted = False
            rebuilt: list[list[str]] = [kept[0]]
            for row in kept[1:]:
                if not meta_inserted and not _is_session_meta_row(row):
                    rebuilt.append(list(cls._meta_row("target_domains", cls._format_domains(domains))))
                    meta_inserted = True
                rebuilt.append(row)
            if not meta_inserted:
                rebuilt.append(list(cls._meta_row("target_domains", cls._format_domains(domains))))
            with path.open("w", newline="", encoding=_CSV_ENCODING) as handle:
                csv.writer(handle).writerows(rebuilt)

    @classmethod
    def finalize_session(cls, filepath: str | Path, *, traffic_bytes: int) -> None:
        path = Path(filepath)
        if not path.exists():
            return
        key = str(path.resolve())
        lock = cls._PATH_LOCKS.setdefault(key, threading.Lock())
        with lock:
            with path.open("r", newline="", encoding=_CSV_ENCODING) as handle:
                rows = list(csv.reader(handle))
            if not rows:
                return
            kept: list[list[str]] = [rows[0]]
            for row in rows[1:]:
                if _is_session_meta_row(row) and len(row) >= 2 and str(row[1]).strip() == "traffic_bytes":
                    continue
                kept.append(row)
            kept.append(list(cls._meta_row("traffic_bytes", str(max(0, int(traffic_bytes))))))
            with path.open("w", newline="", encoding=_CSV_ENCODING) as handle:
                csv.writer(handle).writerows(kept)

    def log(
        self,
        *,
        profile_name: str,
        device: str,
        keyword: str,
        url: str,
        page: int,
        rank: int,
        overall_rank: int,
    ) -> None:
        row = (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            profile_name,
            (device or "").strip() or "Unknown",
            keyword,
            (url or "").strip(),
            page,
            rank,
            overall_rank,
        )
        with self._lock:
            with self.filepath.open("a", newline="", encoding=_CSV_ENCODING) as handle:
                csv.writer(handle).writerow(row)

    @staticmethod
    def count_rows(filepath: str | Path) -> int:
        _headers, rows, _meta = read_session_click_file(filepath)
        return len(rows)

    @staticmethod
    def read_rows(filepath: str | Path) -> tuple[list[str], list[list[str]]]:
        headers, rows, _meta = read_session_click_file(filepath)
        return headers, rows


def _header_index(headers: list[str], name: str) -> int:
    lowered = [header.strip().lower() for header in headers]
    target = name.strip().lower()
    try:
        return lowered.index(target)
    except ValueError:
        return -1


def _is_windows_device(device: str) -> bool:
    lowered = (device or "").strip().lower()
    return lowered.startswith("windows") or lowered.startswith("win")


def _is_mobile_device(device: str) -> bool:
    lowered = (device or "").strip().lower()
    return lowered in {"android", "ios", "mobile"} or lowered.startswith("android")


def _is_session_meta_row(row: list) -> bool:
    return bool(row) and str(row[0]).strip().lower() == _SESSION_META_MARKER


def _normalize_host(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned.startswith("http://"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("https://"):
        cleaned = cleaned[8:]
    cleaned = cleaned.split("/", 1)[0].strip()
    return cleaned.removeprefix("www.")


def url_matches_target_domain(url: str, domain: str) -> bool:
    host = _normalize_host(urlparse((url or "").strip()).netloc or (url or "").strip())
    target = _normalize_host(domain)
    if not host or not target:
        return False
    return host == target or host.endswith(f".{target}")


def parse_session_meta_rows(rows: list[list[str]]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for row in rows:
        if not _is_session_meta_row(row) or len(row) < 3:
            continue
        meta[str(row[1]).strip()] = str(row[2]).strip()
    return meta


def read_session_click_file(filepath: str | Path) -> tuple[list[str], list[list[str]], dict[str, str]]:
    path = Path(filepath)
    if not path.exists() or path.stat().st_size == 0:
        return [], [], {}
    with path.open("r", newline="", encoding=_CSV_ENCODING) as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    if not rows:
        return [], [], {}
    headers = [str(cell).strip() for cell in rows[0]]
    data: list[list[str]] = []
    meta_rows: list[list[str]] = []
    for row in rows[1:]:
        if not any(str(cell).strip() for cell in row):
            continue
        if _is_session_meta_row(row):
            meta_rows.append(row)
            continue
        data.append(list(row))
    return headers, data, parse_session_meta_rows(meta_rows)


def session_target_domains(
    headers: list[str],
    rows: list[list[str]],
    meta: dict[str, str],
) -> list[str]:
    raw = (meta.get("target_domains") or "").strip()
    if raw:
        seen: set[str] = set()
        domains: list[str] = []
        for part in raw.split("|"):
            cleaned = part.strip()
            if not cleaned:
                continue
            key = _normalize_host(cleaned)
            if key in seen:
                continue
            seen.add(key)
            domains.append(cleaned)
        if domains:
            return domains

    url_idx = _header_index(headers, "url")
    if url_idx < 0:
        return []
    seen_hosts: set[str] = set()
    domains: list[str] = []
    for row in rows:
        url = (row[url_idx] if url_idx < len(row) else "").strip()
        host = _normalize_host(urlparse(url).netloc or url)
        if not host or host in seen_hosts:
            continue
        seen_hosts.add(host)
        domains.append(host)
    return domains


def filter_click_rows_by_domain(
    headers: list[str],
    rows: list[list[str]],
    domain: str,
) -> list[list[str]]:
    cleaned = (domain or "").strip()
    if not cleaned or cleaned.lower() == "all":
        return list(rows)
    url_idx = _header_index(headers, "url")
    if url_idx < 0:
        return list(rows)
    return [
        row
        for row in rows
        if url_matches_target_domain(row[url_idx] if url_idx < len(row) else "", cleaned)
    ]


def aggregate_keyword_clicks_for_domain(
    headers: list[str],
    rows: list[list[str]],
    domain: str,
) -> list[KeywordClickSummary]:
    url_idx = _header_index(headers, "url")
    if url_idx < 0:
        return aggregate_keyword_clicks(headers, rows)
    filtered = [
        row
        for row in rows
        if url_matches_target_domain(row[url_idx] if url_idx < len(row) else "", domain)
    ]
    return aggregate_keyword_clicks(headers, filtered)


def aggregate_keyword_clicks(headers: list[str], rows: list[list[str]]) -> list[KeywordClickSummary]:
    keyword_idx = _header_index(headers, "keyword")
    device_idx = _header_index(headers, "device")
    if keyword_idx < 0:
        return []

    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        keyword = (row[keyword_idx] if keyword_idx < len(row) else "").strip()
        if not keyword:
            continue
        device = (row[device_idx] if device_idx >= 0 and device_idx < len(row) else "").strip()
        bucket = totals.setdefault(keyword, {"total": 0, "windows": 0, "mobile": 0})
        bucket["total"] += 1
        if _is_windows_device(device):
            bucket["windows"] += 1
        elif _is_mobile_device(device):
            bucket["mobile"] += 1

    summaries: list[KeywordClickSummary] = [
        {
            "keyword": keyword,
            "total": counts["total"],
            "windows": counts["windows"],
            "mobile": counts["mobile"],
        }
        for keyword, counts in totals.items()
    ]
    summaries.sort(key=lambda item: (-item["total"], item["keyword"].casefold()))
    return summaries


def parse_result_file_stamp(filepath: str | Path) -> Optional[datetime]:
    match = _RESULT_FILENAME_RE.match(Path(filepath).name)
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)}_{match.group(2)}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def format_result_session_label(filepath: str | Path) -> str:
    stamp = parse_result_file_stamp(filepath)
    if stamp is None:
        return Path(filepath).name
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def list_session_result_files(data_dir: str | Path = "data") -> list[Path]:
    root = Path(data_dir)
    if not root.exists():
        return []
    files = [path for path in root.glob("result_*.csv") if _RESULT_FILENAME_RE.match(path.name)]
    files.sort(key=lambda path: parse_result_file_stamp(path) or datetime.min, reverse=True)
    return files


def session_result_window(
    filepath: str | Path,
    all_files: list[Path] | None = None,
) -> tuple[Optional[datetime], Optional[datetime]]:
    path = Path(filepath)
    start = parse_result_file_stamp(path)
    if start is None:
        return None, None
    files = all_files if all_files is not None else list_session_result_files(path.parent)
    ordered = sorted(files, key=lambda item: parse_result_file_stamp(item) or datetime.min)
    try:
        index = ordered.index(path.resolve())
    except ValueError:
        try:
            index = next(i for i, item in enumerate(ordered) if item.name == path.name)
        except StopIteration:
            return start, None
    if index + 1 < len(ordered):
        return start, parse_result_file_stamp(ordered[index + 1])
    return start, None


def count_target_not_found_in_session(
    session_start: datetime,
    session_end_exclusive: Optional[datetime] = None,
    *,
    session_log_path: str | Path = "data/session.log",
) -> int:
    log_path = Path(session_log_path)
    if not log_path.exists():
        return 0
    count = 0
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "Target not found for" not in line:
                continue
            try:
                stamp = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if stamp < session_start:
                continue
            if session_end_exclusive is not None and stamp >= session_end_exclusive:
                continue
            count += 1
    return count


class CsvRankLogger:
    _HEADERS = ("timestamp", "keyword", "target_domain", "page", "rank", "profile_name")

    def __init__(self, filepath: str | Path):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock:
            try:
                if self.filepath.exists():
                    _ensure_csv_utf8_bom(self.filepath)
                if not self.filepath.exists():
                    with self.filepath.open("w", newline="", encoding=_CSV_ENCODING) as f:
                        csv.writer(f).writerow(self._HEADERS)
            except PermissionError:
                pass

    def log(self, keyword: str, target_domain: str, page: int, rank: int, profile_name: str) -> None:
        row = (
            datetime.now(timezone.utc).isoformat(),
            keyword,
            target_domain,
            page,
            rank,
            profile_name,
        )
        with self._lock:
            with self.filepath.open("a", newline="", encoding=_CSV_ENCODING) as f:
                csv.writer(f).writerow(row)

    def count_clicks(self, target_domain: str | None = None) -> int:
        if not self.filepath.exists():
            return 0
        filter_domain = self._normalize_domain(target_domain) if target_domain else None
        count = 0
        with self._lock:
            with self.filepath.open("r", newline="", encoding=_CSV_ENCODING) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if filter_domain:
                        row_domain = self._normalize_domain(row.get("target_domain", ""))
                        if row_domain != filter_domain:
                            continue
                    count += 1
        return count

    @staticmethod
    def _normalize_domain(value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned.startswith("http://"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("https://"):
            cleaned = cleaned[8:]
        cleaned = cleaned.split("/")[0].strip()
        return cleaned.removeprefix("www.")


class KeywordHistoryLogger:
    _HEADERS = ("timestamp", "keyword", "page", "rank", "total_rank", "device")
    _MERGED_DOMAINS: set[str] = set()

    @staticmethod
    def _device_bucket(device: str = "", *, mobile: Optional[bool] = None) -> str:
        if mobile is True:
            return "mobile"
        if mobile is False:
            return "windows"
        if _is_mobile_device(device):
            return "mobile"
        if _is_windows_device(device):
            return "windows"
        return "legacy"

    @staticmethod
    def _page_hint_from_row(row: dict) -> Optional[int]:
        total_rank = KeywordHistoryLogger._to_int(str(row.get("total_rank", "")))
        page = KeywordHistoryLogger._to_int(str(row.get("page", "")))
        if total_rank and total_rank > 0:
            return max(1, ((total_rank - 1) // 10) + 1)
        if page and page > 0:
            return page
        return None

    @classmethod
    def _row_matches_device_pool(
        cls,
        row: dict,
        *,
        mobile: bool,
        max_pages: int,
    ) -> bool:
        bucket = cls._device_bucket(row.get("device") or "")
        page = cls._page_hint_from_row(row)
        if page is None:
            return False
        if bucket == "mobile":
            return mobile
        if bucket == "windows":
            return not mobile
        # Legacy rows without device: shallow pages for mobile, desktop-safe depths for windows.
        if mobile:
            return page <= 2
        return page >= 3 and page <= max(1, int(max_pages))

    def __init__(self, target_domain: str, data_dir: str | Path = "data"):
        self.target_domain = (target_domain or "").strip()
        self.data_dir = Path(data_dir)
        domain_key = self._domain_key(self.target_domain)
        filename = f"{domain_key}_last_keyword.csv"
        self.filepath = self.data_dir / filename
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock:
            try:
                if self.filepath.exists():
                    _ensure_csv_utf8_bom(self.filepath)
                if domain_key not in KeywordHistoryLogger._MERGED_DOMAINS:
                    self._merge_accumulated_history()
                    KeywordHistoryLogger._MERGED_DOMAINS.add(domain_key)
                elif not self.filepath.exists():
                    with self.filepath.open("w", newline="", encoding=_CSV_ENCODING) as f:
                        csv.writer(f).writerow(self._HEADERS)
            except PermissionError:
                pass

    def log(
        self,
        keyword: str,
        page: int,
        rank: int,
        total_rank: int,
        *,
        mobile: bool = False,
    ) -> None:
        cleaned = (keyword or "").strip()
        if not cleaned:
            return
        device = "Android" if mobile else "Windows"
        row = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "keyword": cleaned,
            "page": int(page),
            "rank": int(rank),
            "total_rank": int(total_rank),
            "device": device,
        }
        with self._lock:
            records = self._read_records_unlocked()
            bucket = self._device_bucket(device)
            records[(cleaned, bucket)] = row
            try:
                self._write_records_unlocked(records)
            except PermissionError:
                pass

    def _read_records_unlocked(self) -> dict[tuple[str, str], dict]:
        records: dict[tuple[str, str], dict] = {}
        if not self.filepath.exists():
            return records
        with self.filepath.open("r", newline="", encoding=_CSV_ENCODING) as f:
            reader = csv.DictReader(f)
            for row in reader:
                keyword = (row.get("keyword") or "").strip()
                if not keyword:
                    continue
                page = self._to_int(row.get("page", ""))
                rank = self._to_int(row.get("rank", ""))
                total_rank = self._to_int(row.get("total_rank", ""))
                if not page and not total_rank:
                    continue
                if not total_rank and page and rank:
                    total_rank = ((page - 1) * 10) + rank
                device = (row.get("device") or "").strip()
                bucket = self._device_bucket(device)
                records[(keyword, bucket)] = {
                    "timestamp": (row.get("timestamp") or "").strip()
                    or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "keyword": keyword,
                    "page": page or max(1, ((total_rank - 1) // 10) + 1 if total_rank else 1),
                    "rank": rank or 1,
                    "total_rank": total_rank or 1,
                    "device": device,
                }
        return records

    def _write_records_unlocked(self, records: dict[tuple[str, str], dict]) -> None:
        ordered = sorted(
            records.values(),
            key=lambda item: (item.get("timestamp") or "", item.get("keyword") or ""),
        )
        with self.filepath.open("w", newline="", encoding=_CSV_ENCODING) as f:
            writer = csv.writer(f)
            writer.writerow(self._HEADERS)
            for row in ordered:
                writer.writerow([
                    row["timestamp"],
                    row["keyword"],
                    row["page"],
                    row["rank"],
                    row["total_rank"],
                    row.get("device", ""),
                ])

    def _merge_accumulated_history(self) -> None:
        records = self._read_records_unlocked()
        before = len(records)
        records = self._merge_results_csv(records)
        records = self._merge_session_result_files(records)
        self._write_records_unlocked(records)
        added = len(records) - before
        if added > 0 or before != len(records):
            pass  # merged silently; file now has one row per keyword

    def _merge_results_csv(self, records: dict[str, dict]) -> dict[str, dict]:
        results_path = self.data_dir / "results.csv"
        if not results_path.exists():
            return records
        try:
            with results_path.open("r", newline="", encoding=_CSV_ENCODING) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row_domain = self._domain_key(row.get("target_domain", ""))
                    if row_domain != self._domain_key(self.target_domain):
                        continue
                    keyword = (row.get("keyword") or "").strip()
                    page = self._to_int(row.get("page", ""))
                    rank = self._to_int(row.get("rank", ""))
                    if not keyword or not page:
                        continue
                    total_rank = ((page - 1) * 10) + (rank or 1)
                    self._upsert_record(
                        records,
                        keyword=keyword,
                        timestamp=(row.get("timestamp") or "").strip()[:19].replace("T", " "),
                        page=page,
                        rank=rank or 1,
                        total_rank=total_rank,
                    )
        except PermissionError:
            pass
        return records

    def _merge_session_result_files(self, records: dict[str, dict]) -> dict[str, dict]:
        for path in list_session_result_files(self.data_dir):
            try:
                headers, rows = SessionClickCsvLogger.read_rows(path)
            except Exception:
                continue
            keyword_idx = _header_index(headers, "keyword")
            page_idx = _header_index(headers, "page")
            rank_idx = _header_index(headers, "rank")
            overall_idx = _header_index(headers, "overall_rank")
            dt_idx = _header_index(headers, "datetime")
            device_idx = _header_index(headers, "device")
            if keyword_idx < 0:
                continue
            for row in rows:
                keyword = (row[keyword_idx] if keyword_idx < len(row) else "").strip()
                if not keyword:
                    continue
                page = self._to_int(row[page_idx] if page_idx >= 0 and page_idx < len(row) else "")
                rank = self._to_int(row[rank_idx] if rank_idx >= 0 and rank_idx < len(row) else "")
                total_rank = self._to_int(
                    row[overall_idx] if overall_idx >= 0 and overall_idx < len(row) else ""
                )
                if not total_rank and page:
                    total_rank = ((page - 1) * 10) + (rank or 1)
                if not page and total_rank:
                    page = max(1, ((total_rank - 1) // 10) + 1)
                if not page:
                    continue
                timestamp = ""
                if dt_idx >= 0 and dt_idx < len(row):
                    timestamp = (row[dt_idx] or "").strip()[:19]
                device = (
                    row[device_idx] if device_idx >= 0 and device_idx < len(row) else ""
                ).strip()
                self._upsert_record(
                    records,
                    keyword=keyword,
                    timestamp=timestamp,
                    page=page,
                    rank=rank or 1,
                    total_rank=total_rank or 1,
                    device=device,
                )
        return records

    @staticmethod
    def _upsert_record(
        records: dict[tuple[str, str], dict],
        *,
        keyword: str,
        timestamp: str,
        page: int,
        rank: int,
        total_rank: int,
        device: str = "",
    ) -> None:
        cleaned = keyword.strip()
        if not cleaned:
            return
        stamp = (timestamp or "").strip()
        if not stamp:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        bucket = KeywordHistoryLogger._device_bucket(device)
        incoming = {
            "timestamp": stamp,
            "keyword": cleaned,
            "page": int(page),
            "rank": int(rank),
            "total_rank": int(total_rank),
            "device": (device or "").strip(),
        }
        key = (cleaned, bucket)
        existing = records.get(key)
        if not existing or stamp >= (existing.get("timestamp") or ""):
            records[key] = incoming

    def load_last_page_hints(
        self,
        *,
        mobile: bool = False,
        max_pages: int = 10,
    ) -> dict[str, int]:
        hints: dict[str, int] = {}
        best_stamp: dict[str, str] = {}
        with self._lock:
            records = self._read_records_unlocked()
        for (_keyword, _bucket), row in records.items():
            keyword = (row.get("keyword") or "").strip()
            if not keyword:
                continue
            if not self._row_matches_device_pool(row, mobile=mobile, max_pages=max_pages):
                continue
            page = self._page_hint_from_row(row)
            if page is None:
                continue
            stamp = row.get("timestamp") or ""
            if keyword not in hints or stamp >= best_stamp.get(keyword, ""):
                hints[keyword] = page
                best_stamp[keyword] = stamp
        return hints

    def get_last_page_hint(
        self,
        keyword: str,
        *,
        mobile: bool = False,
        max_pages: int = 10,
    ) -> Optional[int]:
        lookup = keyword.strip()
        if not lookup:
            return None
        return self.load_last_page_hints(mobile=mobile, max_pages=max_pages).get(lookup)

    def filter_for_device(
        self,
        keywords: list[str],
        *,
        mobile: bool,
        max_pages: int = 10,
    ) -> list[str]:
        """Mobile: history pages 1-2 only. Desktop: page 3+ or no history."""
        hints = self.load_last_page_hints(mobile=mobile, max_pages=max_pages)
        eligible: list[str] = []
        for keyword in keywords:
            cleaned = (keyword or "").strip()
            if not cleaned:
                continue
            page = hints.get(cleaned)
            if mobile:
                if page is not None and 1 <= page <= 2:
                    eligible.append(cleaned)
            elif page is None or page >= 3:
                eligible.append(cleaned)
        return eligible

    @staticmethod
    def _to_int(value: str) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _domain_key(target_domain: str) -> str:
        cleaned = (target_domain or "").strip().lower()
        if cleaned.startswith("http://"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("https://"):
            cleaned = cleaned[8:]
        cleaned = cleaned.split("/")[0].strip()
        cleaned = cleaned.removeprefix("www.")
        cleaned = re.sub(r"[^a-z0-9._-]+", "_", cleaned)
        return cleaned or "site"
