"""
Playwright network route interception for residential proxy traffic minimization.

Preserves Google search, cookies, JS, GA4/GTM, Wix runtime bundles, and
target-site analytics while blocking heavy assets, ads, and non-essential
third-party trackers.
"""

from __future__ import annotations

import json
import time
from enum import Enum
from typing import Callable, Optional
from urllib.parse import urlparse

from playwright.sync_api import Page, Request, Route

from utils.app_paths import data_dir

# Resource priority (highest first): document > script > xhr/fetch > stylesheet > image
_PRIORITY_ORDER = ("document", "script", "xhr", "fetch", "stylesheet", "image")


_IP_CHECK_HOSTS = frozenset({
  "api.ipify.org",
  "checkip.amazonaws.com",
  "ipv4.icanhazip.com",
  "icanhazip.com",
})


class PagePhase(str, Enum):
  GOOGLE_SERP = "google_serp"
  TARGET_SITE = "target_site"
  DEFAULT = "default"


class TrafficMonitor:
  """Tracks allowed vs blocked traffic for a browser session."""

  _ESTIMATED_BLOCKED_BYTES = {
    "document": 32_768,
    "script": 48_000,
    "stylesheet": 24_000,
    "image": 80_000,
    "media": 512_000,
    "font": 40_000,
    "xhr": 4_096,
    "fetch": 4_096,
    "other": 16_000,
  }

  def __init__(self) -> None:
    self.request_count = 0
    self.blocked_count = 0
    self.allowed_bytes = 0
    self.blocked_bytes = 0
    self.target_allowed_bytes = 0
    self.other_allowed_bytes = 0
    self._baseline_allowed = 0

  def set_baseline(self) -> None:
    """Snapshot current allowed bytes (for before/after comparison)."""
    self._baseline_allowed = self.allowed_bytes

  def record_allowed(self, size_bytes: int, *, is_target: bool = False) -> None:
    if size_bytes > 0:
      self.allowed_bytes += size_bytes
      if is_target:
        self.target_allowed_bytes += size_bytes
      else:
        self.other_allowed_bytes += size_bytes

  def record_blocked(self, request: Request) -> None:
    self.request_count += 1
    self.blocked_count += 1
    self.blocked_bytes += self._estimate_blocked_size(request)

  @classmethod
  def _estimate_blocked_size(cls, request: Request) -> int:
    try:
      headers = request.headers or {}
      raw = headers.get("content-length") or headers.get("Content-Length")
      if raw:
        return max(0, int(str(raw).strip()))
    except Exception:
      pass
    resource_type = (request.resource_type or "other").lower()
    return cls._ESTIMATED_BLOCKED_BYTES.get(resource_type, cls._ESTIMATED_BLOCKED_BYTES["other"])

  @property
  def total_bytes(self) -> int:
    return self.allowed_bytes + self.blocked_bytes

  @property
  def savings_percent(self) -> float:
    total = self.total_bytes
    if total <= 0:
      return 0.0
    return round((self.blocked_bytes / total) * 100.0, 1)

  def format_log(
    self,
    profile_name: str,
    keyword: str = "",
    *,
    include_baseline: bool = False,
    wire_meter: object | None = None,
  ) -> str:
    total = self._human_bytes(self.total_bytes)
    blocked = self._human_bytes(self.blocked_bytes)
    allowed = self._human_bytes(self.allowed_bytes)
    saved_pct = self.savings_percent
    lines = [
      "[TRAFFIC]",
      f"  profile: {profile_name}",
      f"  keyword: {keyword or '(session)'}",
      f"  intercepted: {self.request_count} (blocked={self.blocked_count})",
      f"  total: {total}",
      f"  allowed: {allowed}",
      f"  blocked: {blocked}",
      f"  saved: {saved_pct}%",
    ]
    if wire_meter is not None:
      wire_down = int(getattr(wire_meter, "wire_download_bytes", 0) or 0)
      wire_up = int(getattr(wire_meter, "wire_upload_bytes", 0) or 0)
      wire_total = wire_down + wire_up
      lines.append(f"  proxy_wire: {self._human_bytes(wire_total)} (down={self._human_bytes(wire_down)}, up={self._human_bytes(wire_up)})")
    if include_baseline and self._baseline_allowed > 0:
      before = self._human_bytes(self._baseline_allowed + self.blocked_bytes)
      after = self._human_bytes(self.allowed_bytes)
      lines.append(f"  before(est): {before}")
      lines.append(f"  after: {after}")
    return "\n".join(lines)

  @staticmethod
  def _human_bytes(value: int) -> str:
    size = max(0, int(value))
    if size < 1024:
      return f"{size}B"
    kb = size / 1024.0
    if kb < 1024.0:
      return f"{kb:.1f}KB"
    mb = size / (1024.0 * 1024.0)
    return f"{mb:.1f}MB"

  def append_session_log(
    self,
    profile_name: str,
    keyword: str = "",
    *,
    wire_meter: object | None = None,
  ) -> None:
    try:
      traffic_log_path = data_dir() / "traffic_sessions.jsonl"
      traffic_log_path.parent.mkdir(parents=True, exist_ok=True)
      wire_down = int(getattr(wire_meter, "wire_download_bytes", 0) or 0) if wire_meter else 0
      wire_up = int(getattr(wire_meter, "wire_upload_bytes", 0) or 0) if wire_meter else 0
      payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "profile": profile_name,
        "keyword": keyword,
        "request_count": self.request_count,
        "blocked_count": self.blocked_count,
        "allowed_bytes": self.allowed_bytes,
        "blocked_bytes": self.blocked_bytes,
        "wire_download_bytes": wire_down,
        "wire_upload_bytes": wire_up,
        "wire_total_bytes": wire_down + wire_up,
        "saved_percent": self.savings_percent,
      }
      with traffic_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
      pass


class NetworkOptimizer:
  """Manages Playwright route interception with GA4 protection and phase rules."""

  _IMAGE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
  )
  _VIDEO_EXTENSIONS = (
    ".mp4", ".webm", ".mov", ".m3u8", ".avi", ".mkv", ".m4v", ".3gp",
  )
  _FONT_EXTENSIONS = (
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
  )
  _AD_HOSTS = (
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "adservice.google.com",
    "pagead2.googlesyndication.com",
  )
  _BLOCKED_TRACKER_HOSTS = (
    "facebook.com",
    "facebook.net",
    "connect.facebook.net",
    "naver.com",
    "naver.net",
    "wcs.naver.net",
    "nelo.naver.com",
    "nlog.naver.com",
    "daum.net",
    "daumcdn.net",
    "kakao.com",
    "kakaocdn.net",
    "daumkakao.com",
    "kakao.co.kr",
    "tiktok.com",
    "tiktokcdn.com",
  )
  _GA_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "analytics.google.com",
    "tagmanager.google.com",
  )
  _GA_URL_TOKENS = (
    "gtag/js",
    "gtag/destination",
    "collect?",
    "/g/collect",
    "/j/collect",
    "/mp/collect",
    "/r/collect",
    "/ccm/collect",
    "google-analytics.com/analytics.js",
    "googletagmanager.com/gtm.js",
    "googletagmanager.com/gtag/",
    "measurement protocol",
  )
  _WIX_RUNTIME_HOSTS = (
    "parastorage.com",
  )
  _CAPTCHA_TOKENS = (
    "recaptcha",
    "/sorry/",
    "captcha",
    "challenges.cloudflare.com",
    "turnstile",
    "enterprise.js",
    "captchaimg",
  )
  _FONT_URL_TOKENS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
  )
  _HEAVY_EMBED_URL_TOKENS = (
    "youtube.com/embed",
    "youtube-nocookie.com/embed",
    "player.vimeo.com",
    "googlevideo.com/videoplayback",
    "dailymotion.com/embed",
  )
  # Phase-independent: block YouTube preview CDN before SERP phase is set.
  _YOUTUBE_THUMBNAIL_HOSTS = frozenset({
    "ytimg.com",
    "i.ytimg.com",
    "img.youtube.com",
  })
  _SERP_LAYOUT_CRITICAL_HOSTS = frozenset({
    "mt0.google.com",
    "mt1.google.com",
    "mt2.google.com",
    "mt3.google.com",
    "khms0.google.com",
    "khms1.google.com",
    "khms2.google.com",
    "khms3.google.com",
    "khms0.googleapis.com",
    "khms1.googleapis.com",
    "khms2.googleapis.com",
    "khms3.googleapis.com",
  })
  _SERP_LAYOUT_CRITICAL_URL_TOKENS = (
    "/async/lcl_",
  )
  # Heavy background media only — must not include SERP layout / local-pack infrastructure.
  _SERP_HEAVY_MEDIA_HOSTS = frozenset({
    "ytimg.com",
    "i.ytimg.com",
    "img.youtube.com",
    "googlevideo.com",
    "youtube.com",
    "youtube-nocookie.com",
    "vimeo.com",
    "dailymotion.com",
    "gvt1.com",
    "redirector.googlevideo.com",
    "streetviewpixels-pa.googleapis.com",
    "ggpht.com",
  })
  _SERP_BANDWIDTH_URL_TOKENS = (
    "encrypted-tbn",
    "encrypted-tb0",
    "encrypted-tb1",
    "encrypted-tb2",
    "encrypted-tb3",
    "videoplayback",
    "googlevideo.com",
    "ytimg.com",
    "ggpht.com",
    "lh3.googleusercontent",
    "lh4.googleusercontent",
    "lh5.googleusercontent",
    "lh6.googleusercontent",
    "/images/thumbnail",
    "thumbnail?id=",
    "imgres?",
    "/imgres",
    "tbm=isch",
    "tbm=vid",
    "udm=7",
    "udm=39",
    "imgurl=",
    "/vi/",
    "/maps/embed",
    "/maps/preview",
    "youtube.com/embed",
    "youtube-nocookie.com/embed",
    "googlevideo.com/videoplayback",
  )
  _SERP_THIRD_PARTY_EMBED_URL_TOKENS = (
    "/maps/embed",
    "/maps/preview",
    "youtube.com/embed",
    "youtube-nocookie.com/embed",
    "player.vimeo.com",
    "googlevideo.com/videoplayback",
    "dailymotion.com/embed",
  )
  _GOOGLE_SERP_VISUAL_HINTS = (
    "encrypted-tbn",
    "/tbn/",
    "googleusercontent.com",
    "ggpht.com",
    "/vi/",
  )
  _GOOGLEUSERCONTENT_SMALL_ALLOW = (
    "=s16", "=s32", "=s48",
    "/s16", "/s32", "/s48",
    "-s16-", "-s32-", "-s48-",
  )
  _SERP_MINIMAL_CHROME_URL_TOKENS = (
    "/gen_204",
    "googlelogo",
    "/images/searchbox",
    "/images/branding/hdpi",
    "/images/branding/googleg",
    "productlogos",
  )
  _TARGET_BRANDING_URL_TOKENS = (
    "favicon",
    "/logo",
    "/logos/",
    "site-icon",
    "apple-touch-icon",
  )
  _TARGET_STORM_WINDOW_SEC = 10.0
  _TARGET_STORM_HOST_THRESHOLD = 450
  _TARGET_STORM_URL_THRESHOLD = 40
  _TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
  )

  def __init__(
    self,
    target_host: str,
    log: Callable[[str], None],
    *,
    profile_name: str = "",
    target_hosts: list[str] | None = None,
    blocking_enabled: bool = True,
  ) -> None:
    hosts: list[str] = []
    for raw in (target_hosts or []):
      normalized = (raw or "").lower().removeprefix("www.")
      if normalized and normalized not in hosts:
        hosts.append(normalized)
    if not hosts and (target_host or "").strip():
      normalized = (target_host or "").lower().removeprefix("www.")
      if normalized:
        hosts.append(normalized)
    self.target_hosts = hosts
    self.target_host = hosts[0] if hosts else (target_host or "").lower().removeprefix("www.")
    self.log = log
    self.profile_name = profile_name
    self.blocking_enabled = bool(blocking_enabled)
    self.phase = PagePhase.DEFAULT
    self.monitor = TrafficMonitor()
    self._current_keyword = ""
    self._route_attached = False
    self._mobile_routed_pages: set[int] = set()
    self._blocked_by_type: dict[str, int] = {}
    self._blocked_script_log_count = 0
    self._blocked_script_log_limit = 8
    self._storm_hosts: set[str] = set()
    self._host_block_times: dict[str, list[float]] = {}
    self._url_block_times: dict[str, list[float]] = {}
    self._storm_counted_urls: set[str] = set()
    self._storm_logged_hosts: set[str] = set()

  def set_phase(self, phase: PagePhase) -> None:
    if phase != self.phase:
      self.log(f"[Network] Phase → {phase.value}")
      if phase != PagePhase.TARGET_SITE:
        self._reset_target_storm_state()
    self.phase = phase

  def _reset_target_storm_state(self) -> None:
    self._storm_hosts.clear()
    self._host_block_times.clear()
    self._url_block_times.clear()
    self._storm_counted_urls.clear()
    self._storm_logged_hosts.clear()

  def is_target_storm_active(self, host: str = "") -> bool:
    if not self._storm_hosts:
      return False
    normalized = (host or "").lower().removeprefix("www.")
    if normalized:
      return any(
        normalized == storm or normalized.endswith(f".{storm}")
        for storm in self._storm_hosts
      )
    return self.phase == PagePhase.TARGET_SITE

  @classmethod
  def _prune_block_times(cls, times: list[float], now: float) -> list[float]:
    cutoff = now - cls._TARGET_STORM_WINDOW_SEC
    return [stamp for stamp in times if stamp >= cutoff]

  def _track_target_block(self, host: str, url: str) -> None:
    if self.phase != PagePhase.TARGET_SITE:
      return
    normalized = (host or "").lower().removeprefix("www.")
    if not normalized:
      return
    now = time.time()
    host_times = self._prune_block_times(self._host_block_times.get(normalized, []), now)
    host_times.append(now)
    self._host_block_times[normalized] = host_times

    url_key = (url or "").split("?", 1)[0].lower()
    if url_key:
      url_times = self._prune_block_times(self._url_block_times.get(url_key, []), now)
      url_times.append(now)
      self._url_block_times[url_key] = url_times
      if len(url_times) >= self._TARGET_STORM_URL_THRESHOLD:
        self._activate_target_storm(normalized, "repeated image URL")
        return

    if len(host_times) >= self._TARGET_STORM_HOST_THRESHOLD:
      self._activate_target_storm(normalized, "blocked-request burst")

  def _activate_target_storm(self, host: str, reason: str) -> None:
    if host in self._storm_hosts:
      return
    self._storm_hosts.add(host)
    if host not in self._storm_logged_hosts:
      self._storm_logged_hosts.add(host)
      self.log(
        f"[Network] Target storm mode on {host} ({reason}) — "
        "fulfilling repeated visual assets to prevent retry loops"
      )

  def _should_fulfill_instead_of_abort(
    self,
    request: Request,
    host: str,
    url: str,
    resource_type: str,
  ) -> bool:
    if self.phase != PagePhase.TARGET_SITE:
      return False
    if not self.is_target_storm_active(host):
      return False
    kind = (resource_type or "other").lower()
    if kind in ("image", "media", "font"):
      return True
    if kind in ("xhr", "fetch", "other", "ping") and self._serp_request_looks_like_visual_media(
      kind, (url or "").lower(),
    ):
      return True
    return self._has_extension(
      url or "",
      self._IMAGE_EXTENSIONS + self._VIDEO_EXTENSIONS + self._FONT_EXTENSIONS,
    )

  def _record_blocked_deduped(self, request: Request, url: str) -> None:
    url_key = (url or "").split("?", 1)[0].lower()
    if url_key and url_key in self._storm_counted_urls:
      return
    if url_key:
      self._storm_counted_urls.add(url_key)
    self.monitor.record_blocked(request)

  def _fulfill_tiny_image(self, route: Route) -> None:
    route.fulfill(
      status=200,
      body=self._TINY_GIF,
      headers={"content-type": "image/gif"},
    )

  def apply_phase_headers(self, page: Page) -> None:
    """Keep a normal browser header profile (no Save-Data hint on target site)."""
    try:
      page.context.set_extra_http_headers({})
    except Exception:
      pass

  def set_keyword(self, keyword: str) -> None:
    self._current_keyword = keyword or ""

  def attach(self, page: Page, *, mobile: bool) -> bool:
    """Attach route handler. Returns False if setup failed."""
    if page.is_closed():
      return False

    if mobile:
      page_key = id(page)
      if page_key in self._mobile_routed_pages:
        return True
    elif self._route_attached:
      return True

    def on_route(route: Route, request: Request) -> None:
      try:
        if self.should_abort(request):
          kind = request.resource_type or "other"
          url = request.url or ""
          host = urlparse(url).netloc.lower().removeprefix("www.")
          visual_block = (
            kind in ("image", "media", "font")
            or self._has_extension(
              url,
              self._IMAGE_EXTENSIONS + self._VIDEO_EXTENSIONS + self._FONT_EXTENSIONS,
            )
          )
          if visual_block:
            self._track_target_block(host, url)
          if self._should_fulfill_instead_of_abort(request, host, url, kind):
            self._blocked_by_type[kind] = self._blocked_by_type.get(kind, 0) + 1
            self._record_blocked_deduped(request, url)
            self._fulfill_tiny_image(route)
            return
          if kind == "script":
            self._log_blocked_script(url)
          self._blocked_by_type[kind] = self._blocked_by_type.get(kind, 0) + 1
          self.monitor.record_blocked(request)
          route.abort()
          return
        route.continue_()
      except Exception as exc:
        try:
          route.continue_()
        except Exception:
          try:
            route.abort()
          except Exception:
            pass
        self.log(f"[Network] Route handler warning: {exc}")

    try:
      if mobile:
        page.route("**/*", on_route)
        self._mobile_routed_pages.add(id(page))
      else:
        page.context.route("**/*", on_route)
        self._route_attached = True
      page.on("domcontentloaded", lambda: self._log_block_summary())
      self.log(
        "[Network] Two-track blocking ON "
        "(common: font/ws/ads/trackers/embeds/ytimg; "
        "SERP: google visual trim + publisher thumbnails; "
        "favicon/layout/recaptcha/async pass-through; "
        "target: allow GA/GTM/scripts + block visual assets)"
      )
      return True
    except Exception as exc:
      self.log(f"[Network] Route setup failed (continuing without optimizer): {exc}")
      return False

  def reattach_page(self, page: Page, *, mobile: bool) -> bool:
    """Attach routes to a new tab (mobile needs per-page routes)."""
    return self.attach(page, mobile=mobile)

  def _log_block_summary(self) -> None:
    total = sum(self._blocked_by_type.values())
    if total <= 0:
      return
    parts = ", ".join(f"{k}={v}" for k, v in sorted(self._blocked_by_type.items()))
    self.log(f"[Network] Blocked {total} requests ({parts})")

  def _log_blocked_script(self, url: str) -> None:
    if self._blocked_script_log_count >= self._blocked_script_log_limit:
      return
    self._blocked_script_log_count += 1
    short = (url or "").strip()[:140]
    self.log(f"[Network] Blocked script: {short}")

  def report_traffic(self, *, force: bool = False, include_baseline: bool = False) -> None:
    if not force and self.monitor.request_count <= 0:
      return
    self.log(self.monitor.format_log(
      self.profile_name,
      self._current_keyword,
      include_baseline=include_baseline,
    ))

  # ---------------------------------------------------------------------------
  # Decision logic
  # ---------------------------------------------------------------------------

  def should_abort(self, request: Request) -> bool:
    if not self.blocking_enabled:
      return False

    url = (request.url or "").lower()
    host = urlparse(url).netloc.lower().removeprefix("www.")
    resource_type = (request.resource_type or "").lower()

    if self.is_ga_whitelisted(url, host):
      return False
    if self.is_captcha_or_security_url(url, host):
      return False
    if self.is_ip_check_url(host):
      return False
    if self.is_wix_runtime_whitelisted(url, host, resource_type):
      return False

    # YouTube preview CDN — block in every phase (including DEFAULT before SERP).
    if self._is_youtube_thumbnail_host(host):
      return True

    if self.phase not in (PagePhase.GOOGLE_SERP, PagePhase.TARGET_SITE):
      return False

    if self._should_abort_common(resource_type, url, host):
      return True
    if self.phase == PagePhase.GOOGLE_SERP:
      return self._should_abort_serp(resource_type, url, host)
    return self._should_abort_target(resource_type, url, host)

  def _should_abort_common(self, resource_type: str, url: str, host: str) -> bool:
    """Phase-agnostic blocks: fonts, websockets, ads, trackers, video embeds."""
    if resource_type == "font":
      return True
    if any(token in url for token in self._FONT_URL_TOKENS):
      return True
    if self._has_extension(url, self._FONT_EXTENSIONS):
      return True

    if resource_type in ("websocket", "eventsource"):
      return True
    if "eventsource" in url:
      return True

    if self._is_ad_host(host, url):
      return True
    if resource_type in ("script", "xhr", "fetch", "other", "ping", "image", "media"):
      if self._is_blocked_tracker_host(host, url):
        return True

    if any(token in url for token in self._HEAVY_EMBED_URL_TOKENS):
      return True

    return False

  def _should_abort_serp(self, resource_type: str, url: str, host: str) -> bool:
    """SERP: trim heavy embeds/CDNs; never block layout-critical Google search chrome."""
    lowered = (url or "").lower()
    kind = (resource_type or "").lower()
    is_gstatic = host.endswith("gstatic.com") or host == "gstatic.com"
    is_google_infra = is_gstatic or self._is_google_host(host)

    if self._is_serp_layout_critical(lowered, host):
      return False

    if self._is_serp_favicon_url(lowered):
      return False

    if is_google_infra:
      if self._is_serp_minimal_chrome_url(lowered):
        return False
      if "recaptcha" in lowered:
        return False
      if kind in ("script", "stylesheet"):
        return False

    if self._is_serp_heavy_media_host(host):
      return True

    if kind == "document" and self._is_serp_third_party_embed_url(lowered):
      return True

    if is_google_infra:
      if "googleusercontent.com" in lowered and not self._should_block_googleusercontent_url(lowered):
        return False
      if kind in ("image", "media"):
        return True
      if self._has_extension(url, self._IMAGE_EXTENSIONS + self._VIDEO_EXTENSIONS):
        return True
      if kind in ("xhr", "fetch", "other", "ping") and self._is_google_serp_visual_hint_url(lowered):
        return True
      if any(token in lowered for token in self._SERP_BANDWIDTH_URL_TOKENS):
        return True
      return False

    # Publisher/news thumbnails on SERP (non-Google hosts).
    if kind in ("image", "media"):
      return True
    if self._has_extension(url, self._IMAGE_EXTENSIONS + self._VIDEO_EXTENSIONS):
      return True
    return False

  @classmethod
  def _is_youtube_thumbnail_host(cls, host: str) -> bool:
    normalized = (host or "").lower().removeprefix("www.")
    return any(
      normalized == blocked or normalized.endswith(f".{blocked}")
      for blocked in cls._YOUTUBE_THUMBNAIL_HOSTS
    )

  @classmethod
  def _is_serp_favicon_url(cls, lowered_url: str) -> bool:
    return "/s2/favicons" in lowered_url or "faviconv2" in lowered_url

  @classmethod
  def _should_block_googleusercontent_url(cls, lowered_url: str) -> bool:
    if "googleusercontent.com" not in lowered_url:
      return False
    if cls._is_serp_favicon_url(lowered_url):
      return False
    if any(token in lowered_url for token in cls._GOOGLEUSERCONTENT_SMALL_ALLOW):
      return False
    if "=w" in lowered_url or "-h" in lowered_url:
      return True
    if any(token in lowered_url for token in ("=s0", "=s64", "=s128", "=s200", "=s400")):
      return True
    return True

  @classmethod
  def _is_google_serp_visual_hint_url(cls, lowered_url: str) -> bool:
    if cls._is_serp_favicon_url(lowered_url):
      return False
    return any(hint in lowered_url for hint in cls._GOOGLE_SERP_VISUAL_HINTS)

  @classmethod
  def _is_serp_layout_critical(cls, lowered_url: str, host: str) -> bool:
    normalized = (host or "").lower().removeprefix("www.")
    if any(
      normalized == allowed or normalized.endswith(f".{allowed}")
      for allowed in cls._SERP_LAYOUT_CRITICAL_HOSTS
    ):
      return True
    return any(token in lowered_url for token in cls._SERP_LAYOUT_CRITICAL_URL_TOKENS)

  @classmethod
  def _is_serp_heavy_media_host(cls, host: str) -> bool:
    normalized = (host or "").lower().removeprefix("www.")
    return any(
      normalized == blocked or normalized.endswith(f".{blocked}")
      for blocked in cls._SERP_HEAVY_MEDIA_HOSTS
    )

  @classmethod
  def _is_serp_third_party_embed_url(cls, lowered_url: str) -> bool:
    """Block iframe/document navigations to embed shells unrelated to core SERP layout."""
    return any(token in lowered_url for token in cls._SERP_THIRD_PARTY_EMBED_URL_TOKENS)

  @classmethod
  def _is_serp_media_host(cls, host: str) -> bool:
    """Backward-compatible alias for heavy-media host checks."""
    return cls._is_serp_heavy_media_host(host)

  @classmethod
  def _is_serp_minimal_chrome_url(cls, lowered_url: str) -> bool:
    if any(token in lowered_url for token in cls._SERP_MINIMAL_CHROME_URL_TOKENS):
      return True
    if "recaptcha" in lowered_url:
      return True
    return False

  @classmethod
  def _serp_request_looks_like_visual_media(cls, resource_type: str, lowered_url: str) -> bool:
    if resource_type in ("image", "media"):
      return True
    if resource_type not in ("xhr", "fetch", "other", "ping"):
      return False
    visual_hints = (
      "encrypted-tbn",
      "/tbn/",
      "thumbnail",
      "imgurl",
      "imgres",
      "googlevideo",
      "videoplayback",
      "ytimg.com",
      "ggpht.com",
      "googleusercontent.com",
      "/vi/",
      "/maps/embed",
      "/maps/preview",
      "tbm=vid",
      "tbm=isch",
      ".jpg",
      ".jpeg",
      ".png",
      ".webp",
      ".gif",
      ".avif",
      ".mp4",
      ".webm",
      ".m3u8",
    )
    return any(hint in lowered_url for hint in visual_hints)

  def _should_abort_target(self, resource_type: str, url: str, host: str) -> bool:
    """Target track: allow HTML/CSS/JS/xhr for GA; block all visual assets."""
    if any(token in url for token in self._TARGET_BRANDING_URL_TOKENS):
      return True
    if resource_type in ("image", "media", "font"):
      return True
    if self._has_extension(
      url,
      self._IMAGE_EXTENSIONS + self._VIDEO_EXTENSIONS + self._FONT_EXTENSIONS,
    ):
      return True
    if self._looks_like_heavy_asset_url(url):
      return True
    if resource_type in ("other", "ping") and self._has_extension(
      url,
      self._IMAGE_EXTENSIONS + self._VIDEO_EXTENSIONS + self._FONT_EXTENSIONS,
    ):
      return True

    if resource_type in ("document", "stylesheet"):
      return False
    if resource_type in ("xhr", "fetch"):
      return False

    if resource_type == "script":
      if self._is_google_host(host):
        return False
      if self._host_matches_target(host):
        return False
      return False

    if resource_type in ("websocket", "eventsource", "manifest"):
      return True
    if resource_type in ("other", "ping") and self._is_heavy_third_party_media(host, url):
      return True

    return False

  @classmethod
  def is_google_serp_infrastructure(cls, url: str, host: str) -> bool:
    """Minimal SERP chrome URLs (logo/searchbox beacons). Captcha uses separate whitelist."""
    lowered = (url or "").lower()
    if cls._is_serp_media_host((host or "").lower().removeprefix("www.")):
      return False
    if host.endswith("gstatic.com") or host == "gstatic.com":
      if "recaptcha" in lowered:
        return True
      return False
    if not cls._is_google_host(host):
      return False
    return cls._is_serp_minimal_chrome_url(lowered)

  # ---------------------------------------------------------------------------
  # Whitelists & classifiers
  # ---------------------------------------------------------------------------

  @classmethod
  def is_ga_whitelisted(cls, url: str, host: str) -> bool:
    if any(host == domain or host.endswith(f".{domain}") for domain in cls._GA_HOSTS):
      return True
    if host.endswith("stats.g.doubleclick.net") and ("/collect" in url or "/g/collect" in url):
      return True
    if cls._is_google_host(host) and "/ccm/collect" in url:
      return True
    return any(token in url for token in cls._GA_URL_TOKENS)

  @classmethod
  def is_wix_runtime_whitelisted(cls, url: str, host: str, resource_type: str) -> bool:
    """Allow Wix thunderbolt/service JS bundles required for site + GA bootstrap."""
    if (resource_type or "").lower() != "script":
      return False
    if not any(host == domain or host.endswith(f".{domain}") for domain in cls._WIX_RUNTIME_HOSTS):
      return False
    path = urlparse(url).path.lower()
    if "/services/" in path:
      return True
    return path.endswith(".js")

  @classmethod
  def is_ip_check_url(cls, host: str) -> bool:
    normalized = (host or "").lower().removeprefix("www.")
    return normalized in _IP_CHECK_HOSTS

  @classmethod
  def is_captcha_or_security_url(cls, url: str, host: str) -> bool:
    lowered = (url or "").lower()
    if any(token in lowered for token in cls._CAPTCHA_TOKENS):
      return True
    if host.endswith("gstatic.com") and "recaptcha" in lowered:
      return True
    if cls._is_google_host(host) and (
      "recaptcha" in lowered
      or "/sorry/" in lowered
      or "captcha" in lowered
    ):
      return True
    return False

  @classmethod
  def _is_blocked_tracker_host(cls, host: str, url: str) -> bool:
    if any(token in host for token in cls._BLOCKED_TRACKER_HOSTS):
      return True
    tracker_url_tokens = (
      "facebook.com/tr",
      "connect.facebook.net",
      "wcs.naver.net",
      "nelo.naver.com",
      "nlog.naver.com",
    )
    return any(token in url for token in tracker_url_tokens)

  @staticmethod
  def _has_extension(url: str, extensions: tuple[str, ...]) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) or f"{ext}?" in url for ext in extensions)

  @staticmethod
  def _is_google_host(host: str) -> bool:
    google_hosts = (
      "google.com",
      "google.co.kr",
      "google.com.hk",
      "googleapis.com",
      "gstatic.com",
      "googleusercontent.com",
      "ggpht.com",
    )
    return any(host == g or host.endswith(f".{g}") for g in google_hosts)

  def _host_matches_target(self, host: str) -> bool:
    if not self.target_hosts:
      return False
    for target_host in self.target_hosts:
      if host == target_host or host.endswith(f".{target_host}"):
        return True
    return False

  def host_matches_target(self, host: str) -> bool:
    normalized = (host or "").lower().removeprefix("www.")
    return self._host_matches_target(normalized)

  @classmethod
  def _is_ad_host(cls, host: str, url: str) -> bool:
    if any(token in host for token in cls._AD_HOSTS):
      if cls.is_ga_whitelisted(url, host):
        return False
      return True
    return False

  @staticmethod
  def _looks_like_heavy_asset_url(url: str) -> bool:
    lowered = (url or "").lower()
    tokens = (
      "/wp-content/uploads/",
      "/uploads/",
      "/images/",
      "/image/",
      "/img/",
      "/photos/",
      "/photo/",
      "/banner/",
      "/thumbs/",
      "/thumbnail",
      "featured-image",
      "/assets/media/",
      "cloudinary.com",
      "ytimg.com",
      "googlevideo.com",
      "youtube.com/embed",
      "youtube-nocookie.com",
      "player.vimeo.com",
      "dailymotion.com",
    )
    return any(token in lowered for token in tokens)

  @classmethod
  def _is_heavy_third_party_media(cls, host: str, url: str = "") -> bool:
    media_hosts = (
      "youtube.com",
      "youtube-nocookie.com",
      "ytimg.com",
      "googlevideo.com",
      "vimeo.com",
      "dailymotion.com",
      "tiktok.com",
      "tiktokcdn.com",
    )
    if any(token in host for token in media_hosts):
      return True
    lowered = (url or "").lower()
    embed_tokens = (
      "youtube.com/embed",
      "youtube-nocookie.com/embed",
      "player.vimeo.com",
      "googlevideo.com/videoplayback",
    )
    return any(token in lowered for token in embed_tokens)


def classify_network_error(exc: BaseException) -> str:
  """Classify network errors for retry / recovery policy."""
  text = str(exc or "").upper()
  name = type(exc).__name__.upper()

  if "CAPTCHA" in text or "/SORRY/" in text:
    return "captcha"
  if (
    "TARGET PAGE, CONTEXT OR BROWSER HAS BEEN CLOSED" in text
    or "TARGETCLOSEDERROR" in text
    or "BROWSERCLOSEDERROR" in text
    or name in ("TARGETCLOSEDERROR", "BROWSERCLOSEDERROR")
  ):
    return "browser_crash"
  if "ERR_NAME_NOT_RESOLVED" in text or "ENOTFOUND" in text or "DNS" in text:
    return "dns"
  if (
    "ERR_TUNNEL_CONNECTION_FAILED" in text
    or "ERR_PROXY_AUTH_REQUESTED" in text
    or "ERR_PROXY_CONNECTION_FAILED" in text
    or "ERR_INVALID_AUTH_CREDENTIALS" in text
  ):
    return "tunnel"
  if (
    "ERR_CONNECTION_TIMED_OUT" in text
    or "TIMEOUT" in text
    or "TIMED OUT" in text
    or name == "TIMEOUTERROR"
  ):
    return "timeout"
  if (
    "ERR_ABORTED" in text
    or "ERR_CONNECTION_RESET" in text
    or "ERR_CONNECTION_CLOSED" in text
    or "FRAME WAS DETACHED" in text
    or "EXECUTION CONTEXT WAS DESTROYED" in text
  ):
    return "connection"
  return "other"
