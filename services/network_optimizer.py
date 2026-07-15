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
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from playwright.sync_api import Page, Request, Route

TRAFFIC_LOG_PATH = Path("data/traffic_sessions.jsonl")

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
    self._baseline_allowed = 0

  def set_baseline(self) -> None:
    """Snapshot current allowed bytes (for before/after comparison)."""
    self._baseline_allowed = self.allowed_bytes

  def record_allowed(self, size_bytes: int) -> None:
    self.request_count += 1
    if size_bytes > 0:
      self.allowed_bytes += size_bytes

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
  ) -> str:
    total = self._human_bytes(self.total_bytes)
    blocked = self._human_bytes(self.blocked_bytes)
    allowed = self._human_bytes(self.allowed_bytes)
    saved_pct = self.savings_percent
    lines = [
      "[TRAFFIC]",
      f"  profile: {profile_name}",
      f"  keyword: {keyword or '(session)'}",
      f"  requests: {self.request_count} (blocked={self.blocked_count})",
      f"  total: {total}",
      f"  allowed: {allowed}",
      f"  blocked: {blocked}",
      f"  saved: {saved_pct}%",
    ]
    if include_baseline and self._baseline_allowed > 0:
      before = self._human_bytes(self._baseline_allowed + self.blocked_bytes)
      after = self._human_bytes(self.allowed_bytes)
      lines.append(f"  before(est): {before}")
      lines.append(f"  after: {after}")
    return "\n".join(lines)

  @staticmethod
  def _human_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
      if size < 1024.0 or unit == "GB":
        if unit == "B":
          return f"{int(size)}{unit}"
        return f"{size:.1f}{unit}"
      size /= 1024.0
    return f"{size:.1f}GB"

  def append_session_log(self, profile_name: str, keyword: str = "") -> None:
    try:
      TRAFFIC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
      payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "profile": profile_name,
        "keyword": keyword,
        "request_count": self.request_count,
        "blocked_count": self.blocked_count,
        "allowed_bytes": self.allowed_bytes,
        "blocked_bytes": self.blocked_bytes,
        "saved_percent": self.savings_percent,
      }
      with TRAFFIC_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
      pass


class NetworkOptimizer:
  """Manages Playwright route interception with GA4 protection and phase rules."""

  _IMAGE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg",
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
  _TRACKER_HOSTS = (
    "facebook.net",
    "facebook.com",
    "connect.facebook.net",
    "twitter.com",
    "analytics.twitter.com",
    "t.co",
    "hotjar.com",
    "hotjar.io",
    "clarity.ms",
    "heatmap.com",
    "criteo.com",
    "taboola.com",
    "outbrain.com",
    "snap.licdn.com",
    "bat.bing.com",
    "tiktokcdn.com",
    "tiktok.com",
    "pinimg.com",
    "cdninstagram.com",
    "amazon-adsystem.com",
    "adsafeprotected.com",
    "moatads.com",
    "scorecardresearch.com",
    "quantserve.com",
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
  _GOOGLE_SEARCH_UI_TOKENS = (
    "/images/searchbox/",
    "/images/nav_logo",
    "/images/branding/googlelogo",
    "/images/branding/",
    "/gen_204?",
    "fonts.gstatic.com/s/i/productlogos",
    "fonts.gstatic.com",
    "gstatic.com/images/icons",
  )

  def __init__(
    self,
    target_host: str,
    log: Callable[[str], None],
    *,
    profile_name: str = "",
    target_hosts: list[str] | None = None,
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
    self.phase = PagePhase.DEFAULT
    self.monitor = TrafficMonitor()
    self._current_keyword = ""
    self._route_attached = False
    self._mobile_routed_pages: set[int] = set()
    self._blocked_by_type: dict[str, int] = {}
    self._blocked_script_log_count = 0
    self._blocked_script_log_limit = 8

  def set_phase(self, phase: PagePhase) -> None:
    if phase != self.phase:
      self.log(f"[Network] Phase → {phase.value}")
    self.phase = phase

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
          if self.phase == PagePhase.TARGET_SITE and kind == "script":
            self._log_blocked_script(request.url or "")
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
        f"[Network] Optimizer active "
        f"(SERP=block images/video; target={self.target_host or '?'} slim)"
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
    url = (request.url or "").lower()
    host = urlparse(url).netloc.lower().removeprefix("www.")
    resource_type = (request.resource_type or "").lower()

    if self.is_ga_whitelisted(url, host):
      return False
    if self.is_wix_runtime_whitelisted(url, host, resource_type):
      return False
    if self.is_captcha_or_security_url(url, host):
      return False
    if self.is_ip_check_url(host):
      return False

    if self.phase == PagePhase.GOOGLE_SERP:
      return self._should_abort_google_serp(resource_type, url, host)

    if self.phase != PagePhase.TARGET_SITE:
      return False

    if self._is_ad_host(host, url):
      return True
    if self._is_tracker_host(host, url):
      return True
    if self._has_extension(url, self._VIDEO_EXTENSIONS):
      return True
    if self._is_heavy_third_party_media(host, url):
      return True

    return self._should_abort_target_site(resource_type, url, host)

  def _should_abort_google_serp(self, resource_type: str, url: str, host: str) -> bool:
    """SERP: block images/video in results, ads, and video units; keep JS/CSS/fonts/XHR."""
    if self._is_google_search_ui_asset(url, host):
      return False
    if resource_type in ("image", "media"):
      return True
    if self._has_extension(url, self._IMAGE_EXTENSIONS + self._VIDEO_EXTENSIONS):
      return True
    if self._is_serp_visual_media_url(url):
      return True
    if self._is_heavy_third_party_media(host, url):
      return True
    if self._is_ad_creative_media(resource_type, url, host):
      return True
    return False

  @staticmethod
  def _is_serp_visual_media_url(url: str) -> bool:
    lowered = (url or "").lower()
    tokens = (
      "encrypted-tbn",
      "ggpht.com",
      "googleusercontent.com",
      "/imgres?",
      "tbn:",
      "googlevideo.com",
      "videoplayback",
      "ytimg.com/vi/",
      "i.ytimg.com",
    )
    return any(token in lowered for token in tokens)

  @classmethod
  def _is_ad_creative_media(cls, resource_type: str, url: str, host: str) -> bool:
    """Block ad image/video assets on SERP; allow ad scripts/iframes."""
    if not cls._is_ad_host(host, url):
      return False
    if resource_type in ("image", "media"):
      return True
    return cls._has_extension(url, cls._IMAGE_EXTENSIONS + cls._VIDEO_EXTENSIONS)

  def _should_abort_target_site(self, resource_type: str, url: str, host: str) -> bool:
    """Target dwell only: keep HTML/JS/XHR + GA/GTM; drop heavy assets."""
    if resource_type in ("document", "xhr", "fetch"):
      return False

    if resource_type == "script":
      if self.is_ga_whitelisted(url, host):
        return False
      if self.is_wix_runtime_whitelisted(url, host, resource_type):
        return False
      if self._host_matches_target(host):
        return False
      if self._is_ad_host(host, url) or self._is_tracker_host(host, url):
        return True
      return True

    if resource_type == "stylesheet":
      if self._host_matches_target(host):
        return False
      return True

    if resource_type in ("media", "font", "image"):
      return True

    if self._has_extension(url, self._FONT_EXTENSIONS + self._VIDEO_EXTENSIONS + self._IMAGE_EXTENSIONS):
      return True

    if self._looks_like_heavy_asset_url(url):
      return True

    if resource_type in ("websocket", "eventsource", "manifest"):
      return True

    return False

  def _should_abort_default(self, resource_type: str, url: str, host: str) -> bool:
    if resource_type in ("media", "font"):
      return True
    if self._has_extension(url, self._IMAGE_EXTENSIONS + self._FONT_EXTENSIONS + self._VIDEO_EXTENSIONS):
      return True
    if self._is_google_host(host):
      if resource_type == "image":
        return True
      if self._is_heavy_google_asset(url):
        return True
      return False
    if self.target_host and self._host_matches_target(host):
      return False
  # Non-target third-party heavy assets
    if self._is_heavy_third_party_media(host):
      return True
    return resource_type == "image"

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
    if any(token in url for token in cls._CAPTCHA_TOKENS):
      return True
    if host.endswith("gstatic.com") and "recaptcha" in url:
      return True
    return False

  @classmethod
  def _is_google_search_ui_asset(cls, url: str, host: str) -> bool:
    if not cls._is_google_host(host):
      return False
    return any(token in url for token in cls._GOOGLE_SEARCH_UI_TOKENS)

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
    )
    return any(host == g or host.endswith(f".{g}") for g in google_hosts)

  @staticmethod
  def _is_heavy_google_asset(url: str) -> bool:
    heavy_tokens = (
      "encrypted-tbn",
      "ggpht.com",
      "googleusercontent.com",
      "/logos/",
      "favicon",
    )
    return any(token in url for token in heavy_tokens)

  def _host_matches_target(self, host: str) -> bool:
    if not self.target_hosts:
      return False
    for target_host in self.target_hosts:
      if host == target_host or host.endswith(f".{target_host}"):
        return True
    return False

  @classmethod
  def _is_ad_host(cls, host: str, url: str) -> bool:
    if any(token in host for token in cls._AD_HOSTS):
      if cls.is_ga_whitelisted(url, host):
        return False
      return True
    return False

  @classmethod
  def _is_tracker_host(cls, host: str, url: str) -> bool:
    if any(token in host for token in cls._TRACKER_HOSTS):
      return True
    tracker_url_tokens = (
      "facebook.com/tr",
      "connect.facebook.net",
      "analytics.twitter.com",
      "twitter.com/i/adsct",
    )
    return any(token in url for token in tracker_url_tokens)

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
