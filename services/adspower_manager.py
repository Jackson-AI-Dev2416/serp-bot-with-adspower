import re
import time
import random
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import requests


@dataclass
class ProfileSpec:
  profile_id: str
  name: str
  proxy_host: str
  proxy_port: int
  proxy_user: str
  proxy_pass: str
  os_type: str
  profile_no: str = ""
  is_active: bool = False

  @property
  def os_browser_label(self) -> str:
    os_name = self.os_type.lower()
    if os_name.startswith("android"):
      return "And/Chrome"
    if os_name.startswith("ios"):
      return "iOS/Chrome"
    if os_name.startswith("mac"):
      return "Mac/Chrome"
    if os_name.startswith("linux"):
      return "Lin/Chrome"
    if os_name.startswith("windows"):
      return "Win/Chrome"
    return "Unknown"

  @property
  def device_label(self) -> str:
    os_type = (self.os_type or "Unknown").strip()
    if not os_type:
      return "Unknown"
    lowered = os_type.lower()
    if lowered == "macos":
      return "macOS"
    if lowered == "ios":
      return "iOS"
    return os_type[:1].upper() + os_type[1:]

  @property
  def is_mobile(self) -> bool:
    return (self.os_type or "").lower().startswith(("android", "ios"))


DEFAULT_PROXY_TYPE = "http"
PROFILE_OS_POOL = ("Windows", "Android")
PROFILE_BROWSER = "chrome"


class AdsPowerManager:
  CREATE_PATH = "/api/v2/browser-profile/create"
  START_PATH = "/api/v2/browser-profile/start"
  STOP_PATH = "/api/v2/browser-profile/stop"
  DELETE_PATH = "/api/v2/browser-profile/delete"
  LIST_PATH = "/api/v2/browser-profile/list"
  UA_PATH = "/api/v2/browser-profile/ua"
  USER_LIST_PATH = "/api/v1/user/list"
  UA_BATCH_LIMIT = 10
  LOCAL_ACTIVE_PATH = "/api/v1/browser/local-active"
  ACTIVE_PATH = "/api/v2/browser-profile/active"
  PROFILE_NAME_COUNTER_PATH = Path("data/profile_name_counter.json")
  _name_counter_lock = threading.Lock()
  _api_lock = threading.Lock()
  _last_api_at = 0.0
  _MIN_API_INTERVAL_SEC = 1.15

  def __init__(self, base_url: str, api_key: str = "", logger: Optional[Callable[[str], None]] = None):
    self.base_url = base_url.rstrip("/")
    self.logger = logger or (lambda _msg: None)
    self.session = requests.Session()
    self.session.trust_env = False
    if api_key:
      self.session.headers["Authorization"] = f"Bearer {api_key}"

  @classmethod
  def _throttle_api(cls) -> None:
    with cls._api_lock:
      now = time.time()
      wait = cls._MIN_API_INTERVAL_SEC - (now - cls._last_api_at)
      if wait > 0:
        time.sleep(wait)
      cls._last_api_at = time.time()

  @staticmethod
  def _is_rate_limit_error(message: str) -> bool:
    text = (message or "").lower()
    return "too many request" in text or "rate limit" in text

  def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
    url = f"{self.base_url}{path}"
    last_error = "unknown AdsPower API error"
    for attempt in range(1, 8):
      self._throttle_api()
      try:
        if method.upper() == "GET":
          response = self.session.get(url, params=payload, timeout=90)
        else:
          response = self.session.post(url, json=payload or {}, timeout=90)
        response.raise_for_status()
      except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
          f"Cannot connect to AdsPower Local API ({self.base_url}). "
          f"Ensure AdsPower is running and the port is correct. ({exc})"
        ) from exc

      body = response.json()
      if body.get("code") != 0:
        msg = str(body.get("msg") or body)
        last_error = msg
        if "api-key" in msg.lower() or "api key" in msg.lower():
          raise RuntimeError(
            "AdsPower API Key required. Copy it from AdsPower → Settings → Local API "
            "and paste it into Configuration → AdsPower API Key."
          )
        if self._is_rate_limit_error(msg) and attempt < 7:
          time.sleep(min(12.0, 1.5 * attempt))
          continue
        raise RuntimeError(msg)
      return body.get("data") or {}
    raise RuntimeError(last_error)

  def check_connection(self) -> tuple[bool, str]:
    """Returns (ok, message) for GUI diagnostics."""
    try:
      url = f"{self.base_url}/status"
      response = self.session.get(url, timeout=10)
      response.raise_for_status()
      body = response.json()
      if body.get("code") != 0:
        return False, str(body.get("msg") or body)
      if not self.session.headers.get("Authorization"):
        return True, "AdsPower connected (API Key missing — required for profile list/create)"
      return True, "AdsPower connected with API Key"
    except Exception as exc:
      return False, str(exc)

  def _post(self, path: str, payload: dict) -> dict:
    return self._request("POST", path, payload)

  def _get(self, path: str, params: Optional[dict] = None) -> dict:
    return self._request("GET", path, params)

  def _ensure_api_key(self) -> None:
    if not self.session.headers.get("Authorization"):
      raise RuntimeError(
        "AdsPower API Key required. Copy it from AdsPower → Settings → Local API "
        "and paste it into Configuration → AdsPower API Key."
      )

  def get_active_profile_ids(self) -> set[str]:
    if not self.session.headers.get("Authorization"):
      return set()

    try:
      data = self._get(self.LOCAL_ACTIVE_PATH)
    except Exception as exc:
      self.logger(f"[AdsPower] Active browser check skipped: {exc}")
      return set()

    active_ids: set[str] = set()
    for item in data.get("list") or []:
      profile_id = item.get("user_id") or item.get("profile_id")
      if profile_id:
        active_ids.add(str(profile_id))
    return active_ids

  def list_profiles(self, group_id: str = "", page_limit: int = 100) -> List[ProfileSpec]:
    """Fetch live profile state from AdsPower via GET /api/v1/user/list (no cache)."""
    return self.list_profiles_live(group_id=group_id, page_size=page_limit)

  def list_profiles_live(self, group_id: str = "", page_size: int = 100) -> List[ProfileSpec]:
    self._ensure_api_key()
    active_ids = self.get_active_profile_ids()
    specs: List[ProfileSpec] = []
    page = 1

    while True:
      params: dict = {"page": page, "page_size": page_size}
      if group_id:
        params["group_id"] = group_id

      data = self._get(self.USER_LIST_PATH, params)
      batch = data.get("list") or []
      if not batch:
        break

      for item in batch:
        spec = self._parse_v1_profile_item(item, active_ids)
        if spec:
          specs.append(spec)

      if len(batch) < page_size:
        break
      page += 1
      time.sleep(0.35)

    ua_map: dict[str, str] = {}
    if specs:
      time.sleep(1.1)
      ua_map = self._fetch_profile_uas([spec.profile_id for spec in specs])
    resolved = 0
    for spec in specs:
      ua = ua_map.get(spec.profile_id, "")
      if not ua:
        continue
      detected = self._os_type_from_ua(ua)
      if detected == "Unknown":
        continue
      # Preserve OS already inferred from profile metadata/name.
      # AdsPower UA endpoint can return generic desktop UA for some mobile profiles.
      if spec.os_type in ("", "Unknown"):
        spec.os_type = detected
        resolved += 1

    self.logger(
      f"[AdsPower] Live sync: {len(specs)} profiles from /api/v1/user/list "
      f"({resolved} OS resolved via /api/v2/browser-profile/ua)"
    )
    return specs

  def _fetch_profile_uas(self, profile_ids: List[str]) -> dict[str, str]:
    if not profile_ids:
      return {}

    ua_map: dict[str, str] = {}
    for offset in range(0, len(profile_ids), self.UA_BATCH_LIMIT):
      batch = profile_ids[offset : offset + self.UA_BATCH_LIMIT]
      data = self._post_profile_uas(batch)
      for item in data.get("list") or []:
        profile_id = str(item.get("profile_id") or "")
        ua = str(item.get("ua") or "").strip()
        if profile_id and ua:
          ua_map[profile_id] = ua

      if offset + self.UA_BATCH_LIMIT < len(profile_ids):
        time.sleep(1.1)

    return ua_map

  def _post_profile_uas(self, profile_ids: List[str]) -> dict:
    last_error: Optional[Exception] = None
    for attempt in range(3):
      if attempt:
        time.sleep(1.1 * attempt)
      try:
        return self._post(self.UA_PATH, {"profile_id": profile_ids})
      except RuntimeError as exc:
        last_error = exc
        if "too many request" not in str(exc).lower():
          raise
    if last_error:
      self.logger(f"[AdsPower] UA lookup skipped for {len(profile_ids)} profile(s): {last_error}")
    return {}

  def _parse_v1_profile_item(self, item: dict, active_ids: set[str]) -> Optional[ProfileSpec]:
    return self._parse_profile_item(item, active_ids)

  def _parse_profile_item(self, item: dict, active_ids: set[str]) -> Optional[ProfileSpec]:
    profile_id = str(item.get("profile_id") or item.get("user_id") or "")
    if not profile_id:
      return None

    name = str(item.get("name") or profile_id)
    proxy_cfg = item.get("user_proxy_config") or {}
    proxy_host = str(proxy_cfg.get("proxy_host") or item.get("ip") or "—")
    proxy_port_raw = proxy_cfg.get("proxy_port") or 0
    try:
      proxy_port = int(proxy_port_raw)
    except (TypeError, ValueError):
      proxy_port = 0
    proxy_user = str(proxy_cfg.get("proxy_user") or "")
    proxy_pass = str(proxy_cfg.get("proxy_password") or "")

    os_type = self._detect_os_type(item, name)
    profile_no = str(item.get("profile_no") or item.get("serial_number") or "").strip()
    return ProfileSpec(
      profile_id=profile_id,
      name=name,
      proxy_host=proxy_host,
      proxy_port=proxy_port,
      proxy_user=proxy_user,
      proxy_pass=proxy_pass,
      os_type=os_type,
      profile_no=profile_no,
      is_active=self._is_profile_active(item, profile_id, active_ids),
    )

  @staticmethod
  def _is_profile_active(item: dict, profile_id: str, active_ids: set[str]) -> bool:
    if profile_id in active_ids:
      return True
    for key in ("status", "run_status", "browser_status", "last_open_status"):
      value = item.get(key)
      if value is None:
        continue
      normalized = str(value).strip().lower()
      if normalized in ("1", "true", "running", "active", "open", "opened", "online"):
        return True
      if normalized in ("0", "false", "stopped", "closed", "offline"):
        return False
    return False

  @staticmethod
  def _os_type_from_ua(ua: str) -> str:
    ua_lower = ua.lower()
    if any(token in ua_lower for token in ("iphone", "ipad", "ipod", "cpu ios", "cpu iphone os")):
      return "iOS"
    if "android" in ua_lower:
      return "Android"
    if "windows" in ua_lower:
      return "Windows"
    if "macintosh" in ua_lower or "mac os x" in ua_lower:
      return "macOS"
    if "linux" in ua_lower:
      return "Linux"
    return "Unknown"

  @staticmethod
  def _detect_os_type(item: dict, name: str) -> str:
    direct_candidates = (
      item.get("os"),
      item.get("os_type"),
      item.get("sys"),
      item.get("platform"),
      item.get("device_os"),
    )
    for candidate in direct_candidates:
      value = str(candidate or "").strip().lower()
      if not value:
        continue
      if "android" in value:
        return "Android"
      if "ios" in value or "iphone" in value or "ipad" in value:
        return "iOS"
      if "windows" in value or value == "win":
        return "Windows"
      if "mac" in value or "darwin" in value:
        return "macOS"
      if "linux" in value:
        return "Linux"

    fingerprint = item.get("fingerprint_config") or {}
    random_ua = fingerprint.get("random_ua") or {}
    systems = (
      random_ua.get("ua_system_version")
      or random_ua.get("ua_system")
      or random_ua.get("ua_version")
      or []
    )
    for system in systems:
      system_lower = str(system).lower()
      if "android" in system_lower:
        return "Android"
      if "ios" in system_lower or "iphone" in system_lower or "ipad" in system_lower:
        return "iOS"
      if "windows" in system_lower:
        return "Windows"
      if "mac" in system_lower:
        return "macOS"
      if "linux" in system_lower:
        return "Linux"

    ua = str(fingerprint.get("ua") or item.get("ua") or "")
    if ua:
      detected = AdsPowerManager._os_type_from_ua(ua)
      if detected != "Unknown":
        return detected

    remark = str(item.get("remark") or "")
    for source in (name, remark):
      source_lower = source.lower()
      if re.search(r"serp_?android|\bandroid\b", source, re.IGNORECASE):
        return "Android"
      if re.search(r"serp_?ios|\bios\b|iphone|ipad", source, re.IGNORECASE):
        return "iOS"
      if re.search(r"serp_?windows|\bwindows\b", source, re.IGNORECASE):
        return "Windows"
      if re.search(r"serp_?mac|\bmac\b|macos", source, re.IGNORECASE):
        return "macOS"
      if "linux" in source_lower:
        return "Linux"

    return "Unknown"

  def verify_profile_ids(self, profile_ids: List[str]) -> List[str]:
    """Return profile IDs that exist in AdsPower (v2 list API)."""
    if not profile_ids:
      return []
    self._ensure_api_key()
    data = self._post(
      self.LIST_PATH,
      {"profile_id": profile_ids, "page": 1, "limit": max(len(profile_ids), 1)},
    )
    found: List[str] = []
    for item in data.get("list") or []:
      profile_id = str(item.get("profile_id") or "")
      if profile_id:
        found.append(profile_id)
    return found

  def create_profiles_batch(
    self,
    proxies: List[Tuple[str, int, str, str]],
    group_id: str = "0",
    total: int = 20,
  ) -> List[ProfileSpec]:
    self._ensure_api_key()
    if total < 1:
      raise ValueError("Target profile count must be at least 1.")

    use_no_proxy = not proxies
    if not use_no_proxy:
      proxy_assignments = self._assign_unique_proxies(proxies, total)
    else:
      proxy_assignments = []

    if use_no_proxy:
      self.logger("[AdsPower] No proxies configured — creating profiles with no_proxy (test mode).")

    os_plan = self._build_os_plan(total)
    os_mix = {os_name: os_plan.count(os_name) for os_name in PROFILE_OS_POOL}
    self.logger(
      "[AdsPower] OS mix for batch: "
      + ", ".join(f"{name}={count}" for name, count in os_mix.items() if count)
      + f" (browser={PROFILE_BROWSER})"
    )

    specs: List[ProfileSpec] = []
    for assignment_index, os_type in enumerate(os_plan):
      name = self._next_profile_name()
      if use_no_proxy:
        host, port, user, password = "—", 0, "", ""
        user_proxy_config = {"proxy_soft": "no_proxy"}
      else:
        host, port, user, password = proxy_assignments[assignment_index]
        user_proxy_config = self._build_http_proxy_config(host, port, user, password)
        self.logger(f"[AdsPower] {name} <- {DEFAULT_PROXY_TYPE.upper()} {host}:{port}")

      payload = {
        "name": name,
        "group_id": str(group_id),
        "tabs": ["about:blank"],
        "user_proxy_config": user_proxy_config,
        "fingerprint_config": self._build_fingerprint(os_type),
      }
      data = self._post(self.CREATE_PATH, payload)
      profile_id = str(data.get("profile_id") or data.get("id") or "")
      profile_no = str(data.get("profile_no") or "")
      if not profile_id:
        raise RuntimeError(f"AdsPower did not return profile_id for {name} (response={data})")

      specs.append(
        ProfileSpec(
          profile_id=profile_id,
          name=name,
          proxy_host=host,
          proxy_port=port,
          proxy_user=user,
          proxy_pass=password,
          os_type=os_type,
          profile_no=profile_no,
        )
      )
      suffix = f" (no={profile_no})" if profile_no else ""
      self.logger(f"[AdsPower] Created profile {name} [{os_type}/{PROFILE_BROWSER}] -> {profile_id}{suffix}")
      time.sleep(1.1)

    created_ids = [spec.profile_id for spec in specs]
    verified = self.verify_profile_ids(created_ids)
    missing = [pid for pid in created_ids if pid not in verified]
    if missing:
      raise RuntimeError(
        f"Profiles were not persisted in AdsPower. Missing IDs: {', '.join(missing)}"
      )
    self.logger(f"[AdsPower] Verified {len(verified)} profile(s) in AdsPower.")

    return specs

  def _next_profile_name(self) -> str:
    with self._name_counter_lock:
      current = 0
      if self.PROFILE_NAME_COUNTER_PATH.exists():
        try:
          parsed = json.loads(self.PROFILE_NAME_COUNTER_PATH.read_text(encoding="utf-8"))
          current = int(parsed.get("last_index", 0) or 0)
        except Exception:
          current = 0
      current += 1
      self.PROFILE_NAME_COUNTER_PATH.parent.mkdir(parents=True, exist_ok=True)
      self.PROFILE_NAME_COUNTER_PATH.write_text(
        json.dumps({"last_index": current}, ensure_ascii=True, indent=2),
        encoding="utf-8",
      )
    return f"s-{current:03d}"

  def reset_profile_name_counter(self, start_index: int = 0) -> None:
    with self._name_counter_lock:
      value = max(0, int(start_index))
      self.PROFILE_NAME_COUNTER_PATH.parent.mkdir(parents=True, exist_ok=True)
      self.PROFILE_NAME_COUNTER_PATH.write_text(
        json.dumps({"last_index": value}, ensure_ascii=True, indent=2),
        encoding="utf-8",
      )
    self.logger(f"[AdsPower] Profile name counter reset to {value}")

  @staticmethod
  def _build_http_proxy_config(host: str, port: int, user: str, password: str) -> dict:
    return {
      "proxy_soft": "other",
      "proxy_type": DEFAULT_PROXY_TYPE,
      "proxy_host": host,
      "proxy_port": str(port),
      "proxy_user": user,
      "proxy_password": password,
    }

  @staticmethod
  def _assign_unique_proxies(
    proxies: List[Tuple[str, int, str, str]],
    total: int,
  ) -> List[Tuple[str, int, str, str]]:
    """Assign one unique proxy per profile — no proxy is reused in the same batch."""
    if total > len(proxies):
      raise ValueError(
        f"Need at least {total} proxies to create {total} profiles "
        f"({len(proxies)} configured). Each proxy is assigned once per batch."
      )
    return list(proxies[:total])

  @staticmethod
  def _build_os_plan(total: int) -> List[str]:
    pool = list(PROFILE_OS_POOL)
    if total <= 0:
      return []
    if total == 1:
      return [random.choice(pool)]
    if total < len(pool):
      chosen = random.sample(pool, total)
      random.shuffle(chosen)
      return chosen
    plan = list(pool)
    plan.extend(random.choice(pool) for _ in range(total - len(pool)))
    random.shuffle(plan)
    return plan

  @staticmethod
  def _build_fingerprint(os_type: str) -> dict:
    fingerprint = {
      "automatic_timezone": "1",
      "language": ["en-US", "en"],
      "flash": "block",
      "fonts": ["all"],
      "webrtc": "disabled",
      "browser_kernel_config": {"type": PROFILE_BROWSER, "version": "ua_auto"},
    }
    chrome_ua = {"ua_browser": [PROFILE_BROWSER]}
    if os_type == "Android":
      fingerprint["screen_resolution"] = "random"
      fingerprint["random_ua"] = {
        **chrome_ua,
        "ua_system_version": ["Android 11", "Android 12", "Android 13"],
      }
    else:
      fingerprint["screen_resolution"] = "random"
      fingerprint["random_ua"] = {
        **chrome_ua,
        "ua_system_version": ["Windows 10", "Windows 11"],
      }
    return fingerprint

  @staticmethod
  def _build_browser_start_payload(profile_id: str) -> dict:
    return {
      "profile_id": profile_id,
      "headless": "0",
      "proxy_detection": "0",
      "last_opened_tabs": "0",
    }

  def start_profile(self, profile_id: str) -> str:
    payload = self._build_browser_start_payload(profile_id)
    self.logger(
      "[AdsPower] Start options: proxy_detection=0, last_opened_tabs=0"
    )
    data = self._post(self.START_PATH, payload)
    ws_data = data.get("ws") or {}
    ws_endpoint = ws_data.get("puppeteer")
    if not ws_endpoint:
      raise RuntimeError(f"No puppeteer ws endpoint returned for {profile_id}")
    self.logger(f"[AdsPower] Started {profile_id}")
    time.sleep(1.2)
    return ws_endpoint

  def stop_profile(self, profile_id: str) -> None:
    try:
      self._post(self.STOP_PATH, {"profile_id": profile_id})
      self.logger(f"[AdsPower] Stopped {profile_id}")
    except Exception as exc:
      self.logger(f"[AdsPower] Stop warning for {profile_id}: {exc}")

  def force_terminate_profile(self, profile_id: str) -> None:
    self.stop_profile(profile_id)
    time.sleep(1.5)

  def delete_profiles(self, profile_ids: List[str]) -> None:
    if not profile_ids:
      return
    self._ensure_api_key()
    for offset in range(0, len(profile_ids), 100):
      batch = profile_ids[offset : offset + 100]
      self._post(self.DELETE_PATH, {"profile_id": batch})
      self.logger(f"[AdsPower] Deleted {len(batch)} profile(s) from AdsPower")
      if offset + 100 < len(profile_ids):
        time.sleep(1.1)
