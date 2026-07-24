import json
import random
import re
import threading
import time
from typing import Callable, Literal, Optional, Tuple, Union
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from playwright.sync_api import Browser, Page, sync_playwright

from config.bot_config import BotConfig
from core.profile_status import ProfileStatus, UiStatusKey, short_error_detail
from services.adspower_manager import ProfileSpec
from services.captcha_solver import CaptchaSolver, CaptchaStatCallback
from services.google_consent import (
  deny_google_geolocation,
  dismiss_google_consent,
  dismiss_google_location_prompt,
  is_google_consent_present,
  is_google_location_prompt_present,
  seed_google_consent_cookies,
)
from services.network_optimizer import NetworkOptimizer, PagePhase, classify_network_error
from utils.app_paths import data_dir
from utils.crash_reporter import capture_exception
from utils.csv_logger import CsvRankLogger, KeywordHistoryLogger, SessionClickCsvLogger
from utils.page_search_order import build_desktop_page_order, pending_desktop_scan_pages
from utils.pair_rotation import PairRotationStore
from utils.serp_result_store import SerpResultStore
from utils.wire_traffic_meter import WireTrafficMeter
from utils.human import (
  dispatch_serp_anchor_touch_tap,
  dispatch_touch_tap,
  enable_mobile_touch,
  get_viewport_touch_metrics,
  human_click,
  human_touch_click,
  human_type,
  human_type_focus_safe,
  micro_scroll,
  random_delay,
  scroll_page,
)

StatusCallback = Callable[[ProfileStatus], None]
UiStatusCallback = Callable[[str, str], None]
FailureCallback = Callable[[ProfileSpec, str, BaseException], None]
TrafficCallback = Callable[[int, int, int, int, int, int], None]
KeywordExhaustedCallback = Callable[[str], None]
TargetClickCallback = Callable[[], None]


class SerpBot:
  _TARGET_LOAD_RETRY_ATTEMPTS = 3
  _TARGET_LOAD_RETRY_WAIT_SECONDS = (10.0, 15.0)
  _TARGET_SERP_RETAP_ATTEMPTS = 3
  _TARGET_SERP_RETAP_WAIT_SECONDS = (10.0, 15.0)
  GOOGLE_ENTRY_URL = "https://www.google.co.kr/"
  GOOGLE_CONNECT_DEADLINE_SECONDS = 90.0
  _MOBILE_GOOGLE_SEARCH_BAR_SELECTORS = (
    'form[role="search"]',
    'div[role="search"]',
    'motion-promo form',
    'motion-promo',
    'motion-promo-header',
    'motion-promo-header-content',
    'div#searchform',
    'header form',
    '[aria-label*="Google 검색"]',
    '[aria-label*="Google Search"]',
  )
  GOTO_TIMEOUT_MS = 35_000
  GOTO_TIMEOUT_MOBILE_MS = 28_000
  CONSECUTIVE_CAPTCHA_LIMIT = 3
  _MOBILE_SERP_RESULTS_PER_PAGE = 10

  def __init__(self, config: BotConfig, logger: Callable[[str], None]):
    self.config = config
    self.logger = logger
    self.captcha = CaptchaSolver(config.capsolver_api_key, logger)
    self.csv = CsvRankLogger(data_dir() / "results.csv")
    self._session_click_log: Optional[SessionClickCsvLogger] = None
    if (config.session_click_log_path or "").strip():
      self._session_click_log = SessionClickCsvLogger(config.session_click_log_path)
    self.keyword_history = KeywordHistoryLogger(config.primary_target_domain)
    self.result_store = SerpResultStore()
    self._session_target_domain: str = ""
    self._last_search_exhausted = False
    self._last_search_exhaustion_eligible = False
    self._session_network: Optional[NetworkOptimizer] = None
    self._mobile_serp_page = 1
    self._mobile_more_probe_done = False
    self._mobile_serp_end_reached = False
    self._session_has_searched = False
    self._session_page_binder: Optional[Callable[[Page], Page]] = None
    self._session_captcha_events = 0
    self._response_tracked_pages: set[int] = set()
    self._navigation_diagnostic_pages: set[int] = set()
    self._mobile_manual_target_landed = False
    self._last_target_open_failed = False
    self._session_wire_meter: Optional[WireTrafficMeter] = None
    self._google_connect_deadline: Optional[float] = None

  @property
  def last_search_exhaustion_eligible(self) -> bool:
    return bool(self._last_search_exhaustion_eligible)

  @property
  def last_target_open_failed(self) -> bool:
    return bool(self._last_target_open_failed)

  def _bind_session_page(self, page: Page) -> Page:
    if page is None or page.is_closed():
      return page
    binder = self._session_page_binder
    if binder is not None:
      try:
        return binder(page)
      except Exception:
        pass
    return page

  @staticmethod
  def _urls_match_for_back(expected: str, actual: str) -> bool:
    """Compare locations ignoring query/hash (Wix and Google often append params)."""
    try:
      pe = urlparse((expected or "").strip())
      pa = urlparse((actual or "").strip())
      if (pe.netloc or "").lower().removeprefix("www.") != (pa.netloc or "").lower().removeprefix("www."):
        return False
      ep = (pe.path or "/").rstrip("/") or "/"
      ap = (pa.path or "/").rstrip("/") or "/"
      return ep == ap
    except Exception:
      return (expected or "").split("?")[0].split("#")[0] == (actual or "").split("?")[0].split("#")[0]

  def _attach_response_handler(self, page: Page, handle_response: Callable) -> None:
    if page is None or page.is_closed():
      return
    page_key = id(page)
    if page_key in self._response_tracked_pages:
      return
    try:
      page.on("response", handle_response)
      self._response_tracked_pages.add(page_key)
    except Exception:
      pass

  def _attach_mobile_navigation_diagnostic(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
  ) -> None:
    """Record main-frame URLs, including user-initiated result clicks."""
    if page is None or page.is_closed() or not self._is_mobile_profile(profile):
      return
    page_key = id(page)
    if page_key in self._navigation_diagnostic_pages:
      return

    def record_navigation(frame) -> None:
      try:
        if frame != page.main_frame:
          return
        url = (frame.url or "").strip()
        if not url:
          return
        on_target = self._host_matches_any_target(
          self._normalize_domain(url) or "",
        )
        if on_target:
          self._mobile_manual_target_landed = True
          log(
            f"[Search] Mobile MANUAL_TARGET_TAP → {url[:220]} "
            f"(assigned={self._session_target_domain or '—'})"
          )
      except Exception:
        pass

    try:
      page.on("framenavigated", record_navigation)
      self._navigation_diagnostic_pages.add(page_key)
    except Exception:
      pass

  def _history_back(
    self,
    page: Page,
    log: Callable[[str], None],
    *,
    timeout_ms: int = 15000,
    success_check: Optional[Callable[[], bool]] = None,
    success_log: str = "",
    warning_label: str = "go_back",
  ) -> Page:
    """Browser back with commit wait; treat navigation as success if URL already matches."""
    page = self._bind_session_page(page)
    if success_check is not None and success_check():
      if success_log:
        log(success_log)
      return page
    try:
      page.go_back(wait_until="commit", timeout=timeout_ms)
      page.wait_for_timeout(random.randint(350, 700))
      if success_log and (success_check is None or success_check()):
        log(success_log)
    except Exception as exc:
      if success_check is not None and success_check():
        if success_log:
          log(f"{success_log} (navigation completed despite timeout)")
        return page
      log(f"[Target] {warning_label} warning: {exc}")
    return page

  @staticmethod
  def _proxy_label(profile: ProfileSpec) -> str:
    return f"{profile.proxy_host}:{profile.proxy_port}"

  def _set_network_phase(
    self,
    phase: PagePhase,
    keyword: str = "",
    page: Optional[Page] = None,
  ) -> None:
    if not self._session_network:
      return
    self._session_network.set_phase(phase)
    if page and not page.is_closed():
      self._session_network.apply_phase_headers(page)
    if keyword:
      self._session_network.set_keyword(keyword)
      self.captcha.update_keyword_context(keyword)

  def _configure_network_optimizer(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
    network: NetworkOptimizer,
  ) -> None:
    if self._is_ios_profile(profile):
      log(
        f"[Network] Skipping Playwright routes on {profile.os_browser_label} "
        "(AdsPower iOS session)"
      )
      return
    try:
      network.monitor.set_baseline()
    except Exception as exc:
      log(f"[Network] Baseline setup warning: {exc}")
    if not self.config.resource_blocking_enabled:
      log("[Network] Resource blocking disabled in settings — all requests allowed")
      try:
        network.apply_phase_headers(page)
      except Exception as exc:
        log(f"[Network] Phase header setup warning: {exc}")
      return
    mobile = self._is_mobile_profile(profile)
    if not network.attach(page, mobile=mobile):
      log("[Network] Route optimizer unavailable — traffic counted without blocking")
    try:
      network.apply_phase_headers(page)
    except Exception as exc:
      log(f"[Network] Phase header setup warning: {exc}")

  def _cleanup_session(
    self,
    browser: Optional[Browser],
    log: Callable[[str], None],
    network: Optional[NetworkOptimizer],
    on_session_cleanup: Optional[Callable[[], None]] = None,
    wire_meter: Optional[WireTrafficMeter] = None,
  ) -> None:
    if network:
      network.report_traffic(force=True, include_baseline=True)
      network.monitor.append_session_log(
        network.profile_name,
        network._current_keyword,
        wire_meter=wire_meter,
      )

    closed_tabs = 0
    if browser:
      try:
        for context in list(browser.contexts):
          for tab in list(context.pages):
            try:
              if not tab.is_closed():
                tab.close()
                closed_tabs += 1
            except Exception:
              pass
          try:
            context.close()
          except Exception:
            pass
        browser.close()
        log(f"Closed {closed_tabs} tab(s), context(s), and disconnected browser")
      except Exception as exc:
        log(f"Browser cleanup warning: {exc}")

    self._session_network = None

    if on_session_cleanup:
      try:
        on_session_cleanup()
        log("Stopped AdsPower profile")
      except Exception as exc:
        log(f"AdsPower stop warning: {exc}")

  @staticmethod
  def _close_all_tabs_and_browser(browser: Browser, log: Callable[[str], None]) -> None:
    SerpBot._cleanup_session_static(browser, log)

  @staticmethod
  def _cleanup_session_static(browser: Browser, log: Callable[[str], None]) -> None:
    closed = 0
    for context in list(browser.contexts):
      for tab in list(context.pages):
        try:
          if not tab.is_closed():
            tab.close()
            closed += 1
        except Exception:
          pass
      try:
        context.close()
      except Exception:
        pass
    try:
      browser.close()
      log(f"Closed {closed} tab(s) and disconnected browser")
    except Exception as exc:
      log(f"Browser disconnect warning: {exc}")

  def run_session(
    self,
    ws_endpoint: str,
    profile: ProfileSpec,
    stop_event: Optional[threading.Event] = None,
    keywords_override: Optional[list[str]] = None,
    assigned_keyword: Optional[str] = None,
    assigned_domain: Optional[str] = None,
    max_attempts: Optional[int] = None,
    on_status: Optional[StatusCallback] = None,
    on_ui_status: Optional[UiStatusCallback] = None,
    on_traffic: Optional[TrafficCallback] = None,
    on_failure: Optional[FailureCallback] = None,
    on_keyword_exhausted: Optional[KeywordExhaustedCallback] = None,
    on_target_click: Optional[TargetClickCallback] = None,
    on_captcha_stat: Optional[CaptchaStatCallback] = None,
    on_session_cleanup: Optional[Callable[[], None]] = None,
  ) -> str:
    def set_status(status: ProfileStatus, detail: str = "") -> None:
      key, text = status.to_ui(detail=detail)
      if on_ui_status:
        on_ui_status(key, text)
      elif on_status:
        on_status(status)

    def stopped() -> bool:
      return bool(stop_event and stop_event.is_set())

    def log(msg: str) -> None:
      self.logger(f"[{profile.name}] {msg}")

    log("[Session] Starting")
    self._google_connect_deadline = None
    self.captcha.reset_session_state()
    self._session_captcha_events = 0
    self._session_has_searched = False
    self._response_tracked_pages = set()
    self.captcha.set_session_logger(log)
    self.captcha.set_stats_callback(on_captcha_stat)
    self.captcha.update_api_key(self.config.capsolver_api_key)
    proxy_label = self._proxy_label(profile)
    self.captcha.set_session_context(
      profile_id=profile.profile_id,
      profile_name=profile.name,
      proxy=proxy_label,
      proxy_host=profile.proxy_host,
      proxy_port=profile.proxy_port,
      proxy_user=profile.proxy_user,
      proxy_pass=profile.proxy_pass,
    )
    target_hosts = self.config.get_target_domains()
    session_keyword = (assigned_keyword or profile.assigned_keyword or "").strip()
    session_domain = (assigned_domain or profile.assigned_domain or "").strip()
    if not session_keyword and keywords_override:
      session_keyword = keywords_override[0].strip()
    if not session_domain and target_hosts:
      session_domain = target_hosts[0]
    if session_keyword and session_domain:
      target_hosts = [session_domain]
      self._session_target_domain = session_domain
    else:
      self._session_target_domain = target_hosts[0] if target_hosts else ""
    network = NetworkOptimizer(
      self._session_target_domain or self.config.primary_target_domain,
      log,
      profile_name=profile.name,
      target_hosts=target_hosts,
      blocking_enabled=self.config.resource_blocking_enabled,
    )
    network.set_phase(PagePhase.GOOGLE_SERP)
    self._session_network = network
    if self.captcha.automated_mode:
      log(f"[CapSolver] Automated mode enabled ({self.captcha._mask_api_key()})")
    else:
      log("[CapSolver] No API key — captcha will remove profile")

    with sync_playwright() as playwright:
      browser: Browser = playwright.chromium.connect_over_cdp(ws_endpoint)
      page: Optional[Page] = None
      pending_report_bytes = 0
      pending_report_target_bytes = 0
      pending_report_other_bytes = 0
      report_threshold_bytes = 128 * 1024
      wire_meter: Optional[WireTrafficMeter] = None

      def report_traffic(force: bool = False) -> None:
        nonlocal pending_report_bytes, pending_report_target_bytes, pending_report_other_bytes
        wire_delta = 0
        wire_target = 0
        wire_other = 0
        if wire_meter is not None:
          wire_delta, wire_target, wire_other = wire_meter.take_pending_delta(force=force)
        if not on_traffic:
          return
        if (
          not force
          and pending_report_bytes < report_threshold_bytes
          and wire_delta <= 0
        ):
          return
        delta = pending_report_bytes
        delta_target = pending_report_target_bytes
        delta_other = pending_report_other_bytes
        pending_report_bytes = 0
        pending_report_target_bytes = 0
        pending_report_other_bytes = 0
        if delta > 0 or wire_delta > 0 or force:
          on_traffic(delta, delta_target, delta_other, wire_delta, wire_target, wire_other)

      wire_meter = WireTrafficMeter(
        on_flush=lambda: report_traffic(force=False),
      )
      self._session_wire_meter = wire_meter

      def handle_response(response) -> None:
        nonlocal pending_report_bytes, pending_report_target_bytes, pending_report_other_bytes
        size_bytes = self._response_size_bytes(response)
        if size_bytes <= 0:
          return
        is_target = wire_meter.count_as_site
        if is_target:
          pending_report_target_bytes += size_bytes
        else:
          pending_report_other_bytes += size_bytes
        pending_report_bytes += size_bytes
        network.monitor.record_allowed(size_bytes, is_target=is_target)
        report_traffic(force=False)

      def bind_session_page(active: Page) -> Page:
        nonlocal page
        if active is None or active.is_closed():
          return active
        page = active
        try:
          self._attach_response_handler(active, handle_response)
        except Exception:
          pass
        try:
          self._attach_mobile_navigation_diagnostic(active, profile, log)
        except Exception:
          pass
        try:
          wire_meter.attach(active)
        except Exception:
          pass
        if not self._is_ios_profile(profile) and self.config.resource_blocking_enabled:
          network.reattach_page(active, mobile=self._is_mobile_profile(profile))
          network.apply_phase_headers(active)
        return active

      self._session_page_binder = bind_session_page

      def rotate_tab(reason: str) -> Page:
        nonlocal page
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        try:
          if page and not page.is_closed():
            page.close()
        except Exception:
          pass
        self._close_extra_tabs(context, keep_page=None)
        page = context.new_page()
        page = bind_session_page(page)
        if self._is_mobile_profile(profile):
          page = self._prepare_mobile_session(page, profile, log)
        log(f"[Captcha] Rotated tab due to captcha ({reason})")
        return page

      try:
        page = self._open_work_page(browser, profile, log)
        work_ref = None
        if not self._is_ios_profile(profile):
          work_ref = self._attach_tab_guard(
            page.context, page, log, target_hosts=target_hosts,
          )
        if page.is_closed():
          page = self._recover_work_page(page.context, profile, log)
          if work_ref is not None:
            work_ref["page"] = page
        seed_google_consent_cookies(page.context, log)
        deny_google_geolocation(page, log)
        if page.is_closed():
          page = self._recover_work_page(page.context, profile, log)
          if work_ref is not None:
            work_ref["page"] = page
        if stopped():
          return "stopped"

        if self._is_mobile_profile(profile):
          page = self._prepare_mobile_session(page, profile, log)
          if work_ref is not None:
            work_ref["page"] = page
        self._begin_google_connect_watch()
        page = self._warm_up_browser_proxy(page, profile, log)
        if work_ref is not None:
          work_ref["page"] = page
        page, proxy_guard = self._check_google_proxy_connect(page, log)
        if proxy_guard:
          set_status(ProfileStatus.ERROR)
          return proxy_guard

        session_baseline_ip = ""
        if self.config.ip_check_session_start:
          set_status(ProfileStatus.CHECKING_IP)
          log("[IP] Checking proxy IP (waiting up to 10s before warm-up)...")
          session_baseline_ip = self._capture_session_ip(
            page, log, stopped=stopped, max_wait_seconds=10.0,
          )
          if session_baseline_ip:
            log(f"[IP] Proxy IP captured: {session_baseline_ip}")
          else:
            log(
              "[IP] Could not capture proxy IP through browser "
              "(continuing; keyword-2 IP compare will be skipped)."
            )
        else:
          log("[IP] Session-start IP check disabled in settings — skipping.")

        self._configure_network_optimizer(page, profile, log, network)
        if work_ref is not None and not work_ref["page"].is_closed():
          page = work_ref["page"]
        page = bind_session_page(page)

        page = self._sync_work_page(page, profile, log)
        if work_ref is not None:
          work_ref["page"] = page

        delay_lo = max(0, int(self.config.session_start_delay_min))
        delay_hi = max(0, int(self.config.session_start_delay_max))
        if delay_hi > 0 and delay_hi >= delay_lo:
          wait_s = random.uniform(float(delay_lo), float(delay_hi))
          if wait_s > 0:
            log(
              f"[Start] Session start delay: waiting {wait_s:.1f}s "
              "before warm-up (no scrolling)"
            )
            set_status(ProfileStatus.RUNNING)
            # Idle wait only — scrolling here made warm-up look like it still scrolled.
            self._interruptible_wait(
              page, int(wait_s * 1000), stopped, scroll_mobile=False,
            )
            if stopped():
              return "stopped"
            page = self._sync_work_page(page, profile, log)
            if work_ref is not None:
              work_ref["page"] = page

        log(
          "[Start] Warm-up from Google.co.kr "
          "(SERP search box typing; Android & Desktop)."
        )
        while True:
          if work_ref is not None and not work_ref["page"].is_closed():
            page = work_ref["page"]
          page = self._sync_work_page(page, profile, log)
          guard, page = self._ensure_captcha_clear(
            page, stop_event, set_status, on_ui_status, profile, log, context="startup",
          )
          if guard == "stopped":
            return "stopped"
          if guard == "blocked":
            set_status(ProfileStatus.ERROR)
            return "blocked"
          if guard == "error":
            set_status(ProfileStatus.ERROR)
            return "error"
          break

        if stopped():
          return "stopped"

        set_status(ProfileStatus.WARMING_UP)
        page, warmup_guard = self._warmup(
          page, profile, stop_event, stopped, set_status, on_ui_status, on_failure, log,
        )
        page = bind_session_page(page)
        if work_ref is not None:
          work_ref["page"] = page
        if warmup_guard == "stopped" or stopped():
          return "stopped"
        if warmup_guard == "blocked":
          set_status(ProfileStatus.ERROR, detail="Consecutive captcha")
          return "blocked"
        if warmup_guard == "error":
          set_status(ProfileStatus.ERROR)
          return "error"
        if warmup_guard == "proxy_connect_failed":
          set_status(ProfileStatus.ERROR)
          return "proxy_connect_failed"

        page, proxy_guard = self._check_google_proxy_connect(page, log)
        if proxy_guard:
          set_status(ProfileStatus.ERROR)
          return proxy_guard

        keyword = session_keyword
        target_domain = session_domain
        if not keyword or not target_domain:
          log("No keyword/domain pair configured for this profile")
          set_status(ProfileStatus.ERROR)
          return "error"

        all_keywords = [item.strip() for item in (self.config.keywords or []) if item and item.strip()]
        all_domains = self.config.get_target_domains()
        attempt_limit = max(1, int(max_attempts or self.config.max_keywords_per_profile or 1))
        log(
          f"Target pair for this profile: '{keyword}' → {target_domain} "
          f"(max attempts: {attempt_limit})"
        )

        attempt = 0
        tried_domains: set[str] = {target_domain}
        pending_fresh_search = False
        while attempt < attempt_limit:
          attempt += 1
          if stopped():
            return "stopped"

          self._set_network_phase(PagePhase.GOOGLE_SERP, keyword, page)
          self._session_target_domain = target_domain
          self._apply_session_target_hosts(network, target_domain)
          self._last_target_open_failed = False

          keyword_grace_deadline = time.time() + 120.0 if attempt == 1 else 0.0
          transient_retries = 0
          while True:
            try:
              if work_ref is not None and not work_ref["page"].is_closed():
                page = work_ref["page"]
              page = self._sync_work_page(page, profile, log)
              guard, page = self._guard_captcha(
                page, stop_event, set_status, on_ui_status, profile, log,
                context=f"before-target-attempt-{attempt}",
              )
              if guard == "stopped":
                return "stopped"
              if guard in ("blocked", "error"):
                set_status(ProfileStatus.ERROR)
                return guard

              page = self._sync_work_page(page, profile, log)
              if not self._is_on_google_serp(page):
                log(
                  f"[Search] Recovering Google SERP before attempt {attempt}/{attempt_limit}: "
                  f"'{keyword}' → {target_domain}"
                )
                page = self._recover_google_serp_before_search(
                  page, keyword, profile, on_failure,
                )
              elif self._is_mobile_profile(profile) and not self._is_google_search_box_visible(page):
                log(
                  f"[Search] Expanding mobile search box before attempt {attempt}/{attempt_limit}: "
                  f"'{keyword}'"
                )
                page = self._ensure_google_search_box_ready(
                  page, profile, log, timeout_seconds=12.0,
                )
              on_keyword_serp = self._is_on_serp_for_keyword(page, keyword)
              on_any_serp = self._is_on_google_serp(page)
              keyword_search_method = "serp_box"
              announce_search_attempt = transient_retries == 0
              if pending_fresh_search:
                set_status(ProfileStatus.SEARCHING, detail=keyword)
                if announce_search_attempt:
                  log(
                    f"Searching attempt {attempt}/{attempt_limit}: '{keyword}' → {target_domain} "
                    "(fresh search after keyword change)"
                  )
                submit_search = True
                pending_fresh_search = False
              elif on_keyword_serp:
                set_status(ProfileStatus.SEARCHING, detail=keyword)
                if announce_search_attempt:
                  log(
                    f"Resuming attempt {attempt}/{attempt_limit}: '{keyword}' → {target_domain} "
                    "(SERP already open — skipping re-type)"
                  )
                submit_search = False
              elif on_any_serp:
                set_status(ProfileStatus.SEARCHING, detail=keyword)
                if announce_search_attempt:
                  log(
                    f"Searching attempt {attempt}/{attempt_limit}: '{keyword}' → {target_domain} "
                    "(SERP search box)"
                  )
                submit_search = True
              elif self._can_use_google_search_box(page):
                set_status(ProfileStatus.SEARCHING, detail=keyword)
                box_kind = (
                  "Google entry search box"
                  if self._is_on_google_home_or_ntp(page)
                  else "search box"
                )
                if announce_search_attempt:
                  log(
                    f"Searching attempt {attempt}/{attempt_limit}: '{keyword}' → {target_domain} "
                    f"({box_kind})"
                  )
                submit_search = True
                keyword_search_method = "serp_box"
              else:
                set_status(ProfileStatus.SEARCHING, detail=keyword)
                if announce_search_attempt:
                  log(
                    f"Searching attempt {attempt}/{attempt_limit}: '{keyword}' → {target_domain} "
                    "(Google search box — recovering entry)"
                  )
                keyword_search_method = "serp_box"
                submit_search = True
              clicks = self._scan_keyword_serp(
                page,
                profile,
                keyword,
                stop_event,
                stopped,
                set_status,
                on_ui_status,
                on_failure,
                log,
                submit_search=submit_search,
                search_method=keyword_search_method if submit_search else None,
                work_ref=work_ref,
                network=network,
                handle_response=handle_response,
                on_target_click=on_target_click,
                end_session_after_dwell=True,
              )
              if stopped():
                return "stopped"

              if clicks > 0:
                report_traffic(force=True)
                return "success"

              if (
                not self._last_target_open_failed
                and not self._last_search_exhaustion_eligible
                and keyword_grace_deadline > 0
                and time.time() < keyword_grace_deadline
                and transient_retries < 1
              ):
                transient_retries += 1
                remaining = max(0, int(keyword_grace_deadline - time.time()))
                log(
                  f"[Retry] '{keyword}' ended before full SERP scan "
                  f"(transient). Retrying for up to {remaining}s."
                )
                time.sleep(2.0)
                continue

              if self._last_target_open_failed:
                break

              log(
                f"No target click for '{keyword}' → {target_domain} "
                f"(attempt {attempt}/{attempt_limit})"
              )
              if on_keyword_exhausted and (
                self._last_search_exhaustion_eligible or self._mobile_serp_end_reached
              ):
                on_keyword_exhausted(keyword)
              elif self._last_search_exhausted:
                log(
                  f"[Search] '{keyword}' ended early before all SERP pages were checked."
                )
              break
            except Exception as exc:
              page = self._sync_work_page(page, profile, log)
              guard, page = self._guard_captcha(
                page, stop_event, set_status, on_ui_status, profile, log,
                context=f"retry-target-attempt-{attempt}",
              )
              if guard == "stopped":
                return "stopped"
              if guard in ("blocked", "error"):
                set_status(ProfileStatus.ERROR)
                return guard
              if self._should_retry_connection_until_deadline(exc, keyword_grace_deadline, stopped):
                remaining = max(0, int(keyword_grace_deadline - time.time()))
                log(
                  f"[Retry] Connection unstable on attempt {attempt} ('{keyword}'). "
                  f"Retrying for up to {remaining}s: {exc}"
                )
                time.sleep(2.0)
                continue
              raise

          if attempt >= attempt_limit:
            break
          next_domain = PairRotationStore.pick_next_untried_domain(all_domains, tried_domains)
          if next_domain:
            target_domain = next_domain
            tried_domains.add(target_domain)
            log(f"[Retry] Next attempt uses target domain {target_domain}")
          else:
            keyword = PairRotationStore.pick_alternate_keyword(all_keywords, keyword)
            if all_domains:
              target_domain = all_domains[0]
            tried_domains = {target_domain}
            pending_fresh_search = True
            log(
              f"[Retry] All sites tried for previous keyword — "
              f"next attempt uses keyword '{keyword}'"
            )

        set_status(ProfileStatus.IDLE)
        report_traffic(force=True)
        if self._last_target_open_failed:
          return "failed"
        return "not_found"
      except Exception as exc:
        if self._is_browser_closed_error(exc):
          log(f"Browser closed during session; treating as stopped. ({type(exc).__name__}: {exc})")
          return "stopped"
        log(f"Session error: {exc}")
        self._report_failure(page, profile, "run_session", exc, on_failure)
        set_status(ProfileStatus.ERROR, detail=short_error_detail(exc, "run_session"))
        error_kind = classify_network_error(exc)
        if error_kind in ("tunnel", "dns"):
          log(f"[Network] {error_kind} error — profile will retry with proxy rotation policy")
          return "tunnel_error"
        if error_kind == "timeout":
          log("[Network] Timeout error after retries")
        return "error"
      finally:
        report_traffic(force=True)
        self._session_page_binder = None
        self._session_wire_meter = None
        self._cleanup_session(browser, log, network, on_session_cleanup, wire_meter=wire_meter)

  def _serp_delay_bounds(self) -> tuple[float, float]:
    return (0.03, 0.09)

  def _search_type_delay_bounds(self) -> tuple[float, float]:
    """Faster typing for target keyword searches (warmup keeps action_delay)."""
    lo = max(0.03, float(self.config.action_delay_min) * 0.35)
    hi = max(lo + 0.02, float(self.config.action_delay_max) * 0.5)
    return (lo, hi)

  @staticmethod
  def _url_on_serp_or_sorry(url: str) -> bool:
    lowered = (url or "").lower()
    return "/search" in lowered or "/sorry" in lowered

  @staticmethod
  def _url_is_google_serp(url: str) -> bool:
    lowered = (url or "").lower()
    if "/sorry" in lowered:
      return False
    return "/search" in lowered

  @staticmethod
  def _url_is_google_special_search_tab(url: str) -> bool:
    """True for Images/Video/Maps/etc. — not the web SERP used for keyword entry."""
    lowered = (url or "").lower()
    if "google." not in lowered:
      return False
    if "/maps" in lowered or "maps.google." in lowered:
      return True
    try:
      qs = parse_qs(urlparse(lowered).query)
      tbm = (qs.get("tbm", [""])[0] or "").strip().lower()
      if tbm:
        return True
    except Exception:
      pass
    blocked_paths = (
      "/imgres",
      "/imgres?",
      "/preferences",
      "/advanced_search",
    )
    return any(token in lowered for token in blocked_paths)

  def _is_on_google_web_search_context(self, page: Page) -> bool:
    """Home/NTP or plain web SERP — safe to type the target keyword."""
    try:
      if page.is_closed():
        return False
      url = page.url or ""
      if self._is_on_google_home_or_ntp(page):
        return True
      if not self._is_on_google_serp(page):
        return False
      return not self._url_is_google_special_search_tab(url)
    except Exception:
      return False

  def _ensure_google_web_search_context(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
    *,
    timeout_seconds: float = 20.0,
  ) -> Page:
    """Leave Images/Video/Maps tabs and land on Google home or web SERP."""
    if self._is_on_google_web_search_context(page):
      return page
    current = (page.url or "")[:120]
    log(
      f"[Search] Non-web Google context detected ({current}) — "
      "returning to Google main for keyword entry"
    )
    try:
      page.goto(
        self.GOOGLE_ENTRY_URL,
        wait_until="commit" if self._is_mobile_profile(profile) else "domcontentloaded",
        timeout=self.GOTO_TIMEOUT_MOBILE_MS if self._is_mobile_profile(profile)
        else self.GOTO_TIMEOUT_MS,
      )
    except Exception as exc:
      log(f"[Search] Google main return warning: {exc}")
      return page
    return self._prepare_google_search_page(
      page, profile, log, timeout_seconds=timeout_seconds,
    )

  def _serp_pause(self) -> None:
    lo, hi = self._serp_delay_bounds()
    random_delay(lo, hi)

  def _serp_micro_scroll(self, page: Page, profile: ProfileSpec, *, times: int = 1) -> None:
    lo, hi = self._serp_delay_bounds()
    micro_scroll(
      page,
      times=times,
      delay_lo=lo,
      delay_hi=hi,
      mobile=self._is_mobile_profile(profile),
    )

  def _fetch_public_ip(self, page: Page) -> str:
    """Resolve egress IP via in-tab fetch (AdsPower proxy), without full navigation."""
    endpoints = (
      "https://api.ipify.org?format=text",
      "https://checkip.amazonaws.com/",
      "https://ipv4.icanhazip.com/",
    )
    for endpoint in endpoints:
      if page.is_closed():
        return ""
      ip = self._read_ip_from_browser_tab(page, endpoint)
      if ip:
        return ip
    return ""

  def _read_ip_from_browser_tab(self, page: Page, url: str) -> str:
    if page.is_closed():
      return ""
    try:
      ip = page.evaluate(
        """async (endpoint) => {
          const response = await fetch(endpoint, { cache: 'no-store', credentials: 'omit' });
          if (!response.ok) return '';
          const text = (await response.text()).trim();
          return text.split(/\\s+/)[0] || '';
        }""",
        url,
      )
      ip = (ip or "").strip()
      if ip and self._looks_like_ip(ip):
        return ip
    except Exception:
      pass
    return ""

  def _capture_session_ip(
    self,
    page: Page,
    log: Callable[[str], None],
    *,
    attempts: int = 3,
    max_wait_seconds: Optional[float] = None,
    stopped: Optional[Callable[[], bool]] = None,
  ) -> str:
    """Capture egress IP via in-tab fetch; optional deadline for session-start wait."""
    if max_wait_seconds is not None and max_wait_seconds > 0:
      deadline = time.monotonic() + float(max_wait_seconds)
      attempt = 0
      retry_interval = 1.0
      while True:
        if stopped and stopped():
          return ""
        if page.is_closed():
          log("[IP] Work tab closed during session IP capture")
          return ""
        attempt += 1
        ip = self._fetch_public_ip(page)
        if ip:
          return ip
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          log(
            f"[IP] Session IP capture timed out after "
            f"{max_wait_seconds:.0f}s ({attempt} attempt(s))"
          )
          return ""
        log(f"[IP] Session IP capture attempt {attempt} failed; retrying...")
        time.sleep(min(retry_interval, remaining))
      return ""

    max_attempts = max(1, int(attempts))
    for attempt in range(1, max_attempts + 1):
      if stopped and stopped():
        return ""
      if page.is_closed():
        log("[IP] Work tab closed during session IP capture")
        return ""
      ip = self._fetch_public_ip(page)
      if ip:
        return ip
      if attempt < max_attempts:
        log(f"[IP] Session IP capture attempt {attempt}/{max_attempts} failed; retrying...")
        time.sleep(2.0)
    return ""

  @staticmethod
  def _looks_like_ip(value: str) -> bool:
    raw = (value or "").strip()
    return bool(re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", raw))

  @staticmethod
  def _is_tunnel_connection_error(exc: BaseException) -> bool:
    return SerpBot._is_proxy_connection_error(exc)

  @staticmethod
  def _is_proxy_connection_error(exc: BaseException) -> bool:
    text = str(exc or "").upper()
    return (
      "ERR_TUNNEL_CONNECTION_FAILED" in text
      or "ERR_PROXY_AUTH_REQUESTED" in text
      or "ERR_PROXY_CONNECTION_FAILED" in text
      or "ERR_INVALID_AUTH_CREDENTIALS" in text
    )

  @staticmethod
  def _is_browser_closed_error(exc: BaseException) -> bool:
    text = str(exc or "").lower()
    return (
      "target page, context or browser has been closed" in text
      or type(exc).__name__ in ("TargetClosedError", "BrowserClosedError")
    )

  def _safe_page_wait(self, page: Page, wait_ms: int) -> bool:
    """Wait on page; return False if the browser/tab was closed."""
    if page.is_closed():
      return False
    try:
      page.wait_for_timeout(wait_ms)
      return True
    except Exception as exc:
      if self._is_browser_closed_error(exc):
        return False
      raise

  @staticmethod
  def _is_connection_error(exc: BaseException) -> bool:
    text = str(exc or "").upper()
    return (
      SerpBot._is_proxy_connection_error(exc)
      or "ERR_ABORTED" in text
      or "ERR_CONNECTION_RESET" in text
      or "ERR_CONNECTION_CLOSED" in text
      or "ERR_CONNECTION_TIMED_OUT" in text
      or "ERR_NAME_NOT_RESOLVED" in text
      or "TARGET PAGE, CONTEXT OR BROWSER HAS BEEN CLOSED" in text
      or "FRAME WAS DETACHED" in text
      or "TARGETCLOSEDERROR" in text
      or "EXECUTION CONTEXT WAS DESTROYED" in text
    )

  def _navigation_goto(
    self,
    page: Page,
    url: str,
    profile: ProfileSpec,
    log: Callable[[str], None],
    *,
    context: str = "navigation",
    **goto_kwargs,
  ) -> Page:
    mobile = self._is_mobile_profile(profile)
    timeout = int(goto_kwargs.pop("timeout", self.GOTO_TIMEOUT_MS))
    if mobile and timeout >= self.GOTO_TIMEOUT_MS:
      timeout = self.GOTO_TIMEOUT_MOBILE_MS
    wait_until = goto_kwargs.pop("wait_until", None)
    if wait_until is None:
      wait_until = "commit" if mobile else "domcontentloaded"
    page.goto(url, wait_until=wait_until, timeout=timeout, **goto_kwargs)
    return page

  def _should_retry_connection_until_deadline(
    self,
    exc: BaseException,
    deadline: float,
    stopped: Callable[[], bool],
  ) -> bool:
    if stopped():
      return False
    if deadline <= 0:
      return False
    if time.time() >= deadline:
      return False
    return self._is_connection_error(exc)

  @staticmethod
  def _close_transient_extra_tabs(context, keep_pages: list[Page]) -> int:
    keep = {tab for tab in keep_pages if tab is not None and not tab.is_closed()}
    closed = 0
    for tab in list(context.pages):
      if tab in keep:
        continue
      try:
        if tab.is_closed():
          continue
        if not SerpBot._is_transient_browser_tab_url(tab.url or ""):
          continue
        tab.close()
        closed += 1
      except Exception:
        pass
    return closed

  def _wait_for_target_tab_ready(
    self,
    target_page: Page,
    max_wait_seconds: float,
  ) -> bool:
    deadline = time.time() + max(5.0, float(max_wait_seconds))
    while time.time() < deadline:
      if target_page.is_closed():
        return False
      try:
        host = self._normalize_domain(target_page.url or "")
        if self._host_matches_any_target(host):
          try:
            target_page.bring_to_front()
          except Exception:
            pass
          return True
      except Exception:
        pass
      time.sleep(0.4)
    return False

  def _open_target_tab_direct(
    self,
    page: Page,
    href: str,
    profile: ProfileSpec,
    max_wait_seconds: float = 45.0,
    *,
    reuse_tab: Optional[Page] = None,
    keyword: str = "",
    stopped: Optional[Callable[[], bool]] = None,
  ) -> Optional[Page]:
    resolved = self._resolve_result_href(href) or (href or "").strip()
    if not resolved:
      return None
    if resolved.startswith("/url?"):
      resolved = f"https://www.google.co.kr{resolved}"
    if not resolved.startswith("http"):
      return None
    context = page.context
    target_page = reuse_tab
    opening_new_tab = target_page is None or target_page.is_closed()
    if opening_new_tab:
      self.logger(f"[Target] Direct new-tab navigation: {resolved[:120]}")
      try:
        target_page = context.new_page()
      except Exception as exc:
        self.logger(f"[Target] Could not open target tab: {exc}")
        return None
    else:
      self.logger(f"[Target] Retrying target navigation on same tab: {resolved[:120]}")
    try:
      target_page.goto(
        resolved,
        wait_until="domcontentloaded",
        timeout=int(max(15000, max_wait_seconds * 1000)),
      )
    except Exception as exc:
      self.logger(f"[Target] Direct target navigation warning: {exc}")
    if self._wait_for_target_tab_ready(target_page, max_wait_seconds):
      return target_page
    if keyword and self._target_click_navigation_started(target_page):
      ok, target_page = self._retry_target_load_after_click(
        target_page,
        resolved,
        profile,
        keyword,
        stopped=stopped,
      )
      if ok:
        return target_page
    if opening_new_tab and target_page and not target_page.is_closed():
      try:
        if self._is_transient_browser_tab_url(target_page.url or ""):
          target_page.close()
      except Exception:
        pass
    return None

  @staticmethod
  def _is_transient_browser_tab_url(url: str) -> bool:
    lowered = (url or "").strip().lower()
    return (
      lowered in ("", "about:blank")
      or lowered.startswith("chrome-error://")
      or "chromewebdata" in lowered
    )

  def _begin_google_connect_watch(self) -> None:
    if self._google_connect_deadline is None:
      self._google_connect_deadline = (
        time.time() + self.GOOGLE_CONNECT_DEADLINE_SECONDS
      )

  def _is_google_connect_ready(self, page: Page) -> bool:
    try:
      if page.is_closed():
        return False
      url = page.url or ""
    except Exception:
      return False
    if self._is_transient_browser_tab_url(url):
      return False
    if self._is_google_search_box_visible(page):
      return True
    if self._is_on_google_serp(page):
      return True
    if self._is_google_entry_tab_url(url) or self._is_on_google_home_or_ntp(page):
      return True
    return False

  def _check_google_proxy_connect(
    self,
    page: Page,
    log: Callable[[str], None],
  ) -> tuple[Page, Optional[str]]:
    if self._is_google_connect_ready(page):
      return page, None
    deadline = self._google_connect_deadline
    if deadline is None or time.time() < deadline:
      return page, None
    current = ""
    try:
      current = (page.url or "").strip()
    except Exception:
      pass
    detail = f" ({current[:90]})" if current else ""
    log(
      f"Failed connect proxy (Google not reachable within "
      f"{int(self.GOOGLE_CONNECT_DEADLINE_SECONDS)}s{detail})"
    )
    return page, "proxy_connect_failed"

  def _target_page_load_confirmed(self, page: Page) -> bool:
    try:
      host = self._normalize_domain(page.url or "")
      if not self._host_matches_any_target(host):
        return False
      try:
        page.wait_for_load_state("domcontentloaded", timeout=4000)
      except Exception:
        pass
      return True
    except Exception:
      return False

  @staticmethod
  def _target_click_navigation_started(page: Page) -> bool:
    try:
      url = (page.url or "").strip().lower()
    except Exception:
      return False
    if SerpBot._is_transient_browser_tab_url(url):
      return True
    if url and "google." not in url:
      return True
    return False

  def _retry_target_load_after_click(
    self,
    page: Page,
    href: str,
    profile: ProfileSpec,
    keyword: str,
    *,
    stopped: Optional[Callable[[], bool]] = None,
  ) -> tuple[bool, Page]:
    """After SERP click, retry goto/reload like a human (F5) before giving up."""
    stopped_fn = stopped or (lambda: False)
    resolved = self._resolve_result_href(href) or (href or "").strip()
    if resolved.startswith("/url?"):
      resolved = f"https://www.google.co.kr{resolved}"
    if not resolved.startswith("http"):
      return False, page

    if self._target_page_load_confirmed(page):
      return True, page
    if not self._target_click_navigation_started(page):
      return False, page

    self.logger(
      f"[Target] Target click started but site not loaded for '{keyword}' "
      f"(url={(page.url or '')[:100]}) — retrying up to "
      f"{self._TARGET_LOAD_RETRY_ATTEMPTS} time(s)"
    )

    mobile = self._is_mobile_profile(profile)

    for attempt in range(1, self._TARGET_LOAD_RETRY_ATTEMPTS + 1):
      if stopped_fn():
        return False, page
      if self._target_page_load_confirmed(page):
        return True, page

      if not mobile:
        try:
          page.bring_to_front()
        except Exception:
          pass

      use_goto = (
        attempt == 1
        or self._is_transient_browser_tab_url(page.url or "")
      )
      action = "goto" if use_goto else "reload"
      self.logger(
        f"[Target] Load retry {attempt}/{self._TARGET_LOAD_RETRY_ATTEMPTS} "
        f"for '{keyword}' via {action}"
      )
      try:
        if use_goto:
          page = self._safe_goto(
            page,
            resolved,
            profile,
            self.logger,
            wait_until="domcontentloaded",
            timeout=30000,
          )
          page = self._bind_session_page(page)
        else:
          page.reload(wait_until="domcontentloaded", timeout=30000)
          page = self._bind_session_page(page)
      except Exception as exc:
        self.logger(
          f"[Target] Load retry {attempt}/{self._TARGET_LOAD_RETRY_ATTEMPTS} "
          f"warning: {exc}"
        )

      wait_seconds = random.uniform(*self._TARGET_LOAD_RETRY_WAIT_SECONDS)
      deadline = time.time() + wait_seconds
      while time.time() < deadline:
        if stopped_fn():
          return False, page
        if self._target_page_load_confirmed(page):
          self.logger(
            f"[Target] Target site loaded after retry {attempt}/"
            f"{self._TARGET_LOAD_RETRY_ATTEMPTS} for '{keyword}'"
          )
          return True, page
        time.sleep(0.5)

    return False, page

  def _desktop_mouse_ctrl_click_link(self, page: Page, link) -> None:
    box = link.bounding_box()
    if not box or box.get("width", 0) < 2 or box.get("height", 0) < 2:
      raise RuntimeError("SERP link has no clickable bounding box")
    margin_x = max(4.0, box["width"] * 0.15)
    margin_y = max(4.0, box["height"] * 0.15)
    x = random.uniform(box["x"] + margin_x, box["x"] + box["width"] - margin_x)
    y = random.uniform(box["y"] + margin_y, box["y"] + box["height"] - margin_y)
    page.keyboard.down("Control")
    try:
      page.mouse.move(x, y)
      page.wait_for_timeout(random.randint(40, 120))
      page.mouse.click(x, y, delay=random.randint(35, 95))
    finally:
      page.keyboard.up("Control")

  def _wait_for_ctrl_click_target_tab(
    self,
    target_page: Page,
    max_wait_seconds: float,
    stopped: Callable[[], bool],
  ) -> bool:
    deadline = time.time() + max(8.0, float(max_wait_seconds))
    while time.time() < deadline:
      if stopped():
        return False
      if target_page.is_closed():
        return False
      try:
        tab_url = target_page.url or ""
        if self._is_transient_browser_tab_url(tab_url):
          time.sleep(0.35)
          continue
        host = self._normalize_domain(tab_url)
        if self._host_matches_any_target(host):
          try:
            target_page.bring_to_front()
          except Exception:
            pass
          return True
      except Exception:
        pass
      time.sleep(0.35)
    return False

  def _find_new_target_tab_after_serp_click(
    self,
    context,
    pages_before: list[Page],
    serp_page: Page,
    max_wait_seconds: float,
    stopped: Callable[[], bool],
  ) -> Optional[Page]:
    before_ids = {id(tab) for tab in pages_before if tab is not None and not tab.is_closed()}
    deadline = time.time() + max(6.0, float(max_wait_seconds) * 0.6)
    while time.time() < deadline:
      if stopped():
        return None
      for tab in list(context.pages):
        if tab.is_closed() or id(tab) in before_ids:
          continue
        if tab is serp_page:
          continue
        try:
          tab_url = tab.url or ""
          if self._host_matches_any_target(self._normalize_domain(tab_url)):
            return tab
          if self._is_transient_browser_tab_url(tab_url):
            return tab
        except Exception:
          pass
      time.sleep(0.35)
    return None

  def _desktop_run_ctrl_click_strategy(
    self,
    strategy_name: str,
    serp_page: Page,
    link,
    delay_lo: float,
    delay_hi: float,
  ) -> None:
    if strategy_name == "human_ctrl_click":
      human_click(
        link,
        delay_lo,
        delay_hi,
        page=serp_page,
        mobile=False,
        modifiers=["Control"],
      )
      return
    if strategy_name == "locator_ctrl_click":
      random_delay(delay_lo, delay_hi)
      link.scroll_into_view_if_needed(timeout=4000)
      link.click(timeout=8000, modifiers=["Control"])
      random_delay(delay_lo, delay_hi)
      return
    if strategy_name == "mouse_ctrl_click":
      random_delay(delay_lo, delay_hi)
      self._desktop_mouse_ctrl_click_link(serp_page, link)
      random_delay(delay_lo, delay_hi)
      return
    raise ValueError(f"Unknown Ctrl+click strategy: {strategy_name}")

  def _desktop_open_target_via_serp_ctrl_click(
    self,
    serp_page: Page,
    link,
    keyword: str,
    max_wait_seconds: float,
    stopped: Callable[[], bool],
    *,
    profile: ProfileSpec,
    resolved_href: str,
  ) -> Optional[Page]:
    delay_lo = self.config.action_delay_min
    delay_hi = self.config.action_delay_max
    max_attempts = 3
    expect_timeout_ms = 22000
    strategy_names = ("human_ctrl_click", "locator_ctrl_click", "mouse_ctrl_click")

    for attempt in range(1, max_attempts + 1):
      if stopped():
        return None
      try:
        serp_page.bring_to_front()
      except Exception:
        pass
      try:
        link.scroll_into_view_if_needed(timeout=5000)
        serp_page.wait_for_timeout(random.randint(220, 520))
      except Exception:
        pass

      pages_before = list(serp_page.context.pages)
      for strategy_name in strategy_names:
        if stopped():
          return None
        target_page: Optional[Page] = None
        self.logger(
          f"[Target] Ctrl+click SERP link for '{keyword}' "
          f"(attempt {attempt}/{max_attempts}, {strategy_name})"
        )
        try:
          with serp_page.context.expect_page(timeout=expect_timeout_ms) as new_page_info:
            self._desktop_run_ctrl_click_strategy(
              strategy_name, serp_page, link, delay_lo, delay_hi,
            )
          target_page = new_page_info.value
        except Exception as exc:
          self.logger(
            f"[Target] Ctrl+click {strategy_name} did not open a new tab: {exc}"
          )
          target_page = self._find_new_target_tab_after_serp_click(
            serp_page.context,
            pages_before,
            serp_page,
            max_wait_seconds,
            stopped,
          )

        if target_page is None or target_page.is_closed():
          continue

        try:
          target_page.wait_for_load_state("domcontentloaded", timeout=14000)
        except Exception:
          pass

        if self._wait_for_ctrl_click_target_tab(
          target_page, max_wait_seconds, stopped,
        ):
          return target_page

        ok, target_page = self._retry_target_load_after_click(
          target_page,
          resolved_href,
          profile,
          keyword,
          stopped=stopped,
        )
        if ok:
          return target_page

        self.logger(
          f"[Target] Ctrl+click tab did not reach target ({strategy_name}) "
          f"— closing and retrying"
        )
        try:
          if not target_page.is_closed():
            target_page.close()
        except Exception:
          pass

    return None

  def _stabilize_serp_link_before_tap(self, page: Page, link, *, mobile: bool) -> None:
    """Re-center a SERP result link before tap with small human-like motion."""
    try:
      link.scroll_into_view_if_needed(timeout=5000)
    except Exception:
      pass
    page.wait_for_timeout(random.randint(220, 480))
    if mobile:
      scroll_page(page, random.randint(-90, 90), mobile=True)
      page.wait_for_timeout(random.randint(160, 340))
      try:
        link.scroll_into_view_if_needed(timeout=4000)
      except Exception:
        pass
      page.wait_for_timeout(random.randint(180, 360))

  def _wait_serp_retap_interval(
    self,
    stopped: Callable[[], bool],
    *,
    keyword: str,
    attempt: int,
    max_attempts: int,
  ) -> bool:
    if attempt >= max_attempts:
      return not stopped()
    wait_seconds = random.uniform(*self._TARGET_SERP_RETAP_WAIT_SECONDS)
    self.logger(
      f"[Target] Waiting {wait_seconds:.0f}s before SERP retap "
      f"{attempt + 1}/{max_attempts} for '{keyword}'"
    )
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
      if stopped():
        return False
      time.sleep(0.35)
    return True

  def _resolve_serp_link_for_click(
    self,
    page: Page,
    href: str,
    expected_domain: str,
    *,
    mobile: bool,
  ):
    link = self._find_result_link_for_href(page, href, mobile=mobile)
    if not link and mobile:
      link = self._find_mobile_serp_link_relaxed(page, href)
    if not link:
      return None
    try:
      raw_href = link.get_attribute("href") or href
      resolved = self._resolve_result_href(raw_href)
      if not self._href_matches_target(resolved or href, expected_domain):
        self.logger(
          f"[Target] Refusing SERP click — host mismatch "
          f"({self._normalize_domain(resolved or href)} vs "
          f"{self._normalize_domain(expected_domain)})"
        )
        return None
    except Exception:
      pass
    return link

  def _open_target_from_serp_click(
    self,
    page: Page,
    href: str,
    keyword: str,
    profile: ProfileSpec,
    stop_event: Optional[threading.Event],
    stopped: Callable[[], bool],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    max_wait_seconds: float = 45.0,
    *,
    work_ref: Optional[dict] = None,
    matched_domain: str = "",
  ) -> tuple[bool, Page, Optional[Page]]:
    expected_domain = matched_domain or self.config.primary_target_domain
    mobile = self._is_mobile_profile(profile)
    serp_page: Optional[Page] = page if not mobile else None
    if work_ref is not None:
      work_ref["allow_target_tab_until"] = time.time() + max(70.0, float(max_wait_seconds) + 25.0)

    guard, page = self._guard_captcha(
      page, stop_event, set_status, on_ui_status, profile, self.logger,
    )
    if guard == "stopped":
      return False, page, serp_page
    if guard in ("error", "blocked"):
      return False, page, serp_page

    resolved_href = self._resolve_result_href(href) or href

    if not mobile:
      serp_anchor_url = ""
      try:
        serp_anchor_url = (page.url or "").strip()
      except Exception:
        pass
      for attempt in range(1, self._TARGET_SERP_RETAP_ATTEMPTS + 1):
        if stopped():
          return False, page, serp_page
        link = self._resolve_serp_link_for_click(
          page,
          href,
          expected_domain,
          mobile=False,
        )
        if not link:
          self.logger(
            f"[Target] SERP link locator miss for '{keyword}' "
            f"on re-click attempt {attempt}/{self._TARGET_SERP_RETAP_ATTEMPTS}"
          )
        else:
          try:
            raw_href = link.get_attribute("href") or href
            resolved_href = self._resolve_result_href(raw_href) or href
          except Exception:
            pass
          self._stabilize_serp_link_before_tap(page, link, mobile=False)
          self.logger(
            f"[Target] Opening target via Ctrl+click SERP link for '{keyword}' "
            f"(attempt {attempt}/{self._TARGET_SERP_RETAP_ATTEMPTS})"
          )
          target_page = self._desktop_open_target_via_serp_ctrl_click(
            page,
            link,
            keyword,
            max_wait_seconds,
            stopped,
            profile=profile,
            resolved_href=resolved_href,
          )
          if target_page:
            self._close_transient_extra_tabs(
              page.context,
              [tab for tab in (page, target_page, serp_page) if tab is not None],
            )
            if work_ref is not None:
              work_ref["allow_target_tab_until"] = 0.0
            return True, target_page, serp_page
          self.logger(
            f"[Target] Ctrl+click attempt {attempt}/{self._TARGET_SERP_RETAP_ATTEMPTS} "
            f"missed for '{keyword}'"
          )
        if not self._wait_serp_retap_interval(
          stopped,
          keyword=keyword,
          attempt=attempt,
          max_attempts=self._TARGET_SERP_RETAP_ATTEMPTS,
        ):
          return False, page, serp_page
        if serp_anchor_url and not self._is_google_serp_url(page.url or ""):
          try:
            page.go_back(wait_until="domcontentloaded", timeout=15000)
            page = self._bind_session_page(page)
          except Exception as exc:
            self.logger(f"[Target] Desktop SERP recovery before retap warning: {exc}")

      self.logger(
        f"[Target] Ctrl+click failed for '{keyword}' "
        f"— fallback direct new-tab navigation"
      )
      target_page = self._open_target_tab_direct(
        page,
        resolved_href,
        profile,
        max_wait_seconds=max_wait_seconds,
        keyword=keyword,
        stopped=stopped,
      )
      if target_page:
        self._close_transient_extra_tabs(
          page.context,
          [tab for tab in (page, target_page, serp_page) if tab is not None],
        )
        if work_ref is not None:
          work_ref["allow_target_tab_until"] = 0.0
        return True, target_page, serp_page
      return False, page, serp_page

    serp_anchor_url = ""
    try:
      serp_anchor_url = (page.url or "").strip()
    except Exception:
      pass
    for attempt in range(1, self._TARGET_SERP_RETAP_ATTEMPTS + 1):
      if stopped():
        return False, page, serp_page
      if serp_anchor_url and not self._is_google_serp_url(page.url or ""):
        page = self._escape_mobile_target_to_serp(
          page,
          keyword,
          profile,
          None,
          self.logger,
          before_serp_url=serp_anchor_url,
        )
        page = self._bind_session_page(page)
      link = self._resolve_serp_link_for_click(
        page,
        href,
        expected_domain,
        mobile=True,
      )
      if not link:
        self.logger(
          f"[Target] SERP link locator miss for '{keyword}' "
          f"on retap attempt {attempt}/{self._TARGET_SERP_RETAP_ATTEMPTS} "
          f"({href[:100]})"
        )
      else:
        self._stabilize_serp_link_before_tap(page, link, mobile=True)
        if self._mobile_click_serp_result_link(page, link, href):
          return True, page, serp_page
        if self._target_click_navigation_started(page):
          ok, page = self._retry_target_load_after_click(
            page,
            href,
            profile,
            keyword,
            stopped=stopped,
          )
          if ok:
            return True, page, serp_page
        self.logger(
          f"[Target] Mobile SERP tap attempt {attempt}/{self._TARGET_SERP_RETAP_ATTEMPTS} "
          f"missed for '{keyword}' (url={(page.url or '')[:100]})"
        )
      if not self._wait_serp_retap_interval(
        stopped,
        keyword=keyword,
        attempt=attempt,
        max_attempts=self._TARGET_SERP_RETAP_ATTEMPTS,
      ):
        return False, page, serp_page

    self.logger(
      f"[Target] Mobile SERP tap missed for '{keyword}' after "
      f"{self._TARGET_SERP_RETAP_ATTEMPTS} on-page attempt(s) — no direct URL fallback"
    )
    return False, page, serp_page

  def _open_url_with_retry(
    self,
    page: Page,
    url: str,
    stopped: Callable[[], bool],
    profile: ProfileSpec,
    log: Callable[[str], None],
    max_wait_seconds: float = 120.0,
    purpose: str = "generic",
  ) -> tuple[bool, Page]:
    deadline = time.time() + max(1.0, float(max_wait_seconds))
    timeout_retries = 0
    max_timeout_retries = 2
    while time.time() < deadline:
      if stopped():
        return False, page
      try:
        page = self._safe_goto(
          page,
          url,
          profile,
          log,
          wait_until="domcontentloaded",
          timeout=60000,
        )
        return True, page
      except Exception as exc:
        error_kind = classify_network_error(exc)
        if error_kind == "timeout":
          timeout_retries += 1
          if timeout_retries <= max_timeout_retries:
            remaining = max(0, int(deadline - time.time()))
            log(
              f"[Retry] Timeout ({timeout_retries}/{max_timeout_retries}) "
              f"for {purpose}, {remaining}s left: {exc}"
            )
            time.sleep(2.0)
            continue
        if error_kind == "browser_crash":
          log(f"[Retry] Browser/tab closed during {purpose}; recovering work page")
          try:
            page = self._recover_work_page(page.context, profile, log)
          except Exception:
            return False, page
          time.sleep(1.5)
          continue
        if not self._is_connection_error(exc):
          raise
        remaining = max(0, int(deadline - time.time()))
        log(f"[Retry] URL open retry ({purpose}, {error_kind}, {remaining}s left): {exc}")
        try:
          context = page.context
        except Exception:
          return False, page
        page = self._recover_work_page(context, profile, log)
        pause_s = 3.5 if error_kind in ("tunnel", "dns") else 2.0
        time.sleep(pause_s)
    return False, page

  @staticmethod
  def _is_google_entry_tab_url(url: str) -> bool:
    lowered = (url or "").strip().lower()
    if lowered.startswith("chrome://new-tab-page"):
      return True
    if "consent.google." in lowered:
      return True
    if SerpBot._url_on_serp_or_sorry(lowered):
      return False
    try:
      host = (urlparse(lowered).netloc or "").lower()
      if "google." not in host:
        return False
      path = (urlparse(lowered).path or "").lower().rstrip("/") or "/"
      return path in ("/", "/webhp")
    except Exception:
      return False

  def _is_google_search_box_visible(self, page: Page) -> bool:
    try:
      if page.is_closed():
        return False
      search_box = page.locator('textarea[name="q"], input[name="q"]').first
      if search_box.count() <= 0:
        return False
      return search_box.is_visible(timeout=600)
    except Exception:
      return False

  def _reveal_mobile_google_search_box(
    self,
    page: Page,
    log: Callable[[str], None],
    profile: Optional[ProfileSpec] = None,
  ) -> bool:
    """Expand collapsed mobile Google search UI so textarea[name=q] becomes visible."""
    mobile = self._is_mobile_profile(profile) if profile is not None else self._is_mobile_profile_page(page)
    if not mobile:
      return self._is_google_search_box_visible(page)
    if self._is_google_search_box_visible(page):
      return True
    enable_mobile_touch(page)
    type_lo, type_hi = self._search_type_delay_bounds()
    search_box = page.locator('textarea[name="q"], input[name="q"]').first
    if search_box.count() > 0:
      try:
        form = search_box.locator("xpath=ancestor::form[1]")
        if form.count() > 0 and form.first.is_visible(timeout=500):
          human_touch_click(page, form.first, type_lo, type_hi)
          page.wait_for_timeout(random.randint(280, 520))
          if self._is_google_search_box_visible(page):
            log("[Search] Mobile search box expanded (form tap)")
            return True
      except Exception:
        pass
    for selector in self._MOBILE_GOOGLE_SEARCH_BAR_SELECTORS:
      locator = page.locator(selector).first
      try:
        if locator.count() <= 0 or not locator.is_visible(timeout=450):
          continue
        human_touch_click(page, locator, type_lo, type_hi)
        page.wait_for_timeout(random.randint(280, 520))
        if self._is_google_search_box_visible(page):
          log(f"[Search] Mobile search box expanded ({selector})")
          return True
      except Exception:
        continue
    try:
      if search_box.count() > 0:
        search_box.evaluate(
          """el => {
            if (!el) return;
            el.focus();
            el.click();
            el.dispatchEvent(new Event('focus', { bubbles: true }));
          }"""
        )
        page.wait_for_timeout(random.randint(250, 480))
        if self._is_google_search_box_visible(page):
          log("[Search] Mobile search box expanded (focus)")
          return True
    except Exception:
      pass
    try:
      metrics = get_viewport_touch_metrics(page)
      width = float(metrics.get("width") or 0)
      height = float(metrics.get("height") or 0)
      if width > 0 and height > 0:
        tap_x = width * random.uniform(0.42, 0.58)
        tap_y = min(height * 0.14, 130.0)
        dispatch_touch_tap(page, tap_x, tap_y, logger=self.logger, label="mobile-search-bar")
        page.wait_for_timeout(random.randint(320, 560))
        if self._is_google_search_box_visible(page):
          log("[Search] Mobile search box expanded (header tap)")
          return True
    except Exception:
      pass
    return self._is_google_search_box_visible(page)

  @staticmethod
  def _is_mobile_profile_page(page: Page) -> bool:
    try:
      metrics = page.evaluate(
        """() => ({
          width: window.innerWidth || 0,
          touch: navigator.maxTouchPoints || 0,
          ua: navigator.userAgent || '',
        })"""
      )
      if not isinstance(metrics, dict):
        return False
      width = int(metrics.get("width") or 0)
      touch = int(metrics.get("touch") or 0)
      ua = str(metrics.get("ua") or "").lower()
      return width > 0 and width < 520 and (touch > 0 or "android" in ua or "mobile" in ua)
    except Exception:
      return False

  def _reveal_desktop_google_search_box(
    self,
    page: Page,
    log: Callable[[str], None],
  ) -> bool:
    """Try to expose a hidden desktop Google search box (overlays / collapsed header)."""
    if page.is_closed():
      return False
    self._dismiss_google_serp_overlays(page, log)
    search_box = page.locator('textarea[name="q"], input[name="q"]').first
    if search_box.count() <= 0:
      return False
    try:
      search_box.scroll_into_view_if_needed(timeout=2000)
    except Exception:
      pass
    try:
      form = search_box.locator("xpath=ancestor::form[1]")
      if form.count() > 0:
        form.first.click(timeout=1500)
        page.wait_for_timeout(random.randint(200, 420))
    except Exception:
      pass
    try:
      search_box.evaluate(
        """el => {
          if (!el) return;
          el.removeAttribute('readonly');
          el.focus();
          el.click();
          el.dispatchEvent(new Event('focus', { bubbles: true }));
        }"""
      )
      page.wait_for_timeout(random.randint(180, 360))
    except Exception:
      pass
    if self._is_google_search_box_visible(page):
      log("[Search] Desktop search box revealed")
      return True
    try:
      viewport = page.viewport_size or {"width": 1280, "height": 800}
      cx = max(8, int(viewport.get("width", 1280)) // 2)
      cy = max(8, int(viewport.get("height", 800)) * 0.12)
      page.mouse.click(cx, cy)
      page.wait_for_timeout(random.randint(220, 420))
    except Exception:
      pass
    return self._is_google_search_box_visible(page)

  def _wait_for_google_search_box(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
    *,
    timeout_ms: int = 12_000,
  ):
    mobile = self._is_mobile_profile(profile)
    search_box = page.locator('textarea[name="q"], input[name="q"]').first
    deadline = time.time() + max(2.0, timeout_ms / 1000.0)
    while time.time() < deadline:
      self._dismiss_google_serp_overlays(page, log)
      if self._is_google_search_box_visible(page):
        return search_box
      if mobile:
        self._reveal_mobile_google_search_box(page, log, profile)
      else:
        self._reveal_desktop_google_search_box(page, log)
      if self._is_google_search_box_visible(page):
        return search_box
      try:
        page.wait_for_timeout(random.randint(280, 450))
      except Exception:
        time.sleep(0.35)
    if not mobile:
      self._reveal_desktop_google_search_box(page, log)
      if self._is_google_search_box_visible(page):
        return search_box
    try:
      search_box.wait_for(state="visible", timeout=max(1500, timeout_ms // 3))
      return search_box
    except Exception:
      return None

  def _ensure_google_search_box_ready(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
    *,
    timeout_seconds: float = 20.0,
  ) -> Page:
    """Prepare a typable Google search box without leaving mobile SERP."""
    page = self._sync_work_page(page, profile, log)
    page = self._ensure_google_web_search_context(
      page, profile, log, timeout_seconds=timeout_seconds,
    )
    mobile = self._is_mobile_profile(profile)
    on_serp = self._is_on_google_serp(page)
    on_entry = self._is_google_entry_tab_url(page.url or "") or self._is_on_google_home_or_ntp(page)

    self._dismiss_google_serp_overlays(page, log)
    if self._is_google_search_box_visible(page):
      return page
    if mobile:
      self._reveal_mobile_google_search_box(page, log, profile)
      if self._is_google_search_box_visible(page):
        return page
    if on_serp and mobile:
      deadline = time.time() + max(4.0, float(timeout_seconds) * 0.6)
      while time.time() < deadline:
        self._dismiss_google_serp_overlays(page, log)
        self._reveal_mobile_google_search_box(page, log, profile)
        if self._is_google_search_box_visible(page):
          return page
        page.wait_for_timeout(random.randint(320, 520))
      log("[Search] Mobile SERP search box still collapsed — staying on results page")
      return page
    if not on_entry and not on_serp:
      page = self._ensure_google_entry_page(
        page, profile, log, timeout_seconds=timeout_seconds,
      )
    elif not self._is_google_search_box_visible(page):
      page = self._ensure_google_entry_page(
        page, profile, log, timeout_seconds=timeout_seconds,
      )
    if mobile and not self._is_google_search_box_visible(page):
      self._reveal_mobile_google_search_box(page, log, profile)
    return page

  def _ensure_google_entry_page(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
    *,
    timeout_seconds: float = 25.0,
  ) -> Page:
    if page.is_closed():
      try:
        page = self._recover_work_page(page.context, profile, log)
      except Exception:
        return page

    try:
      context = page.context
    except Exception:
      context = None

    if context is not None:
      alive = self._filter_work_tabs(
        [tab for tab in list(context.pages) if not tab.is_closed()]
      )
      google_tab = None
      for tab in alive:
        if self._is_google_entry_tab_url(tab.url or ""):
          google_tab = tab
      if google_tab is not None and google_tab != page:
        log(f"[Start] Adopted Google entry tab ({(google_tab.url or '')[:100]})")
        page = self._bind_session_page(google_tab)

    mobile = self._is_mobile_profile(profile)
    goto_timeout_ms = 12000 if mobile else 15000
    deadline = time.time() + max(5.0, float(timeout_seconds))
    logged_waiting = False
    opened_google = self._is_google_entry_tab_url(page.url or "")

    while time.time() < deadline:
      try:
        page.bring_to_front()
      except Exception:
        pass
      self._dismiss_google_serp_overlays(page, log)
      if self._is_google_search_box_visible(page):
        if self._is_on_google_home_or_ntp(page):
          log("[Start] Google entry search box ready (home/NTP)")
        elif self._is_on_google_serp(page):
          log("[Start] Google SERP search box ready")
        else:
          log("[Start] Google search box ready")
        return page
      if mobile and self._is_on_google_serp(page):
        if self._reveal_mobile_google_search_box(page, log, profile):
          if self._is_google_search_box_visible(page):
            log("[Start] Google SERP search box ready (mobile expanded)")
            return page
      elif not opened_google and not self._is_google_entry_tab_url(page.url or ""):
        try:
          log(f"[Start] Opening Google entry ({self.GOOGLE_ENTRY_URL})")
          page.goto(
            self.GOOGLE_ENTRY_URL,
            wait_until="domcontentloaded",
            timeout=goto_timeout_ms,
          )
          opened_google = True
        except Exception as exc:
          if self._is_proxy_connection_error(exc):
            log(f"[Start] Google entry navigation retrying: {exc}")
            time.sleep(1.5)
          else:
            log(f"[Start] Google entry navigation warning: {exc}")
      elif mobile and self._is_google_entry_tab_url(page.url or ""):
        self._reveal_mobile_google_search_box(page, log, profile)
      if not logged_waiting:
        log("[Start] Waiting for Google New Tab / search box...")
        logged_waiting = True
      try:
        page.wait_for_timeout(400)
      except Exception:
        time.sleep(0.4)

    log("[Start] Google entry not ready within timeout — keeping current tab for warm-up")
    return page

  def _prepare_google_search_page(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
    *,
    timeout_seconds: float = 20.0,
  ) -> Page:
    return self._ensure_google_search_box_ready(
      page, profile, log, timeout_seconds=timeout_seconds,
    )

  def _warm_up_browser_proxy(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
  ) -> Page:
    if page.is_closed():
      try:
        return self._recover_work_page(page.context, profile, log)
      except Exception:
        return page
    mobile = self._is_mobile_profile(profile)
    label = "Mobile" if mobile else "Desktop"
    log(f"[Start] {label} Google.co.kr entry (SERP search box warmup)")
    try:
      page = self._prepare_google_search_page(
        page, profile, log, timeout_seconds=25.0,
      )
      current = (page.url or "").strip()
      if self._is_google_search_box_visible(page):
        log("[Start] Google search box ready")
      else:
        log(f"[Start] Google entry pending ({current[:90] or 'unknown'})")
    except Exception as exc:
      log(f"[Start] Google entry warmup warning: {exc}")
    return page

  @staticmethod
  def _response_size_bytes(response) -> int:
    try:
      headers = response.headers or {}
      raw = headers.get("content-length") or headers.get("Content-Length")
      if not raw:
        return 0
      return max(0, int(str(raw).strip()))
    except Exception:
      return 0


  @staticmethod
  def _is_captcha_url(url: str) -> bool:
    return CaptchaSolver._url_indicates_captcha(url)

  @staticmethod
  def _url_looks_like_captcha(page: Page) -> bool:
    try:
      return SerpBot._is_captcha_url(page.url or "")
    except Exception:
      return False

  @staticmethod
  def _is_devtools_url(url: str) -> bool:
    return (url or "").strip().lower().startswith("devtools://")

  @staticmethod
  def _is_blank_tab_url(url: str) -> bool:
    lowered = (url or "").strip().lower()
    if SerpBot._is_devtools_url(lowered):
      return False
    return not lowered or lowered == "about:blank"

  @staticmethod
  def _filter_work_tabs(tabs: list[Page]) -> list[Page]:
    return [
      tab for tab in tabs
      if not tab.is_closed() and not SerpBot._is_devtools_url(tab.url or "")
    ]

  @staticmethod
  def _pick_work_page(tabs: list[Page], profile: ProfileSpec) -> Page:
    tabs = SerpBot._filter_work_tabs(tabs)
    if not tabs:
      raise ValueError("no content tabs available (devtools excluded)")
    google_tabs = [tab for tab in tabs if SerpBot._is_google_entry_tab_url(tab.url or "")]
    if google_tabs:
      return google_tabs[-1] if SerpBot._is_mobile_profile(profile) else google_tabs[0]
    blank_tabs = [tab for tab in tabs if SerpBot._is_blank_tab_url(tab.url or "")]
    if blank_tabs:
      return blank_tabs[-1] if SerpBot._is_mobile_profile(profile) else blank_tabs[0]
    if not SerpBot._is_mobile_profile(profile):
      return tabs[0]
    for tab in reversed(tabs):
      if SerpBot._is_blank_tab_url(tab.url or ""):
        continue
      return tab
    return tabs[-1]

  def _safe_goto(
    self,
    page: Page,
    url: str,
    profile: ProfileSpec,
    log: Callable[[str], None],
    **goto_kwargs,
  ) -> Page:
    context = None
    try:
      context = page.context
    except Exception:
      pass
    page = self._sync_work_page(page, profile, log)
    if page.is_closed():
      if context is None:
        raise RuntimeError("work page closed and browser context unavailable")
      page = self._recover_work_page(context, profile, log)
      log(f"[Tab] Recovered closed work page before navigation to {url[:80]}")
    return self._navigation_goto(page, url, profile, log, **goto_kwargs)

  def _recover_work_page(
    self,
    context,
    profile: ProfileSpec,
    log: Callable[[str], None],
    preferred: Optional[Page] = None,
  ) -> Page:
    if preferred is not None and not preferred.is_closed():
      return preferred
    alive = self._filter_work_tabs(
      [tab for tab in list(context.pages) if not tab.is_closed()]
    )
    wait_rounds = 6 if self._is_mobile_profile(profile) else 4
    if not alive:
      for _ in range(wait_rounds):
        time.sleep(0.5)
        alive = self._filter_work_tabs(
      [tab for tab in list(context.pages) if not tab.is_closed()]
    )
        if alive:
          break
    if not alive:
      try:
        page = context.new_page()
        label = "mobile" if self._is_mobile_profile(profile) else "desktop"
        log(f"[Tab] No open tabs — opened new work tab ({label})")
        return self._bind_session_page(page)
      except Exception as exc:
        if self._is_mobile_profile(profile):
          raise RuntimeError(f"no open tabs available on mobile profile ({exc})") from exc
        raise
    page = self._pick_work_page(alive, profile)
    log(f"[Tab] Recovered work tab ({len(alive)} open, url={(page.url or '')[:80]})")
    return self._bind_session_page(page)

  def _sync_work_page(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
  ) -> Page:
    try:
      context = page.context
    except Exception:
      return page
    alive = self._filter_work_tabs(
      [tab for tab in list(context.pages) if not tab.is_closed()]
    )
    for candidate in alive:
      try:
        if self.captcha.requires_captcha_clear(candidate):
          if candidate != page:
            log(f"[Captcha] Active on browser tab: {(candidate.url or '')[:100]}")
          try:
            candidate.bring_to_front()
          except Exception:
            pass
          return self._bind_session_page(candidate)
      except Exception:
        continue
    if page is not None and not page.is_closed():
      return page
    return self._bind_session_page(self._recover_work_page(context, profile, log))

  @staticmethod
  def _open_work_page(browser: Browser, profile: ProfileSpec, log: Callable[[str], None]) -> Page:
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    pages = [tab for tab in list(context.pages) if not tab.is_closed()]
    mobile = SerpBot._is_mobile_profile(profile)
    devtools_closed = 0
    for tab in list(pages):
      if SerpBot._is_devtools_url(tab.url or ""):
        try:
          tab.close()
          devtools_closed += 1
        except Exception:
          pass
    if devtools_closed:
      log(f"Closed {devtools_closed} DevTools tab(s) — using content tab only")
    pages = SerpBot._filter_work_tabs([tab for tab in list(context.pages) if not tab.is_closed()])
    if pages:
      page = SerpBot._pick_work_page(pages, profile)
      closed_extras = 0
      if mobile:
        log(
          f"Reusing AdsPower tab ({profile.os_browser_label}: active tab, "
          f"{len(pages)} open, leaving sibling tabs intact)"
        )
      else:
        for extra in pages:
          try:
            if not extra.is_closed() and extra != page:
              extra.close()
              closed_extras += 1
          except Exception:
            pass
        log(
          f"Reusing AdsPower tab (closed {closed_extras} extra tab(s), "
          f"url={(page.url or '')[:80]})"
        )
    else:
      page = context.new_page()
      log("Opened new work tab")
    return page

  @staticmethod
  def _close_extra_tabs(context, keep_page: Optional[Page]) -> int:
    closed = 0
    for tab in list(context.pages):
      if keep_page is not None and tab == keep_page:
        continue
      try:
        if not tab.is_closed():
          tab.close()
          closed += 1
      except Exception:
        pass
    return closed

  @staticmethod
  def _attach_tab_guard(
    context,
    work_page: Page,
    log: Callable[[str], None],
    *,
    target_host: str = "",
    target_hosts: list[str] | None = None,
  ) -> dict:
    work_ref = {"page": work_page, "serp_page": work_page, "allow_target_tab_until": 0.0}
    normalized_hosts: list[str] = []
    for raw in (target_hosts or []):
      normalized = SerpBot._normalize_domain(raw)
      if normalized and normalized not in normalized_hosts:
        normalized_hosts.append(normalized)
    if not normalized_hosts and target_host:
      normalized = SerpBot._normalize_domain(target_host)
      if normalized:
        normalized_hosts.append(normalized)

    def _is_target_tab(url: str) -> bool:
      if not normalized_hosts:
        return False
      try:
        host = SerpBot._normalize_domain(urlparse(url).netloc or "")
      except Exception:
        return False
      return any(
        host == normalized or host.endswith(f".{normalized}")
        for normalized in normalized_hosts
      )

    def on_new_tab(new_page: Page) -> None:
      current = work_ref["page"]
      if new_page == current and not current.is_closed():
        return
      try:
        url = (new_page.url or "").strip()
        label = url[:90] if url and url != "about:blank" else "blank"
        if SerpBot._is_captcha_url(url):
          log(f"[Captcha] Keeping captcha tab open ({label})")
          work_ref["page"] = new_page
          return
        allow_until = float(work_ref.get("allow_target_tab_until") or 0)
        if time.time() < allow_until:
          log(f"[Tab] Allowing new tab during target open ({label})")
          return
        if _is_target_tab(url):
          log(f"[Tab] Keeping target-site tab ({label})")
          return
        if current is None or current.is_closed():
          log(f"[Tab] Work tab was closed — adopting new tab ({label})")
          work_ref["page"] = new_page
          return
        log(f"[Tab] Closing popup/extra tab ({label})")
        if not new_page.is_closed():
          new_page.close()
      except Exception:
        pass

    context.on("page", on_new_tab)
    return work_ref

  def _prepare_mobile_session(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
  ) -> Page:
    if not self._is_mobile_profile(profile):
      return page
    if page.is_closed():
      page = self._recover_work_page(page.context, profile, log)
    log(f"[Mobile] Preparing session ({profile.os_browser_label})")
    if enable_mobile_touch(page, timeout_seconds=10.0):
      log(f"[Mobile] CDP touch emulation ready ({profile.os_browser_label})")
    else:
      log(
        f"[Mobile] CDP touch emulation skipped or timed out ({profile.os_browser_label}) "
        "— continuing without touch setup"
      )
    return page

  def _page_html(self, page: Optional[Page]) -> str:
    if not page:
      return ""
    try:
      return page.content()
    except Exception:
      return ""

  def _report_failure(
    self,
    page: Optional[Page],
    profile: ProfileSpec,
    context: str,
    exc: BaseException,
    on_failure: Optional[FailureCallback],
  ) -> None:
    capture_exception(
      profile_id=profile.profile_id,
      profile_name=profile.name,
      context=context,
      exc=exc,
      page_html=self._page_html(page),
    )
    self.logger(f"[CrashReport] Saved failure context for {profile.name} -> data/crash_report.json")
    if on_failure:
      on_failure(profile, context, exc)

  @staticmethod
  def _resume_status_for_context(context: str) -> ProfileStatus:
    lowered = (context or "").lower()
    if "target" in lowered or "visit" in lowered or "dwell" in lowered:
      return ProfileStatus.VISITING_SITE
    if "warmup" in lowered or "startup" in lowered:
      return ProfileStatus.WARMING_UP
    if "search" in lowered or "keyword" in lowered or "serp" in lowered or "retry" in lowered:
      return ProfileStatus.SEARCHING
    return ProfileStatus.SEARCHING

  def _guard_captcha(
    self,
    page: Page,
    stop_event: Optional[threading.Event],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    profile: ProfileSpec,
    log: Callable[[str], None],
    context: str = "action",
  ) -> tuple[str, Page]:
    page = self._sync_work_page(page, profile, log)

    def captcha_status(status_key: str, display_text: str) -> None:
      if on_ui_status:
        on_ui_status(status_key, display_text)
      elif status_key == UiStatusKey.CAPTCHA_MANUAL.value:
        set_status(ProfileStatus.CAPTCHA_MANUAL)
      else:
        set_status(ProfileStatus.CAPTCHA_WAIT)

    def resume_after_captcha() -> None:
      set_status(SerpBot._resume_status_for_context(context))

    def finish_captcha_flow(result: str) -> tuple[str, Page]:
      nonlocal page
      if result == "ok":
        resume_after_captcha()
        page = self._sync_work_page(page, profile, log)
        page = self._recover_page_after_captcha(page, profile, log, context=context)
        log(f"[Captcha] Post-solve page synced ({context}) — locators must be recreated")
      return result, page

    try:
      captcha_present = self.captcha.requires_captcha_clear(page)
    except Exception as exc:
      if self.captcha.is_awaiting_clear():
        log(f"[Captcha] Check failed during {context}; still waiting for solve: {exc}")
        return finish_captcha_flow(
          self.captcha.handle_before_action(
            page, stop_event, captcha_status, resume_after_captcha,
          )
        )
      if self._is_connection_error(exc):
        log(f"[Captcha] Check failed during {context}; retrying: {exc}")
        time.sleep(1.2)
        return "ok", page
      log(f"[Captcha] Check error during {context}: {exc}")
      set_status(ProfileStatus.ERROR)
      return "error", page
    if not captcha_present:
      return "ok", page
    blocked = self._register_captcha_detection(log)
    if blocked:
      set_status(ProfileStatus.ERROR, detail="Consecutive captcha")
      return "blocked", page
    log(
      f"[Captcha] Detected during {context} "
      f"(automated={'yes' if self.captcha.automated_mode else 'no'}, url={(page.url or '')[:100]})"
    )

    try:
      return finish_captcha_flow(
        self.captcha.handle_before_action(
          page, stop_event, captcha_status, resume_after_captcha,
        )
      )
    except Exception as exc:
      if self.captcha.is_awaiting_clear() or self._is_connection_error(exc):
        log(f"[Captcha] Solver flow interrupted during {context}; waiting for solve: {exc}")
        try:
          return finish_captcha_flow(
            self.captcha.handle_before_action(
              page, stop_event, captcha_status, resume_after_captcha,
            )
          )
        except Exception as retry_exc:
          log(f"[Captcha] Solve retry failed during {context}: {retry_exc}")
      log(f"[Captcha] Error: {exc}")
      set_status(ProfileStatus.ERROR)
      return "error", page

  def _warmup(
    self,
    page: Page,
    profile: ProfileSpec,
    stop_event: Optional[threading.Event],
    stopped: Callable[[], bool],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    on_failure: Optional[FailureCallback],
    log: Callable[[str], None],
  ) -> tuple[Page, str]:
    if not self.config.warmup_queries:
      return page, "ok"

    count_lo = max(1, int(self.config.warmup_count_min))
    count_hi = max(count_lo, int(self.config.warmup_count_max))
    query_count = min(len(self.config.warmup_queries), random.randint(count_lo, count_hi))
    queries = random.sample(self.config.warmup_queries, k=query_count)
    log(
      f"[Warmup] Running {query_count} warm-up quer{'y' if query_count == 1 else 'ies'} "
      f"(configured {count_lo}-{count_hi}, human idle text select — no scroll/link clicks)"
    )

    for index, query in enumerate(queries, start=1):
      if stopped():
        return page, "stopped"
      page, proxy_guard = self._check_google_proxy_connect(page, log)
      if proxy_guard:
        return page, proxy_guard
      self._set_network_phase(PagePhase.GOOGLE_SERP, query, page)
      guard, page = self._guard_captcha(
        page, stop_event, set_status, on_ui_status, profile, log, context="warmup",
      )
      if guard in ("stopped", "error", "blocked"):
        return page, guard
      set_status(ProfileStatus.WARMING_UP, detail=query)
      warmup_min_ms = max(1000, int(self.config.warmup_dwell_min * 1000))
      warmup_max_ms = max(warmup_min_ms, int(self.config.warmup_dwell_max * 1000))
      stay_ms = random.randint(warmup_min_ms, warmup_max_ms)
      mobile = self._is_mobile_profile(profile)
      label = "Mobile" if mobile else "Desktop"
      page = self._prepare_google_search_page(
        page, profile, log, timeout_seconds=20.0,
      )
      page, proxy_guard = self._check_google_proxy_connect(page, log)
      if proxy_guard:
        return page, proxy_guard
      if self._is_on_google_home_or_ntp(page):
        log(f"[Warmup] {label} Google entry search box: {query}")
      else:
        log(f"[Warmup] {label} SERP search box: {query}")
      search_ok = False
      last_search_exc: Optional[BaseException] = None
      try:
        page = self._google_search(
          page, query, profile, on_failure, method="serp_box",
        )
        guard, page = self._ensure_captcha_clear(
          page, stop_event, set_status, on_ui_status, profile, log, context="post-warmup-search",
        )
        if guard in self._captcha_abort_values():
          return page, guard
        if self._url_looks_like_captcha(page) or self.captcha.requires_captcha_clear(page):
          guard, page = self._ensure_captcha_clear(
            page, stop_event, set_status, on_ui_status, profile, log, context="warmup-sorry",
          )
          if guard in self._captcha_abort_values():
            return page, guard
        page = self._wait_for_serp_stable(page, log, timeout_seconds=8.0)
        search_ok = self._is_on_google_serp(page)
      except Exception as exc:
        last_search_exc = exc
        self.logger(f"[Warmup] Search phase warning: {exc}")
        guard, page = self._ensure_captcha_clear(
          page, stop_event, set_status, on_ui_status, profile, log, context="warmup-search-error",
        )
        if guard in ("stopped", "error", "blocked"):
          return page, guard
        if not search_ok:
          search_ok = self._is_on_google_serp(page)

      if not search_ok:
        search_ok = self._is_on_google_serp(page)

      if not search_ok and not self._is_on_google_serp(page):
        self.logger("[Warmup] SERP not reached — retrying Google entry + search box")
        try:
          page = self._prepare_google_search_page(
            page, profile, log, timeout_seconds=20.0,
          )
          page = self._google_search(
            page, query, profile, on_failure, method="serp_box",
          )
          guard, page = self._ensure_captcha_clear(
            page, stop_event, set_status, on_ui_status, profile, log, context="post-warmup-retry",
          )
          if guard in self._captcha_abort_values():
            return page, guard
          page = self._wait_for_serp_stable(page, log, timeout_seconds=8.0)
          search_ok = self._is_on_google_serp(page)
        except Exception as retry_exc:
          self.logger(f"[Warmup] Warm-up search retry failed: {retry_exc}")

      if search_ok and self._is_on_google_serp(page):
        self.logger(
          f"[Warmup] SERP dwell started ({stay_ms // 1000}s, "
          "human idle text select — no scrolling/link clicks)"
        )
        page, dwell_guard = self._warmup_dwell(
          page,
          stay_ms,
          stopped,
          profile,
          stop_event,
          set_status,
          on_ui_status,
          log,
        )
        if dwell_guard in self._captcha_abort_values():
          return page, dwell_guard
      else:
        if self._url_looks_like_captcha(page) or self.captcha.requires_captcha_clear(page):
          guard, page = self._ensure_captcha_clear(
            page, stop_event, set_status, on_ui_status, profile, log, context="warmup-not-serp",
          )
          if guard in self._captcha_abort_values():
            return page, guard
        else:
          self.logger(
            "[Warmup] SERP not open after warm-up search — skipping text-hold dwell "
            f"({(page.url or '')[:90]})"
          )
          page, proxy_guard = self._check_google_proxy_connect(page, log)
          if proxy_guard:
            return page, proxy_guard
          fallback_ms = min(stay_ms, 3000)
          if fallback_ms > 0 and not stopped():
            self._safe_page_wait(page, fallback_ms)
      if stopped():
        return page, "stopped"

    if not stopped():
      page = self._return_to_google_entry_after_warmup(page, profile, log)
    page = self._sync_work_page(page, profile, log)
    return page, "ok"

  @staticmethod
  def _uses_low_traffic_warmup_interaction(profile: ProfileSpec) -> bool:
    os_type = (profile.os_type or "").strip().lower()
    return os_type.startswith("windows") or os_type.startswith("android")

  def _first_viewport_text_regions(self, page: Page) -> list[dict]:
    """Return visible non-interactive text rectangles without scrolling the page."""
    try:
      regions = page.evaluate(
        """() => {
          const viewportWidth = window.innerWidth || 0;
          const viewportHeight = window.innerHeight || 0;
          const interactive = 'a, button, input, textarea, select, [role="button"], '
            + '[role="link"], [contenteditable="true"], [onclick]';
          const blocked = 'header, nav, form, [role="navigation"], [role="search"]';
          const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_TEXT,
            {
              acceptNode(node) {
                const text = (node.nodeValue || '').replace(/\\s+/g, ' ').trim();
                if (text.length < 12) return NodeFilter.FILTER_REJECT;
                const parent = node.parentElement;
                if (!parent || parent.closest(interactive) || parent.closest(blocked)) {
                  return NodeFilter.FILTER_REJECT;
                }
                const style = window.getComputedStyle(parent);
                if (style.display === 'none' || style.visibility === 'hidden'
                    || style.opacity === '0') {
                  return NodeFilter.FILTER_REJECT;
                }
                return NodeFilter.FILTER_ACCEPT;
              }
            }
          );
          const regions = [];
          while (walker.nextNode() && regions.length < 80) {
            const node = walker.currentNode;
            const range = document.createRange();
            range.selectNodeContents(node);
            for (const rect of range.getClientRects()) {
              const left = Math.max(0, rect.left);
              const top = Math.max(0, rect.top);
              const right = Math.min(viewportWidth, rect.right);
              const bottom = Math.min(viewportHeight, rect.bottom);
              const width = right - left;
              const height = bottom - top;
              if (top < 90 || bottom > viewportHeight - 8 || width < 90 || height < 10) {
                continue;
              }
              regions.push({ x: left, y: top, width, height });
              break;
            }
          }
          return regions;
        }"""
      )
      if not isinstance(regions, list):
        return []
      return [
        region for region in regions
        if isinstance(region, dict)
        and float(region.get("width") or 0) >= 90
        and float(region.get("height") or 0) >= 10
      ]
    except Exception:
      return []

  def _warmup_point_is_safe(self, page: Page, x: float, y: float) -> bool:
    """True when the point is not over a link/button/input (warm-up must not tap results)."""
    try:
      return bool(
        page.evaluate(
          """([x, y]) => {
            const el = document.elementFromPoint(x, y);
            if (!el) return false;
            const interactive = 'a, button, input, textarea, select, [role="button"], '
              + '[role="link"], [contenteditable="true"], [onclick]';
            return !el.closest(interactive);
          }""",
          [float(x), float(y)],
        )
      )
    except Exception:
      return False

  @staticmethod
  def _clear_page_text_selection(page: Page) -> None:
    try:
      page.evaluate(
        """() => {
          const selection = window.getSelection && window.getSelection();
          if (selection) selection.removeAllRanges();
        }"""
      )
    except Exception:
      pass

  def _warmup_windows_text_drag(self, page: Page, region: dict) -> bool:
    x = float(region.get("x") or 0)
    y = float(region.get("y") or 0)
    width = float(region.get("width") or 0)
    height = float(region.get("height") or 0)
    if width < 90 or height < 10:
      return False
    start_x = x + min(max(8.0, width * 0.12), width - 18.0)
    end_x = min(x + width - 8.0, start_x + random.uniform(45.0, min(170.0, width * 0.72)))
    point_y = y + min(max(5.0, height * 0.58), height - 3.0)
    if not self._warmup_point_is_safe(page, start_x, point_y):
      return False
    if not self._warmup_point_is_safe(page, end_x, point_y):
      return False
    pressed = False
    try:
      # Natural pointer path: approach → press → brief hold → drag → release.
      page.mouse.move(
        start_x + random.uniform(-10, 10),
        point_y + random.uniform(-8, 8),
        steps=random.randint(4, 8),
      )
      page.wait_for_timeout(random.randint(80, 220))
      page.mouse.move(start_x, point_y, steps=random.randint(3, 7))
      page.wait_for_timeout(random.randint(120, 320))
      page.mouse.down()
      pressed = True
      page.wait_for_timeout(random.randint(900, 1800))
      page.mouse.move(
        end_x,
        point_y + random.uniform(-2.0, 2.0),
        steps=random.randint(12, 24),
      )
      page.mouse.up()
      pressed = False
      # Keep any natural highlight visible briefly, then release like a person.
      page.wait_for_timeout(random.randint(600, 1300))
      self._clear_page_text_selection(page)
      return True
    except Exception:
      return False
    finally:
      if pressed:
        try:
          page.mouse.up()
        except Exception:
          pass

  def _warmup_android_text_hold(self, page: Page, region: dict) -> bool:
    x = float(region.get("x") or 0)
    y = float(region.get("y") or 0)
    width = float(region.get("width") or 0)
    height = float(region.get("height") or 0)
    if width < 90 or height < 10:
      return False
    start_x = x + min(max(8.0, width * 0.16), width - 20.0)
    end_x = min(x + width - 8.0, start_x + random.uniform(48.0, min(160.0, width * 0.70)))
    point_y = y + min(max(5.0, height * 0.55), height - 3.0)
    if not self._warmup_point_is_safe(page, start_x, point_y):
      return False
    if not self._warmup_point_is_safe(page, end_x, point_y):
      return False
    client = None
    touching = False
    try:
      enable_mobile_touch(page)
      client = page.context.new_cdp_session(page)
      radius = random.uniform(8.0, 14.0)
      force = round(random.uniform(0.45, 0.75), 3)

      def _touch_point(px: float, py: float) -> dict:
        return {
          "x": round(px, 1),
          "y": round(py, 1),
          "radiusX": round(radius, 1),
          "radiusY": round(radius * random.uniform(0.9, 1.1), 1),
          "force": force,
          "id": 0,
        }

      client.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [_touch_point(start_x, point_y)]},
      )
      touching = True
      # Long-press then short horizontal drag — native selection only, no JS force.
      page.wait_for_timeout(random.randint(900, 1800))
      steps = random.randint(6, 12)
      for step in range(1, steps + 1):
        ratio = step / steps
        move_x = start_x + (end_x - start_x) * ratio
        move_y = point_y + random.uniform(-1.2, 1.2)
        client.send(
          "Input.dispatchTouchEvent",
          {"type": "touchMove", "touchPoints": [_touch_point(move_x, move_y)]},
        )
        page.wait_for_timeout(random.randint(18, 40))
      client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
      touching = False
      page.wait_for_timeout(random.randint(600, 1300))
      self._clear_page_text_selection(page)
      try:
        page.keyboard.press("Escape")
      except Exception:
        pass
      return True
    except Exception:
      return False
    finally:
      if touching and client is not None:
        try:
          client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        except Exception:
          pass

  def _perform_warmup_text_hold(
    self,
    page: Page,
    profile: ProfileSpec,
  ) -> bool:
    regions = self._first_viewport_text_regions(page)
    if not regions:
      return False
    random.shuffle(regions)
    mobile = (profile.os_type or "").strip().lower().startswith("android")
    for region in regions[:8]:
      if mobile:
        if self._warmup_android_text_hold(page, region):
          return True
      elif self._warmup_windows_text_drag(page, region):
        return True
    return False

  def _warmup_idle_gaze(self, page: Page, profile: ProfileSpec) -> None:
    """Light human idle over safe non-link areas. Never scroll or click links."""
    if page.is_closed():
      return
    mobile = (profile.os_type or "").strip().lower().startswith("android")
    try:
      if mobile:
        # Android: idle pause only — short taps can still feel like accidental presses.
        page.wait_for_timeout(random.randint(280, 650))
        return
      viewport = page.viewport_size or {"width": 1280, "height": 800}
      width = float(viewport.get("width") or 1280)
      height = float(viewport.get("height") or 800)
      regions = self._first_viewport_text_regions(page)
      targets: list[tuple[float, float]] = []
      for region in regions[:6]:
        rx = float(region.get("x") or 0)
        ry = float(region.get("y") or 0)
        rw = float(region.get("width") or 0)
        rh = float(region.get("height") or 0)
        px = rx + random.uniform(rw * 0.2, max(rw * 0.2, rw * 0.8))
        py = ry + random.uniform(rh * 0.3, max(rh * 0.3, rh * 0.7))
        if self._warmup_point_is_safe(page, px, py):
          targets.append((px, py))
      if not targets:
        for _ in range(6):
          px = width * random.uniform(0.22, 0.78)
          py = height * random.uniform(0.28, 0.62)
          if self._warmup_point_is_safe(page, px, py):
            targets.append((px, py))
      if not targets:
        page.wait_for_timeout(random.randint(250, 500))
        return
      random.shuffle(targets)
      for px, py in targets[: random.randint(1, 2)]:
        page.mouse.move(px, py, steps=random.randint(6, 14))
        page.wait_for_timeout(random.randint(180, 480))
    except Exception:
      pass

  def _return_to_google_entry_after_warmup(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
  ) -> Page:
    """Leave the warm-up SERP and prepare a clean Google box for the target query."""
    try:
      log("[Warmup] Dwell complete — returning to Google main for target search")
      page = self._ensure_google_web_search_context(
        page, profile, log, timeout_seconds=20.0,
      )
      if self._is_on_google_web_search_context(page):
        return page
      page.goto(
        self.GOOGLE_ENTRY_URL,
        wait_until="commit" if self._is_mobile_profile(profile) else "domcontentloaded",
        timeout=self.GOTO_TIMEOUT_MOBILE_MS if self._is_mobile_profile(profile)
        else self.GOTO_TIMEOUT_MS,
      )
      return self._prepare_google_search_page(
        page, profile, log, timeout_seconds=20.0,
      )
    except Exception as exc:
      log(f"[Warmup] Google main return warning: {exc}")
      return page

  def _warmup_dwell(
    self,
    page: Page,
    stay_ms: int,
    stopped: Callable[[], bool],
    profile: ProfileSpec,
    stop_event: Optional[threading.Event],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    log: Callable[[str], None],
  ) -> tuple[Page, str]:
    started_at = time.monotonic()
    if self._uses_low_traffic_warmup_interaction(profile):
      requested_actions = random.randint(1, 2)
      completed_actions = 0
      for _ in range(requested_actions):
        if stopped() or int((time.monotonic() - started_at) * 1000) >= stay_ms:
          break
        if self._perform_warmup_text_hold(page, profile):
          completed_actions += 1
          page.wait_for_timeout(random.randint(200, 500))
          if not stopped():
            self._warmup_idle_gaze(page, profile)
        else:
          break
      self.logger(
        f"[Warmup] First-screen text interaction {completed_actions}/"
        f"{requested_actions} complete (no scroll/link clicks)"
      )

    chunk_ms = min(3000, max(1000, stay_ms // 4))
    while not stopped():
      elapsed = int((time.monotonic() - started_at) * 1000)
      if elapsed >= stay_ms:
        break
      guard, page = self._ensure_captcha_clear(
        page, stop_event, set_status, on_ui_status, profile, log, context="warmup-dwell",
      )
      if guard != "ok":
        return page, guard
      remaining = min(chunk_ms, stay_ms - elapsed)
      # Idle wait with occasional safe gaze — never scroll during warm-up dwell.
      if (
        self._uses_low_traffic_warmup_interaction(profile)
        and remaining > 900
        and random.random() < 0.45
      ):
        self._warmup_idle_gaze(page, profile)
      self._interruptible_wait(page, remaining, stopped, scroll_mobile=False)
    return page, "ok"

  def _google_search_url(self, query: str, profile: ProfileSpec, *, start: int = 0) -> str:
    params = f"q={quote_plus(query)}"
    if start > 0:
      params += f"&start={start}"
    if not self._is_mobile_profile(profile):
      params += "&udm=14"
    return f"https://www.google.co.kr/search?{params}"

  @staticmethod
  def _normalize_keyword(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip().lower())

  def _is_on_google_serp(self, page: Page) -> bool:
    try:
      if page.is_closed():
        return False
      host = (urlparse(page.url).netloc or "").lower()
      if "google." not in host:
        return False
      return self._url_is_google_serp(page.url or "")
    except Exception:
      return False

  @staticmethod
  def _is_on_google_home_or_ntp(page: Page) -> bool:
    """AdsPower SunBrowser New Tab or Google homepage (not yet on /search)."""
    try:
      if page.is_closed():
        return False
      url = (page.url or "").strip()
      lowered = url.lower()
      if SerpBot._url_on_serp_or_sorry(lowered):
        return False
      if lowered.startswith("chrome://new-tab-page"):
        return True
      host = (urlparse(url).netloc or "").lower()
      if "google." not in host:
        return False
      path = (urlparse(url).path or "").lower().rstrip("/") or "/"
      return path in ("/", "/webhp")
    except Exception:
      return False

  def _can_use_google_search_box(self, page: Page) -> bool:
    return self._is_google_search_box_visible(page)

  def _is_on_serp_for_keyword(self, page: Page, keyword: str) -> bool:
    try:
      if not self._is_on_google_serp(page):
        return False
      qs = parse_qs(urlparse(page.url).query)
      current_q = unquote(qs.get("q", [""])[0] or "")
      return self._normalize_keyword(current_q) == self._normalize_keyword(keyword)
    except Exception:
      return False

  def _wait_for_serp_stable(
    self,
    page: Page,
    log: Callable[[str], None],
    timeout_seconds: float = 10.0,
  ) -> Page:
    try:
      url = page.url or ""
      if self._is_captcha_url(url):
        return page
    except Exception:
      pass
    deadline = time.time() + max(2.0, float(timeout_seconds))
    while time.time() < deadline:
      try:
        url = page.url or ""
        if self._is_captcha_url(url):
          return page
        if self._url_is_google_serp(url):
          self._dismiss_google_serp_overlays(page, log)
          try:
            page.wait_for_load_state("domcontentloaded", timeout=4000)
          except Exception:
            pass
          page.wait_for_timeout(250)
          if page.locator("#search, #rso, div#search").count() > 0:
            return page
      except Exception as exc:
        if self._is_connection_error(exc):
          log(f"[Search] Waiting for SERP to stabilize: {exc}")
          time.sleep(0.4)
          continue
        raise
      time.sleep(0.18)
    return page

  def _ensure_page_focus(self, page: Page) -> None:
    for attempt in range(3):
      try:
        page.bring_to_front()
      except Exception:
        pass
      try:
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        cx = max(8, int(viewport.get("width", 1280)) // 2)
        cy = max(8, int(viewport.get("height", 800)) // 3)
        page.mouse.click(cx, cy)
        page.wait_for_timeout(random.randint(100, 200))
      except Exception:
        pass
      page.wait_for_timeout(random.randint(150, 280))
      if attempt >= 1:
        break

  def _recover_google_serp_before_search(
    self,
    page: Page,
    query: str,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
  ) -> Page:
    page = self._ensure_google_web_search_context(
      page, profile, self.logger, timeout_seconds=15.0,
    )
    if self._is_google_search_box_visible(page):
      return page
    current = (page.url or "")[:100]
    self.logger(
      f"[Search] Google search box not visible before '{query}' ({current}) — preparing UI"
    )
    return self._ensure_google_search_box_ready(
      page, profile, self.logger, timeout_seconds=15.0,
    )

  def _google_search_serp_box(
    self,
    page: Page,
    query: str,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
  ) -> Page:
    page = self._recover_google_serp_before_search(page, query, profile, on_failure)
    type_lo, type_hi = self._search_type_delay_bounds()
    mobile = self._is_mobile_profile(profile)
    search_box = self._wait_for_google_search_box(
      page, profile, self.logger, timeout_ms=12_000 if mobile else 10_000,
    )
    if search_box is None:
      self.logger(
        f"[Search] Search box hidden for '{query}' — expanding Google UI (no URL search)"
      )
      page = self._ensure_google_search_box_ready(
        page, profile, self.logger, timeout_seconds=15.0,
      )
      search_box = self._wait_for_google_search_box(
        page, profile, self.logger, timeout_ms=12_000 if mobile else 10_000,
      )
    if search_box is None:
      raise RuntimeError(
        f"Google search box unavailable for '{query}' — refusing direct URL search"
      )
    label = "Mobile" if mobile else "Desktop"
    entry = self._is_on_google_home_or_ntp(page)
    box_label = "entry" if entry else "SERP"
    self.logger(f"[Search] {label} Google {box_label} search box: {query}")
    human_type_focus_safe(
      search_box,
      query,
      type_lo,
      type_hi,
      typo_chance=0.03,
      min_length_for_typo=9,
      page=page,
      mobile=mobile,
    )
    if mobile:
      try:
        typed = (search_box.input_value(timeout=2000) or "").strip()
        expected = query.strip()
        if typed and typed != expected:
          self.logger(
            f"[Search] Mobile SERP box text mismatch after clear "
            f"(got '{typed[:72]}', expected '{expected[:72]}') — retrying search box"
          )
          try:
            search_box.clear(timeout=2000)
          except Exception:
            pass
          page.wait_for_timeout(random.randint(200, 400))
          human_type_focus_safe(
            search_box,
            query,
            type_lo,
            type_hi,
            typo_chance=0.0,
            min_length_for_typo=999,
            page=page,
            mobile=mobile,
          )
      except Exception as exc:
        self.logger(f"[Search] Mobile SERP box verify warning: {exc}")
    return self._submit_search_query(
      page,
      query,
      profile,
      on_failure,
      enter_wait_seconds=12.0 if mobile else 8.0,
      search_box=search_box,
    )

  def _wait_for_serp_after_enter(
    self,
    page: Page,
    query: str,
    *,
    enter_wait_seconds: float = 8.0,
  ) -> Page:
    enter_deadline = time.time() + max(2.0, float(enter_wait_seconds))
    while time.time() < enter_deadline:
      if self._url_on_serp_or_sorry(page.url or ""):
        break
      try:
        page.wait_for_load_state("domcontentloaded", timeout=1200)
      except Exception:
        pass
      time.sleep(0.12)
    else:
      self.logger(
        f"[Search] Enter did not open SERP within {enter_wait_seconds:.0f}s — retrying Enter"
      )
      type_lo, type_hi = self._search_type_delay_bounds()
      random_delay(type_lo * 0.35, type_hi * 0.55)
      try:
        page.keyboard.press("Enter")
      except Exception:
        pass
      retry_deadline = time.time() + max(4.0, float(enter_wait_seconds) * 0.75)
      while time.time() < retry_deadline:
        if self._url_on_serp_or_sorry(page.url or ""):
          break
        try:
          page.wait_for_load_state("domcontentloaded", timeout=1200)
        except Exception:
          pass
        time.sleep(0.12)
      else:
        raise RuntimeError(
          f"SERP did not open after search box submit for '{query}'"
        )
    return self._wait_for_serp_stable(page, self.logger, timeout_seconds=8.0)

  def _submit_search_query(
    self,
    page: Page,
    query: str,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
    *,
    enter_wait_seconds: float = 3.0,
    search_box=None,
  ) -> Page:
    type_lo, type_hi = self._search_type_delay_bounds()
    random_delay(type_lo * 0.35, type_hi * 0.55)
    if search_box is not None:
      try:
        search_box.press("Enter")
      except Exception:
        page.keyboard.press("Enter")
    else:
      page.keyboard.press("Enter")

    return self._wait_for_serp_after_enter(
      page, query, enter_wait_seconds=enter_wait_seconds,
    )

  def _evaluate_with_retry(
    self,
    page: Page,
    script: str,
    arg=None,
    *,
    retries: int = 5,
    log: Optional[Callable[[str], None]] = None,
  ):
    last_exc: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
      try:
        if page.is_closed():
          raise RuntimeError("page closed before evaluate")
        if arg is None:
          return page.evaluate(script)
        return page.evaluate(script, arg)
      except Exception as exc:
        last_exc = exc
        if not self._is_connection_error(exc):
          raise
        if log:
          log(f"[Search] DOM evaluate retry {attempt}/{retries}: {exc}")
        time.sleep(0.7 * attempt)
        try:
          page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
          pass
    if last_exc:
      raise last_exc
    return None

  def _google_search(
    self,
    page: Page,
    query: str,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
    *,
    method: str = "auto",
  ) -> Page:
    self._dismiss_google_serp_overlays(page, self.logger)
    mobile = self._is_mobile_profile(profile)
    resolved = method
    if resolved in ("auto", "omnibox"):
      resolved = "serp_box"

    if resolved == "serp_box":
      self.logger(f"[Search] SERP search box query: {query}")
      try:
        page = self._google_search_serp_box(page, query, profile, on_failure)
      except Exception as exc:
        if self._is_connection_error(exc):
          raise
        if mobile and self._is_on_google_serp(page):
          self.logger(f"[Search] SERP box failed on mobile SERP — expanding search UI: {exc}")
          page = self._ensure_google_search_box_ready(
            page, profile, self.logger, timeout_seconds=12.0,
          )
        else:
          self.logger(f"[Search] SERP box failed, retrying Google entry: {exc}")
          page = self._ensure_google_search_box_ready(
            page, profile, self.logger, timeout_seconds=15.0,
          )
        page = self._google_search_serp_box(page, query, profile, on_failure)
    else:
      raise ValueError(f"Unknown search method: {method}")

    self._session_has_searched = True
    self._dismiss_google_serp_overlays(page, self.logger)
    if self.captcha.requires_captcha_clear(page) or self._url_looks_like_captcha(page):
      self.logger(
        f"[Captcha] Post-search captcha signal detected (url={(page.url or '')[:120]})"
      )
    else:
      settle_ms = random.randint(350, 700) if mobile else random.randint(500, 900)
      page.wait_for_timeout(settle_ms)
      # Light post-search settle (human-like). Warm-up dwell itself still avoids
      # deliberate scrolling / link clicks.
      micro_scroll(
        page,
        times=1,
        delay_lo=0.15,
        delay_hi=0.35,
        mobile=mobile,
      )
      stable_timeout = 10.0 if mobile else 12.0
      page = self._wait_for_serp_stable(page, self.logger, timeout_seconds=stable_timeout)
    return page

  @staticmethod
  def _extract_sorry_continue_url(url: str) -> str:
    try:
      qs = parse_qs(urlparse(url).query)
      continue_url = unquote(qs.get("continue", [""])[0] or "").strip()
      if continue_url and "google." in continue_url.lower():
        return continue_url
    except Exception:
      pass
    return ""

  def _recover_page_after_captcha(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
    *,
    context: str = "",
  ) -> Page:
    if page.is_closed():
      return page
    mobile = self._is_mobile_profile(profile)
    stable_timeout = 10.0 if mobile else 12.0
    try:
      current_url = page.url or ""
      if "/sorry" in current_url.lower():
        continue_url = self._extract_sorry_continue_url(current_url)
        if continue_url:
          log(f"[Captcha] Following sorry continue URL after solve ({context or 'captcha'})")
          page.goto(continue_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
      log(f"[Captcha] Continue URL navigation warning: {exc}")

    self._dismiss_google_serp_overlays(page, log)
    page = self._wait_for_serp_stable(page, log, timeout_seconds=stable_timeout)

    if not self._is_on_google_serp(page):
      continue_url = self._extract_sorry_continue_url(page.url or "")
      if continue_url:
        try:
          log("[Captcha] Retrying continue URL — SERP not ready after solve")
          page.goto(continue_url, wait_until="domcontentloaded", timeout=60000)
          self._dismiss_google_serp_overlays(page, log)
          page = self._wait_for_serp_stable(page, log, timeout_seconds=stable_timeout)
        except Exception as exc:
          log(f"[Captcha] SERP recovery retry warning: {exc}")
      elif self._is_on_google_home_or_ntp(page):
        log("[Captcha] Post-solve on Google home/NTP — SERP not restored yet")
    return page

  def _dismiss_google_serp_overlays(self, page: Page, log: Callable[[str], None]) -> None:
    self._dismiss_google_consent(page, log)
    try:
      if is_google_location_prompt_present(page):
        dismiss_google_location_prompt(page, log)
    except Exception as exc:
      log(f"[Consent] Location prompt dismiss warning: {exc}")

  def _dismiss_google_consent(self, page: Page, log: Callable[[str], None]) -> None:
    try:
      if not is_google_consent_present(page):
        return
      dismiss_google_consent(page, log)
    except Exception as exc:
      log(f"[Consent] Dismiss warning: {exc}")

  def _captcha_abort_values(self) -> tuple[str, ...]:
    return ("stopped", "error", "blocked")

  def _register_captcha_detection(self, log: Callable[[str], None]) -> bool:
    """Track captcha hits for diagnostics; solve budget is enforced in CaptchaSolver."""
    self._session_captcha_events += 1
    return False

  def _ensure_captcha_clear(
    self,
    page: Page,
    stop_event: Optional[threading.Event],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    profile: ProfileSpec,
    log: Callable[[str], None],
    context: str,
  ) -> tuple[str, Page]:
    if not page.is_closed():
      self._dismiss_google_serp_overlays(page, log)
    while True:
      if stop_event and stop_event.is_set():
        return "stopped", page

      page = self._sync_work_page(page, profile, log)
      try:
        captcha_present = self.captcha.requires_captcha_clear(page)
      except Exception as exc:
        if self.captcha.is_awaiting_clear():
          log(f"[Captcha] Active at {context} — waiting for solve after check error: {exc}")
          time.sleep(1.2)
          continue
        if self._is_connection_error(exc):
          log(f"[Captcha] Connection issue at {context}; retrying: {exc}")
          time.sleep(2.0)
          continue
        return "error", page

      if not captcha_present:
        return "ok", page

      log(f"[Captcha] Active at {context} — waiting until captcha is solved")
      guard, page = self._guard_captcha(
        page,
        stop_event,
        set_status,
        on_ui_status,
        profile,
        log,
        context=context,
      )
      if guard == "ok":
        if not self.captcha.requires_captcha_clear(page):
          page = self._recover_page_after_captcha(page, profile, log, context=context)
          return "ok", page
        continue
      if guard in self._captcha_abort_values():
        return guard, page
      time.sleep(1.2)

  def _resolve_captcha_after_navigation(
    self,
    page: Page,
    stop_event: Optional[threading.Event],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    profile: ProfileSpec,
    log: Callable[[str], None],
    context: str,
  ) -> tuple[str, Page]:
    return self._ensure_captcha_clear(
      page, stop_event, set_status, on_ui_status, profile, log, context,
    )

  def _visit_first_organic_result(
    self,
    page: Page,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
  ) -> bool:
    random_delay(self.config.action_delay_min, self.config.action_delay_max)
    mobile = self._is_mobile_profile(profile)
    candidate_hrefs = self._collect_warmup_candidate_hrefs(page, mobile=mobile)
    if not candidate_hrefs:
      self.logger("[Warmup] No clickable result found on first page")
      return False

    for tried, display_href in enumerate(candidate_hrefs[:8], start=1):
      self.logger(f"[Warmup] Visiting candidate {tried}: {display_href[:120]}")
      try:
        if mobile:
          page.goto(display_href, wait_until="domcontentloaded", timeout=60000)
        else:
          link = self._find_result_link_for_href(page, display_href, mobile=mobile)
          if link is None:
            page.goto(display_href, wait_until="domcontentloaded", timeout=60000)
          else:
            human_click(
              link,
              self.config.action_delay_min,
              self.config.action_delay_max,
              page=page,
              mobile=False,
            )
        try:
          page.wait_for_load_state("domcontentloaded", timeout=60000)
        except Exception:
          pass
        random_delay(self.config.action_delay_min, self.config.action_delay_max)
        return True
      except Exception as exc:
        self.logger(f"[Warmup] Candidate visit failed: {exc}")
        continue

    self.logger("[Warmup] Tried candidates but none were reachable")
    return False

  def _collect_warmup_candidate_hrefs(self, page: Page, *, mobile: bool) -> list[str]:
    selectors = self._organic_link_selectors(mobile)
    hrefs: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
      links = page.locator(selector)
      for index in range(min(links.count(), 25)):
        raw_href = links.nth(index).get_attribute("href") or ""
        if not self._is_clickable_result_href(raw_href):
          continue
        resolved = self._resolve_result_href(raw_href)
        if not resolved or resolved in seen:
          continue
        seen.add(resolved)
        hrefs.append(resolved)
    return hrefs

  def _find_result_link_for_href(self, page: Page, target_href: str, *, mobile: bool):
    target_resolved = self._resolve_result_href(target_href) or target_href
    normalized_target = self._normalize_domain(target_resolved)
    target_path = (urlparse(target_resolved).path or "").lower().rstrip("/")
    exact_match = None
    path_match = None
    domain_match = None
    for selector in self._organic_link_selectors(mobile):
      links = page.locator(selector)
      for index in range(min(links.count(), 140)):
        link = links.nth(index)
        raw_href = link.get_attribute("href") or ""
        resolved = self._resolve_result_href(raw_href)
        if not resolved:
          continue
        resolved_path = (urlparse(resolved).path or "").lower().rstrip("/")
        if resolved == target_resolved or resolved == target_href:
          exact_match = link
          break
        if (
          self._normalize_domain(resolved) == normalized_target
          and target_path
          and (resolved_path == target_path or resolved_path.endswith(target_path))
        ):
          path_match = path_match or link
        elif (
          self._normalize_domain(resolved) == normalized_target
          and not target_path
        ):
          domain_match = domain_match or link
      if exact_match is not None:
        break
    link = exact_match or path_match or domain_match
    if link is None:
      return None
    try:
      link.scroll_into_view_if_needed(timeout=5000)
      page.wait_for_timeout(random.randint(120, 280))
    except Exception:
      pass
    return link

  def _find_mobile_serp_link_relaxed(self, page: Page, target_href: str):
    """Broader SERP anchor lookup when standard organic selectors miss on mobile."""
    target_resolved = self._resolve_result_href(target_href) or target_href
    normalized_target = self._normalize_domain(target_resolved)
    target_path = (urlparse(target_resolved).path or "").lower().rstrip("/")
    if not normalized_target:
      return None

    domain_selectors = (
      f'#rso a[href*="{normalized_target}"]',
      f'div#search a[href*="{normalized_target}"]',
      f'a[href*="{normalized_target}"]',
    )
    for selector in domain_selectors:
      try:
        links = page.locator(selector)
        for index in range(min(links.count(), 60)):
          link = links.nth(index)
          raw_href = link.get_attribute("href") or ""
          resolved = self._resolve_result_href(raw_href)
          if not resolved:
            continue
          if self._href_matches_target(resolved, normalized_target):
            try:
              link.scroll_into_view_if_needed(timeout=5000)
              page.wait_for_timeout(random.randint(120, 280))
            except Exception:
              pass
            return link
          resolved_path = (urlparse(resolved).path or "").lower().rstrip("/")
          if (
            target_path
            and self._normalize_domain(resolved) == normalized_target
            and (resolved_path == target_path or resolved_path.endswith(target_path))
          ):
            try:
              link.scroll_into_view_if_needed(timeout=5000)
              page.wait_for_timeout(random.randint(120, 280))
            except Exception:
              pass
            return link
      except Exception:
        continue

    try:
      cite = page.locator(f'cite:has-text("{normalized_target}")').first
      if cite.count():
        card_link = cite.locator(
          'xpath=ancestor::*[.//a[@href]][1]//a[@href]'
        ).first
        if card_link.count():
          raw_href = card_link.get_attribute("href") or ""
          resolved = self._resolve_result_href(raw_href)
          if resolved and self._href_matches_target(resolved, normalized_target):
            try:
              card_link.scroll_into_view_if_needed(timeout=5000)
              page.wait_for_timeout(random.randint(120, 280))
            except Exception:
              pass
            return card_link
    except Exception:
      pass
    return None

  def _mobile_landed_on_target(self, page: Page) -> bool:
    try:
      host = self._normalize_domain(page.url or "")
    except Exception:
      return False
    return self._host_matches_any_target(host)

  def _clear_mobile_manual_target_signal(self) -> None:
    self._mobile_manual_target_landed = False

  def _try_finish_mobile_manual_target(
    self,
    page: Page,
    profile: ProfileSpec,
    keyword: str,
    page_num: int,
    *,
    stop_event: Optional[threading.Event],
    stopped: Callable[[], bool],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    on_failure: Optional[FailureCallback],
    work_ref: Optional[dict],
    network: Optional[NetworkOptimizer],
    handle_response: Optional[Callable],
    on_target_click: Optional[TargetClickCallback],
    log: Callable[[str], None],
    end_session_after_dwell: bool = False,
  ) -> tuple[int, Page]:
    """Treat a user-initiated target navigation as a successful SERP click."""
    if not self._mobile_landed_on_target(page) and not self._mobile_manual_target_landed:
      return 0, page
    targets = self._get_target_domains()
    matched_domain = self._href_match_target_domain(page.url or "", targets) or ""
    if not matched_domain:
      matched_domain = self._session_target_domain or (targets[0] if targets else "")
    self._clear_mobile_manual_target_signal()
    self.logger(
      f"[Search] Mobile: target site open during scan (page {page_num}) — "
      f"proceeding to dwell ({matched_domain})"
    )
    success, page = self._process_serp_match_click(
      page,
      profile,
      keyword,
      page_num,
      1,
      page.url or "",
      matched_domain,
      stop_event=stop_event,
      stopped=stopped,
      set_status=set_status,
      on_ui_status=on_ui_status,
      on_failure=on_failure,
      work_ref=work_ref,
      network=network,
      handle_response=handle_response,
      on_target_click=on_target_click,
      log=log,
      already_on_target=True,
      end_session_after_dwell=end_session_after_dwell,
    )
    return (1 if success else 0), page

  def _mobile_serp_tap_locator(self, link):
    """Prefer the visible title (h3) inside a SERP result card for CDP taps."""
    try:
      if link.locator("h3").count() > 0:
        return link.locator("h3").first
    except Exception:
      pass
    return link

  def _mobile_click_serp_result_link(self, page: Page, link, href: str) -> bool:
    resolved = self._resolve_result_href(href) or href
    delay_lo = self.config.action_delay_min
    delay_hi = self.config.action_delay_max
    tap_target = self._mobile_serp_tap_locator(link)
    host_hint = self._normalize_domain(resolved) or "target"

    def landed() -> bool:
      try:
        if self._mobile_landed_on_target(page):
          return True
        current = self._resolve_result_href(page.url or "") or (page.url or "")
        if resolved and current and (
          current == resolved
          or self._normalize_domain(current) == self._normalize_domain(resolved)
        ):
          return True
      except Exception:
        pass
      return False

    try:
      if dispatch_serp_anchor_touch_tap(
        page,
        tap_target,
        logger=self.logger,
        label=f"serp-target:{host_hint}",
        landed_check=landed,
        delay_lo=delay_lo,
        delay_hi=delay_hi,
      ):
        return True
    except Exception as exc:
      self.logger(
        f"[Touch] serp-target:{host_hint} touch dispatch error: "
        f"{exc.__class__.__name__}: {exc}"
      )

    if landed():
      self.logger(
        f"[Touch] serp-target:{host_hint} success detected after touch "
        f"(navigation complete, url={(page.url or '')[:100]})"
      )
      return True

    try:
      human_touch_click(
        page,
        tap_target,
        delay_lo,
        delay_hi,
      )
      page.wait_for_timeout(random.randint(450, 950))
      if landed():
        self.logger(f"[Touch] serp-target:{host_hint} success via human_touch_click fallback")
        return True
    except Exception:
      pass

    try:
      link.click(timeout=5000, force=True)
      page.wait_for_timeout(random.randint(450, 950))
      return landed()
    except Exception:
      return False

  def _mobile_open_target_direct(
    self,
    page: Page,
    href: str,
    profile: ProfileSpec,
    keyword: str,
    max_wait_seconds: float,
  ) -> tuple[bool, Page]:
    resolved = self._resolve_result_href(href) or href
    self.logger(
      f"[Target] Mobile direct navigation for '{keyword}': {resolved[:120]}"
    )
    try:
      page = self._safe_goto(
        page,
        resolved,
        profile,
        self.logger,
        wait_until="domcontentloaded",
        timeout=int(max_wait_seconds * 1000),
      )
      page = self._bind_session_page(page)
      return self._mobile_landed_on_target(page), page
    except Exception as exc:
      self.logger(f"[Target] Mobile direct navigation failed for '{keyword}': {exc}")
      return False, page

  @staticmethod
  def _organic_link_selectors(mobile: bool) -> tuple[str, ...]:
    if mobile:
      return (
        "#rso a:has(h3)[href]",
        "div#search a:has(h3)[href]",
        "#rso a[data-ved][href]",
        "div#search a[data-ved][href]",
        '#rso a[href]:has(cite)',
        'div#search a[href]:has(cite)',
        '[data-sokoban-container] a[href^="/url?"]',
        '[data-sokoban-container] a[href^="http"]',
        "div#search a[href^='/url?']",
        "#rso a[href^='/url?']",
        'div#search a[href^="http"]',
        '#rso a[href^="http"]',
      )
    return (
      "#rso a:has(h3)",
      "div#search a:has(h3)",
      "#search .g a[href]",
      'div#search a[href^="http"]',
      '#rso a[href^="http"]',
      "div#search a[href]",
      "#rso a[href]",
    )

  def _click_first_organic_result(
    self,
    page: Page,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
  ) -> bool:
    return self._visit_first_organic_result(page, profile, on_failure)

  def _first_organic_result_link(self, page: Page):
    selectors = (
      "#rso a:has(h3)",
      "div#search a:has(h3)",
      "#search .g a[href]",
      'div#search a[href^="http"]',
      '#rso a[href^="http"]',
    )
    for selector in selectors:
      links = page.locator(selector)
      for index in range(min(links.count(), 15)):
        link = links.nth(index)
        href = link.get_attribute("href") or ""
        if self._is_clickable_result_href(href):
          return link
    self._last_search_exhausted = True
    return None

  def _first_warmup_fallback_link(self, page: Page):
    selectors = (
      "div#search a[href]",
      "#rso a[href]",
      'a[href^="http"]',
    )
    for selector in selectors:
      links = page.locator(selector)
      for index in range(min(links.count(), 40)):
        link = links.nth(index)
        href = link.get_attribute("href") or ""
        if self._is_clickable_result_href(href):
          return link
    return None

  @staticmethod
  def _is_clickable_result_href(href: str) -> bool:
    if not href or href.startswith("#") or href.startswith("javascript:"):
      return False
    lower = href.lower()
    if lower.startswith("/url?"):
      return True
    blocked = (
      "google.com/search",
      "google.com/maps",
      "accounts.google",
      "support.google",
      "policies.google",
      "facebook.com",
      "instagram.com",
      "youtube.com",
      "tiktok.com",
      "x.com",
      "twitter.com",
    )
    return not any(token in lower for token in blocked)

  @staticmethod
  def _resolve_result_href(href: str) -> str:
    if not href:
      return ""
    if href.startswith("/url?"):
      href = f"https://www.google.co.kr{href}"
    parsed = urlparse(href)
    if "google." in parsed.netloc and parsed.path in ("/url", "/imgres"):
      params = parse_qs(parsed.query)
      for key in ("q", "url", "adurl"):
        for raw in params.get(key, []):
          decoded = unquote((raw or "").strip())
          if decoded:
            return decoded
      return href
    return href

  def _log_serp_match_miss(
    self,
    keyword: str,
    page_num: int,
    hrefs: list[str],
    targets: list[str],
  ) -> None:
    sample_hosts: list[str] = []
    for href in hrefs[:12]:
      resolved = self._resolve_result_href(href) or href
      host = self._normalize_domain(resolved) or (resolved or "")[:48]
      if host and host not in sample_hosts:
        sample_hosts.append(host)
    target_keys = [
      self._normalize_domain(target)
      for target in targets
      if self._normalize_domain(target)
    ]
    raw_hit = any(
      any(key in (href or "").lower() for key in target_keys)
      for href in hrefs
    )
    resolved_hit = any(
      self._href_match_target_domain(self._resolve_result_href(href) or href, targets)
      for href in hrefs
    )
    hint = ""
    if raw_hit and not resolved_hit:
      hint = " (target substring in raw hrefs but resolve/match failed)"
    elif not raw_hit:
      hint = " (target substring not in parsed hrefs — cite/DOM fallback may apply)"
    self.logger(
      f"[Search] Page {page_num}: 0 target matches for '{keyword}' "
      f"({len(hrefs)} href(s), hosts: {', '.join(sample_hosts[:8]) or '—'}){hint}"
    )

  def _collect_dom_fallback_target_matches(
    self,
    page: Page,
    clicked_keys: set[str],
    clicked_domains: Optional[set[str]] = None,
    *,
    page_hrefs: Optional[list[str]] = None,
    page_num: int = 1,
  ) -> list[tuple[int, str, str]]:
    """Find target links when href parsing missed a visible mobile/desktop result."""
    targets = self._get_target_domains()
    target_keys = [
      self._normalize_domain(target)
      for target in targets
      if self._normalize_domain(target)
    ]
    if not target_keys:
      return []

    matches: list[tuple[int, str, str]] = []
    seen_dedupe: set[str] = set()

    def append_match(raw_href: str, *, rank_hint: Optional[int] = None) -> None:
      raw = str(raw_href or "").strip()
      if not raw:
        return
      resolved = self._resolve_result_href(raw) or raw
      matched_domain = self._href_match_target_domain(resolved, targets)
      if not matched_domain:
        matched_domain = self._href_match_target_domain(raw, targets)
      if not matched_domain:
        return
      href_for_click = resolved if resolved else raw
      if not self._is_valid_organic_href(href_for_click) and not self._is_valid_organic_href(raw):
        if not any(key in (raw or "").lower() or key in (resolved or "").lower() for key in target_keys):
          return
        href_for_click = resolved or raw
      domain_key = self._target_domain_dedupe_key(matched_domain)
      if clicked_domains is not None and domain_key in clicked_domains:
        return
      dedupe = self._serp_click_dedupe_key(href_for_click)
      if dedupe in clicked_keys or dedupe in seen_dedupe:
        return
      rank = rank_hint
      if rank is None:
        rank = self._page_local_rank_for_href(href_for_click, page_hrefs or [])
      if rank is None:
        rank = len(matches) + 1
      seen_dedupe.add(dedupe)
      matches.append((rank, href_for_click, matched_domain))

    try:
      cite_probe = page.evaluate(
        """(targetKeys) => {
          const out = [];
          const seen = new Set();
          let visibleTargetCount = 0;
          let unresolvedTargetCount = 0;
          const normalizeDomain = (value) => (value || '')
            .toLowerCase()
            .replace(/[\\u200b-\\u200d\\ufeff]/g, '')
            .replace(/\\s+/g, '')
            .replace(/^https?:\\/\\//, '')
            .replace(/^www\\./, '')
            .split('/')[0]
            .trim();
          const domainMatches = (text) => {
            const normalized = normalizeDomain(text);
            if (!normalized) return false;
            return targetKeys.some((key) => (
              normalized === key
              || normalized.endsWith('.' + key)
              || normalized.includes(key)
            ));
          };
          const usableHref = (anchor) => {
            if (!anchor) return '';
            const href = (anchor.getAttribute('href') || '').trim();
            if (!href || href.startsWith('#') || href.startsWith('javascript:')) return '';
            return href;
          };
          const hrefMatches = (href) => {
            const lowered = (href || '').toLowerCase();
            return targetKeys.some((key) => lowered.includes(key));
          };
          const findCardAnchor = (node) => {
            // Mobile SERPs often render the displayed domain inside the same anchor.
            const direct = node.closest('a[href]');
            const directHref = usableHref(direct);
            if (directHref && hrefMatches(directHref)) return direct;

            // Do not stop at the nearest data-ved node: it is often only a tiny
            // child wrapper. Walk upward until the displayed domain and title
            // link share a result-card ancestor.
            let parent = node.parentElement;
            for (let depth = 0; parent && depth < 14; depth++, parent = parent.parentElement) {
              const anchors = Array.from(parent.querySelectorAll('a[href]'));
              const targetHrefAnchor = anchors.find((anchor) => {
                const href = usableHref(anchor);
                return href && hrefMatches(href);
              });
              if (targetHrefAnchor) return targetHrefAnchor;

              const titleAnchors = anchors.filter((anchor) => (
                usableHref(anchor)
                && (anchor.querySelector('h3, .LC20lb') || anchor.closest('h3'))
              ));
              if (titleAnchors.length === 1) return titleAnchors[0];
              if (titleAnchors.length > 1) {
                const withDomain = titleAnchors.find((anchor) => hrefMatches(usableHref(anchor)));
                if (withDomain) return withDomain;
                return titleAnchors[0];
              }

              // A compact card with one external-looking link is also safe.
              const cardText = (parent.innerText || parent.textContent || '');
              if (cardText.length <= 1600 && domainMatches(cardText)) {
                const external = anchors.filter((anchor) => {
                  const href = usableHref(anchor);
                  if (!href) return false;
                  try {
                    const url = new URL(href, window.location.href);
                    const host = (url.hostname || '').toLowerCase();
                    return !host.includes('google.') || url.pathname === '/url';
                  } catch (e) {
                    return false;
                  }
                });
                if (external.length === 1) return external[0];
                if (external.length > 1) {
                  const preferred = external.find((anchor) => hrefMatches(usableHref(anchor)));
                  return preferred || external[0];
                }
              }
            }
            return null;
          };
          const citeSelectors = [
            '#rso cite',
            'div#search cite',
            '[data-sokoban-container] cite',
            'cite',
            '.iUh30',
            'span[data-dt]',
            '.vvjwJb',
            'div[role="link"] span',
          ];
          const citeNodes = new Set();
          for (const selector of citeSelectors) {
            document.querySelectorAll(selector).forEach((el) => citeNodes.add(el));
          }
          // Google changes the mobile displayed-URL element frequently. Add
          // minimal leaf nodes whose own visible text contains the assigned domain.
          const roots = document.querySelectorAll('#rso, div#search, [data-sokoban-container]');
          const scopes = roots.length ? Array.from(roots) : [document.body];
          for (const root of scopes) {
            for (const node of root.querySelectorAll('a, cite, span, div')) {
              const text = (node.innerText || node.textContent || '').trim();
              if (!text || text.length > 260 || !domainMatches(text)) continue;
              const childHasTarget = Array.from(node.children || []).some((child) => {
                const childText = (child.innerText || child.textContent || '').trim();
                return childText && domainMatches(childText);
              });
              if (!childHasTarget) citeNodes.add(node);
            }
          }
          for (const node of citeNodes) {
            const text = (node.innerText || node.textContent || '');
            if (!domainMatches(text)) continue;
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            if (
              style.display === 'none'
              || style.visibility === 'hidden'
              || rect.width < 1
              || rect.height < 1
            ) continue;
            visibleTargetCount += 1;
            const anchor = findCardAnchor(node);
            if (!anchor) {
              unresolvedTargetCount += 1;
              continue;
            }
            const href = usableHref(anchor);
            if (!href) {
              unresolvedTargetCount += 1;
              continue;
            }
            if (seen.has(href)) continue;
            seen.add(href);
            out.push(href);
          }
          return { hrefs: out, visibleTargetCount, unresolvedTargetCount };
        }""",
        target_keys,
      )
      cite_hrefs = (
        cite_probe.get("hrefs", [])
        if isinstance(cite_probe, dict)
        else (cite_probe or [])
      )
      for raw_href in cite_hrefs:
        before = len(matches)
        append_match(str(raw_href))
        if len(matches) == before:
          # Visible cite matched the domain, but href host matching failed
          # (odd /url wrappers). Still accept the card link for click.
          raw = str(raw_href or "").strip()
          if not raw:
            continue
          resolved = self._resolve_result_href(raw) or raw
          forced = (self._session_target_domain or "").strip() or (
            targets[0] if targets else ""
          )
          if not forced:
            continue
          href_for_click = resolved if resolved else raw
          domain_key = self._target_domain_dedupe_key(forced)
          if clicked_domains is not None and domain_key in clicked_domains:
            continue
          dedupe = self._serp_click_dedupe_key(href_for_click)
          if dedupe in clicked_keys or dedupe in seen_dedupe:
            continue
          rank = self._page_local_rank_for_href(href_for_click, page_hrefs or [])
          if rank is None:
            rank = len(matches) + 1
          seen_dedupe.add(dedupe)
          matches.append((rank, href_for_click, forced))
    except Exception as exc:
      self.logger(f"[Search] Page {page_num}: displayed-domain probe warning — {exc}")

    try:
      raw_hrefs = page.evaluate(
        """(targetKeys) => {
          const out = [];
          const seen = new Set();
          const roots = document.querySelectorAll(
            '#rso, div#search, [data-sokoban-container]'
          );
          const scope = roots.length ? Array.from(roots) : [document.body];
          for (const root of scope) {
            for (const anchor of root.querySelectorAll('a[href]')) {
              const href = anchor.getAttribute('href') || '';
              if (!href || href.startsWith('#') || href.startsWith('javascript:')) {
                continue;
              }
              const card = anchor.closest(
                '[data-sokoban-container], .g, div[data-hveid], [data-ved]'
              ) || anchor.parentElement;
              const cardText = ((card && card.innerText) || anchor.innerText || '')
                .toLowerCase();
              const hrefLower = href.toLowerCase();
              let matched = false;
              for (const key of targetKeys) {
                if (cardText.includes(key) || hrefLower.includes(key)) {
                  matched = true;
                  break;
                }
              }
              if (!matched || seen.has(href)) {
                continue;
              }
              seen.add(href);
              out.push(href);
            }
          }
          return out;
        }""",
        target_keys,
      )
    except Exception:
      raw_hrefs = []

    for raw_href in raw_hrefs or []:
      append_match(str(raw_href))

    if matches:
      self.logger(
        f"[Search] DOM cite/text fallback found {len(matches)} target match(es) "
        f"on page {page_num}"
      )
    return matches

  def _get_target_domains(self) -> list[str]:
    if (self._session_target_domain or "").strip():
      return [self._session_target_domain.strip()]
    return self.config.get_target_domains()

  @staticmethod
  def _apply_session_target_hosts(network: NetworkOptimizer, domain: str) -> None:
    normalized = (domain or "").lower().removeprefix("www.").strip()
    if not normalized:
      return
    network.target_hosts = [normalized]
    network.target_host = normalized

  def _host_matches_any_target(self, host: str) -> bool:
    normalized_host = (host or "").lower().removeprefix("www.")
    if not normalized_host:
      return False
    for domain in self._get_target_domains():
      target = self._normalize_domain(domain)
      if target and (normalized_host == target or normalized_host.endswith(f".{target}")):
        return True
    return False

  def _href_match_target_domain(self, href: str, targets: list[str]) -> Optional[str]:
    for target in targets:
      if self._href_matches_target(href, target):
        return target
    return None

  def _serp_click_dedupe_key(self, href: str) -> str:
    resolved = self._resolve_result_href(href) or href
    try:
      parsed = urlparse(resolved)
      host = (parsed.netloc or "").lower().removeprefix("www.")
      path = (parsed.path or "/").rstrip("/") or "/"
      query = parsed.query or ""
      return f"{host}{path}?{query}" if query else f"{host}{path}"
    except Exception:
      return (resolved or href).strip().lower()

  @staticmethod
  def _target_domain_dedupe_key(matched_domain: str) -> str:
    return (matched_domain or "").strip().lower().removeprefix("www.")

  @classmethod
  def _global_organic_rank_to_page_local(cls, global_rank: int) -> tuple[int, int]:
    """Convert 1-based cumulative SERP position to (page, rank_on_page)."""
    global_rank = max(1, int(global_rank))
    page_size = cls._MOBILE_SERP_RESULTS_PER_PAGE
    page_num = (global_rank - 1) // page_size + 1
    local_rank = (global_rank - 1) % page_size + 1
    return page_num, local_rank

  @classmethod
  def _overall_rank_from_page_local(cls, page_num: int, local_rank: int) -> int:
    page_num = max(1, int(page_num))
    local_rank = max(1, int(local_rank))
    return (page_num - 1) * cls._MOBILE_SERP_RESULTS_PER_PAGE + local_rank

  @classmethod
  def _slice_hrefs_for_mobile_serp_page(
    cls,
    result_hrefs: list[str],
    page_num: int,
  ) -> list[str]:
    """Return hrefs for the current mobile SERP page (cumulative scroll vs start= URL)."""
    page_num = max(1, int(page_num))
    page_size = cls._MOBILE_SERP_RESULTS_PER_PAGE
    base = (page_num - 1) * page_size

    if not result_hrefs:
      return []

    # Page 1 may include a few extra modules — still scan every parsed organic link.
    if page_num == 1 and len(result_hrefs) <= page_size + 8:
      return result_hrefs

    # Current page window within cumulative infinite-scroll DOM.
    if base < len(result_hrefs):
      return result_hrefs[base:base + page_size]

    # start=10/20 URL pagination: DOM usually has only the current page (~10 links).
    return result_hrefs[:page_size]

  @classmethod
  def _page_local_rank_for_href(
    cls,
    href: str,
    page_hrefs: list[str],
  ) -> Optional[int]:
    """Map a matched href to its 1-based rank within the current SERP page window."""
    if not page_hrefs:
      return None
    target_key = cls._serp_click_dedupe_key_static(href)
    target_domain = cls._normalize_domain_static(href)
    for index, candidate in enumerate(page_hrefs, start=1):
      candidate_key = cls._serp_click_dedupe_key_static(candidate)
      if candidate_key and candidate_key == target_key:
        return index
      if (
        target_domain
        and target_domain == cls._normalize_domain_static(candidate)
      ):
        return index
    return None

  @staticmethod
  def _serp_click_dedupe_key_static(href: str) -> str:
    resolved = SerpBot._resolve_result_href(href) or href
    try:
      parsed = urlparse(resolved)
      host = (parsed.netloc or "").lower().removeprefix("www.")
      path = (parsed.path or "").rstrip("/").lower()
      return f"{host}{path}"
    except Exception:
      return (resolved or href).strip().lower()

  @staticmethod
  def _normalize_domain_static(value: str) -> str:
    resolved = SerpBot._resolve_result_href(value) or value
    try:
      host = urlparse(resolved).netloc or resolved
    except Exception:
      host = value
    return (host or "").lower().removeprefix("www.").strip()

  def _escape_mobile_target_to_serp(
    self,
    page: Page,
    keyword: str,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
    log: Callable[[str], None],
    *,
    before_serp_url: str = "",
  ) -> Page:
    page = self._bind_session_page(page)
    if self._is_on_google_serp(page) and self._is_on_serp_for_keyword(page, keyword):
      return self._wait_for_serp_stable(page, log, timeout_seconds=8.0)
    if self._mobile_landed_on_target(page) or not self._is_on_google_serp(page):
      log("[Search] Mobile: leaving accidental target/navigation without dwell")
      page = self._history_back(
        page,
        log,
        timeout_ms=15000,
        success_check=lambda: self._is_on_google_serp(page),
        warning_label="Mobile escape go_back",
      )
    if not self._is_on_google_serp(page) and before_serp_url and self._is_google_serp_url(before_serp_url):
      try:
        page.goto(before_serp_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(random.randint(300, 600))
      except Exception as exc:
        log(f"[Search] Mobile SERP URL restore warning: {exc}")
    if not self._is_on_google_serp(page):
      page = self._recover_google_serp_before_search(page, keyword, profile, on_failure)
    return self._wait_for_serp_stable(page, log, timeout_seconds=8.0)

  def _collect_target_matches_on_page(
    self,
    result_hrefs: list[str],
    clicked_keys: set[str],
    clicked_domains: Optional[set[str]] = None,
  ) -> list[tuple[int, str, str]]:
    targets = self._get_target_domains()
    matches: list[tuple[int, str, str]] = []
    for rank, href in enumerate(result_hrefs, start=1):
      matched_domain = self._href_match_target_domain(href, targets)
      if not matched_domain:
        continue
      domain_key = self._target_domain_dedupe_key(matched_domain)
      if clicked_domains is not None and domain_key in clicked_domains:
        continue
      dedupe = self._serp_click_dedupe_key(href)
      if dedupe in clicked_keys:
        continue
      matches.append((rank, href, matched_domain))
    return matches

  def _process_serp_match_click(
    self,
    page: Page,
    profile: ProfileSpec,
    keyword: str,
    page_num: int,
    rank: int,
    href: str,
    matched_domain: str,
    *,
    stop_event: Optional[threading.Event],
    stopped: Callable[[], bool],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    on_failure: Optional[FailureCallback],
    work_ref: Optional[dict],
    network: Optional[NetworkOptimizer],
    handle_response: Optional[Callable],
    on_target_click: Optional[TargetClickCallback],
    log: Callable[[str], None],
    already_on_target: bool = False,
    end_session_after_dwell: bool = False,
  ) -> tuple[bool, Page]:
    self._set_network_phase(PagePhase.TARGET_SITE, keyword, page)
    if work_ref is not None:
      work_ref["allow_target_tab_until"] = time.time() + 70.0
    mobile = self._is_mobile_profile(profile)
    serp_tab: Optional[Page] = page if not mobile else None
    if mobile and work_ref is not None and not already_on_target:
      try:
        serp_url = (page.url or "").strip()
        if self._is_google_serp_url(serp_url):
          work_ref["serp_resume_url"] = serp_url
      except Exception:
        pass
    if already_on_target:
      page = self._bind_session_page(page)
      opened = self._mobile_landed_on_target(page)
      if not opened:
        return False, page
      log(
        f"[Target] Mobile target already open ({(page.url or '')[:100]}) — proceeding to dwell"
      )
    else:
      opened, page, serp_tab = self._open_target_from_serp_click(
        page,
        href,
        keyword=keyword,
        profile=profile,
        stop_event=stop_event,
        stopped=stopped,
        set_status=set_status,
        on_ui_status=on_ui_status,
        max_wait_seconds=45.0,
        work_ref=work_ref,
        matched_domain=matched_domain,
      )
    if stopped():
      return False, page
    if not opened:
      self._last_target_open_failed = True
      log(
        f"[Target] Failed open target site: '{keyword}' → {matched_domain}"
      )
      return False, page

    if self._session_wire_meter is not None:
      self._session_wire_meter.mark_site_visit_started()

    if network is not None and handle_response is not None:
      self._attach_response_handler(page, handle_response)
      page = self._bind_session_page(page)
      network.reattach_page(page, mobile=mobile)
      network.apply_phase_headers(page)

    guard, page = self._ensure_captcha_clear(
      page, stop_event, set_status, on_ui_status, profile, log, context="post-target-open",
    )
    if guard in self._captcha_abort_values():
      return False, page

    visited_url = href or ""
    try:
      visited_url = (page.url or href or "").strip()
    except Exception:
      pass

    total_rank = self._overall_rank_from_page_local(page_num, rank)
    self.result_store.upsert(
      keyword=keyword,
      site=matched_domain,
      device=profile.device_label,
      page=page_num,
      rank=rank,
      mobile=mobile,
    )
    if not self.csv.log(keyword, matched_domain, page_num, rank, profile.name):
      log(
        f"[Target] results.csv is locked — click logged to result.csv only "
        f"({matched_domain}, page {page_num})"
      )
    if self._session_click_log:
      self._session_click_log.log(
        profile_name=profile.name,
        device=profile.device_label,
        keyword=keyword,
        url=visited_url,
        page=page_num,
        rank=rank,
        overall_rank=total_rank,
        site=matched_domain,
      )
    if on_target_click:
      on_target_click()
    self.keyword_history.log(
      keyword, page_num, rank, total_rank, mobile=mobile,
    )
    log(f"Found {matched_domain} at page {page_num}, rank {rank} for '{keyword}'")
    set_status(ProfileStatus.VISITING_SITE, detail=matched_domain)
    dwell_seconds = random.uniform(self.config.dwell_min, self.config.dwell_max)
    log(
      f"[Target] Dwelling on site: '{keyword}' → {matched_domain} "
      f"for {dwell_seconds:.0f}s"
    )
    self._dwell_on_site(page, dwell_seconds, stopped, profile, network=network)
    if end_session_after_dwell:
      log("[Target] Dwell complete — ending session (no SERP return)")
      return True, page
    set_status(ProfileStatus.SEARCHING, detail=keyword)
    self._set_network_phase(PagePhase.GOOGLE_SERP, keyword, page)

    if serp_tab and not mobile:
      try:
        if page != serp_tab and not page.is_closed():
          page.close()
          log("[Target] Closed target tab — returning to SERP")
      except Exception as exc:
        log(f"[Target] Close target tab warning: {exc}")
      page = serp_tab
      if work_ref is not None:
        work_ref["page"] = page
        work_ref["serp_page"] = serp_tab
      try:
        page.bring_to_front()
      except Exception:
        pass
      if network is not None:
        network.reattach_page(page, mobile=False)
        network.apply_phase_headers(page)
    elif mobile:
      page = self._ensure_mobile_serp_after_target_visit(
        page,
        keyword,
        profile,
        on_failure,
        work_ref,
        log,
      )
      if network is not None:
        network.reattach_page(page, mobile=True)
        network.apply_phase_headers(page)
      log("[Target] Returned to SERP after dwell (mobile)")

    if work_ref is not None:
      work_ref["allow_target_tab_until"] = 0.0
    return True, page

  def _go_back_after_internal_link(
    self,
    page: Page,
    stopped: Callable[[], bool],
    *,
    landing_url: str = "",
  ) -> Page:
    """Return to the target landing page after browsing one internal URL."""
    if stopped():
      return page
    landing = (landing_url or "").strip()

    def _on_landing() -> bool:
      if not landing:
        return False
      try:
        return self._urls_match_for_back(landing, page.url or "")
      except Exception:
        return False

    page = self._history_back(
      page,
      self.logger,
      timeout_ms=15000,
      success_check=_on_landing if landing else None,
      success_log="[Target] Returned to landing page after internal link (go_back)",
      warning_label="Internal link go_back",
    )
    if landing and not _on_landing() and not stopped():
      page = self._history_back(
        page,
        self.logger,
        timeout_ms=12000,
        success_check=_on_landing,
        success_log="[Target] Returned to landing page after internal link (go_back, retry)",
        warning_label="Internal link go_back retry",
      )
    return page

  def _go_back_to_mobile_serp(
    self,
    page: Page,
    keyword: str,
    log: Callable[[str], None],
    *,
    max_steps: int = 5,
  ) -> Page:
    """Walk browser history back until the keyword SERP is open again."""
    page = self._bind_session_page(page)
    if self._is_on_serp_for_keyword(page, keyword):
      return page

    for step in range(1, max(1, int(max_steps)) + 1):
      if self._is_on_serp_for_keyword(page, keyword):
        break

      def _serp_restored() -> bool:
        return (
          self._is_on_serp_for_keyword(page, keyword)
          or (
            self._is_on_google_serp(page)
            and not self._mobile_landed_on_target(page)
          )
        )

      page = self._history_back(
        page,
        log,
        timeout_ms=20000,
        success_check=_serp_restored,
        success_log=f"[Search] Mobile SERP restored via back navigation (step {step})",
        warning_label=f"Mobile SERP back step {step}",
      )
      if _serp_restored():
        break
    return page

  def _ensure_mobile_serp_after_target_visit(
    self,
    page: Page,
    keyword: str,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
    work_ref: Optional[dict],
    log: Callable[[str], None],
  ) -> Page:
    """Re-sync Google SERP after a mobile target dwell so pagination can continue."""
    page = self._bind_session_page(page)
    resume_url = ""
    if work_ref is not None:
      resume_url = str(work_ref.get("serp_resume_url") or "").strip()

    def _finalize() -> Page:
      page_local = self._wait_for_serp_stable(page, log, timeout_seconds=8.0)
      if work_ref is not None:
        work_ref["page"] = page_local
      self._sync_mobile_serp_page(page_local, profile)
      self._set_network_phase(PagePhase.GOOGLE_SERP, keyword, page_local)
      return page_local

    try:
      if self._is_on_serp_for_keyword(page, keyword):
        return _finalize()

      page = self._go_back_to_mobile_serp(page, keyword, log, max_steps=5)
      if self._is_on_serp_for_keyword(page, keyword) or self._is_on_google_serp(page):
        return _finalize()

      if resume_url and self._is_google_serp_url(resume_url):
        try:
          page.goto(resume_url, wait_until="domcontentloaded", timeout=20000)
          page.wait_for_timeout(random.randint(300, 600))
          if self._is_on_google_serp(page):
            log("[Search] Mobile SERP restored via saved URL (go_back fallback)")
            return _finalize()
        except Exception as exc:
          log(f"[Search] Mobile SERP URL restore warning: {exc}")

      log("[Search] Mobile SERP restore failed — recovering Google entry as last resort")
      page = self._recover_google_serp_before_search(page, keyword, profile, on_failure)
      return _finalize()
    except Exception as exc:
      log(f"[Search] Mobile SERP re-sync after target visit warning: {exc}")
      try:
        page = self._go_back_to_mobile_serp(page, keyword, log, max_steps=3)
        if not self._is_on_google_serp(page):
          if resume_url and self._is_google_serp_url(resume_url):
            page.goto(resume_url, wait_until="domcontentloaded", timeout=20000)
          else:
            page = self._recover_google_serp_before_search(page, keyword, profile, on_failure)
      except Exception:
        pass
      return _finalize()

  def _process_page_target_matches(
    self,
    page: Page,
    profile: ProfileSpec,
    keyword: str,
    served_page_num: int,
    result_hrefs: list[str],
    clicked_keys: set[str],
    *,
    stop_event: Optional[threading.Event],
    stopped: Callable[[], bool],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    on_failure: Optional[FailureCallback],
    work_ref: Optional[dict],
    network: Optional[NetworkOptimizer],
    handle_response: Optional[Callable],
    on_target_click: Optional[TargetClickCallback],
    log: Callable[[str], None],
    clicked_domains: Optional[set[str]] = None,
    mobile_page_hrefs: Optional[list[str]] = None,
    end_session_after_dwell: bool = False,
  ) -> tuple[int, Page]:
    hrefs_for_rank = mobile_page_hrefs if mobile_page_hrefs is not None else result_hrefs
    targets = self._get_target_domains()
    matched_via_full_cumulative = False
    matches: list[tuple[int, str, str]] = []
    if mobile_page_hrefs is not None:
      self._scroll_target_into_view_if_present(page)
      dom_hrefs = result_hrefs or mobile_page_hrefs
      matches = self._collect_dom_fallback_target_matches(
        page,
        clicked_keys,
        clicked_domains,
        page_hrefs=dom_hrefs,
        page_num=served_page_num,
      )
    if not matches:
      matches = self._collect_target_matches_on_page(
        hrefs_for_rank, clicked_keys, clicked_domains,
      )
    if not matches and mobile_page_hrefs is not None and result_hrefs:
      matches = self._collect_target_matches_on_page(
        result_hrefs, clicked_keys, clicked_domains,
      )
      if matches:
        matched_via_full_cumulative = True
        self.logger(
          f"[Search] Page {served_page_num}: target found in full SERP DOM "
          f"(outside page window slice)"
        )
    if not matches:
      dom_hrefs = result_hrefs or hrefs_for_rank
      matches = self._collect_dom_fallback_target_matches(
        page,
        clicked_keys,
        clicked_domains,
        page_hrefs=dom_hrefs,
        page_num=served_page_num,
      )
    clicks = 0
    if not matches:
      self._log_serp_match_miss(keyword, served_page_num, hrefs_for_rank, targets)
      return 0, page
    self.logger(
      f"[Search] Page {served_page_num}: {len(matches)} target match(es) for '{keyword}'"
    )
    for rank, href, matched_domain in matches:
      if stopped():
        break
      if matched_via_full_cumulative:
        click_page, click_rank = self._global_organic_rank_to_page_local(rank)
        self.logger(
          f"[Search] Global SERP position {rank} → page {click_page}, "
          f"rank {click_rank} for '{keyword}'"
        )
      else:
        click_page, click_rank = served_page_num, rank
      dedupe = self._serp_click_dedupe_key(href)
      success, page = self._process_serp_match_click(
        page,
        profile,
        keyword,
        click_page,
        click_rank,
        href,
        matched_domain,
        stop_event=stop_event,
        stopped=stopped,
        set_status=set_status,
        on_ui_status=on_ui_status,
        on_failure=on_failure,
        work_ref=work_ref,
        network=network,
        handle_response=handle_response,
        on_target_click=on_target_click,
        log=log,
        end_session_after_dwell=end_session_after_dwell,
      )
      if success:
        clicked_keys.add(dedupe)
        if clicked_domains is not None:
          clicked_domains.add(self._target_domain_dedupe_key(matched_domain))
        clicks += 1
        break
      self._last_target_open_failed = True
      break
    return clicks, page

  def _scan_keyword_serp(
    self,
    page: Page,
    profile: ProfileSpec,
    keyword: str,
    stop_event: Optional[threading.Event],
    stopped: Callable[[], bool],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    on_failure: Optional[FailureCallback],
    log: Callable[[str], None],
    *,
    submit_search: bool = True,
    search_method: Optional[str] = None,
    work_ref: Optional[dict] = None,
    network: Optional[NetworkOptimizer] = None,
    handle_response: Optional[Callable] = None,
    on_target_click: Optional[TargetClickCallback] = None,
    end_session_after_dwell: bool = False,
  ) -> int:
    self._last_search_exhausted = False
    self._last_search_exhaustion_eligible = False
    self._last_target_open_failed = False
    self._set_network_phase(PagePhase.GOOGLE_SERP, keyword, page)
    max_pages = max(1, int(self.config.max_search_pages))
    clicked_keys: set[str] = set()
    clicked_domains: set[str] = set()
    total_clicks = 0

    guard, page = self._guard_captcha(
      page, stop_event, set_status, on_ui_status, profile, log, context="search-results",
    )
    if guard in ("stopped", "error", "blocked"):
      return 0
    set_status(ProfileStatus.SEARCHING, detail=keyword)
    mobile = self._is_mobile_profile(profile)
    if submit_search:
      method = search_method or "auto"
      page = self._google_search(page, keyword, profile, on_failure, method=method)
    else:
      log(f"[Search] Continuing SERP scan for '{keyword}' without re-typing")
      page = self._wait_for_serp_stable(page, log)
    if mobile:
      if submit_search:
        self._reset_mobile_serp_page()
      else:
        self._sync_mobile_serp_page(page, profile)
    guard, page = self._resolve_captcha_after_navigation(
      page, stop_event, set_status, on_ui_status, profile, log, context="post-google-search",
    )
    if guard in self._captcha_abort_values():
      return 0

    if mobile:
      return self._scan_keyword_serp_mobile(
        page,
        profile,
        keyword,
        max_pages,
        stop_event,
        stopped,
        set_status,
        on_ui_status,
        on_failure,
        log,
        submit_search=submit_search,
        clicked_keys=clicked_keys,
        clicked_domains=clicked_domains,
        work_ref=work_ref,
        network=network,
        handle_response=handle_response,
        on_target_click=on_target_click,
        end_session_after_dwell=end_session_after_dwell,
      )

    visited_result_pages: set[int] = set()
    serp_last_page: Optional[int] = None
    pages_with_results = 0
    current_page: Optional[int] = None
    current_page = self._serp_page_num(page, profile)
    visible_serp_last_page = self._update_serp_last_page(
      page, current_page, None, max_pages, profile, log, history_page=None,
    )
    serp_last_page = visible_serp_last_page
    history_page = self.result_store.get_page_hint(
      keyword,
      self._session_target_domain or self.config.primary_target_domain,
      mobile=False,
    )
    effective_cap = min(max_pages, serp_last_page) if serp_last_page else max_pages
    additional_pages = build_desktop_page_order(history_page, effective_cap, max_pages)
    search_order = [1] + additional_pages
    self.logger(
      f"[Search] Desktop scan for '{keyword}' → {self._session_target_domain}: "
      f"page 1 first, then {additional_pages or 'no extra pages'} "
      f"(history page {history_page or 'none'}, SERP cap {effective_cap})"
    )

    for page_num in search_order:
      if stopped():
        return total_clicks

      effective_cap = min(max_pages, serp_last_page) if serp_last_page else max_pages
      if page_num > effective_cap:
        self.logger(
          f"[Search] Stop at page {page_num}; Google results end at page {effective_cap} "
          f"for '{keyword}'"
        )
        break

      if current_page != page_num:
        if not self._navigate_to_search_page(
          page, keyword, page_num, profile, stop_event, set_status, on_ui_status, on_failure, log,
        ):
          return total_clicks
        current_page = self._serp_page_num(page, profile)
        if current_page != page_num:
          self.logger(
            f"[Search] Desktop: landed on page {current_page}, expected {page_num} "
            f"for '{keyword}' — skipping page (no direct URL)"
          )
          continue

      served_page_num = current_page
      if served_page_num in visited_result_pages:
        if page_num > served_page_num:
          self.logger(
            f"[Search] Desktop: cannot reach page {page_num} from {served_page_num} "
            f"for '{keyword}' — skipping (no direct URL)"
          )
          continue
        if served_page_num in visited_result_pages:
          serp_last_page = min(serp_last_page or served_page_num, served_page_num)
          continue
      visited_result_pages.add(served_page_num)

      self._serp_micro_scroll(page, profile, times=1)
      serp_last_page = self._update_serp_last_page(
        page, served_page_num, serp_last_page, max_pages, profile, log, history_page=None,
      )
      visible_serp_last_page = min(max_pages, max(visible_serp_last_page or 1, serp_last_page or 1))

      result_hrefs = self._collect_organic_result_hrefs_with_retry(
        page, served_page_num, profile, stop_event, set_status, on_ui_status, log,
      )
      if result_hrefs is None:
        return total_clicks
      if not result_hrefs:
        if served_page_num == 1:
          self.logger(
            f"[Search] Page 1 returned no parseable results for '{keyword}' "
            "(transient/block — keyword kept in list)."
          )
          return total_clicks
        serp_last_page = min(serp_last_page or max(served_page_num - 1, 1), max(served_page_num - 1, 1))
        visible_serp_last_page = min(visible_serp_last_page or serp_last_page, serp_last_page)
        continue

      pages_with_results += 1
      self.logger(
        f"[Search] Page {served_page_num}: parsed {len(result_hrefs)} organic link(s) for '{keyword}'"
      )
      page_clicks, page = self._process_page_target_matches(
        page,
        profile,
        keyword,
        served_page_num,
        result_hrefs,
        clicked_keys,
        stop_event=stop_event,
        stopped=stopped,
        set_status=set_status,
        on_ui_status=on_ui_status,
        on_failure=on_failure,
        work_ref=work_ref,
        network=network,
        handle_response=handle_response,
        on_target_click=on_target_click,
        log=log,
        end_session_after_dwell=end_session_after_dwell,
      )
      total_clicks += page_clicks
      if work_ref is not None:
        work_ref["page"] = page

      if total_clicks > 0:
        self.logger(
          f"[Search] Desktop: target visited on page {served_page_num} for '{keyword}' "
          "— stopping SERP scan"
        )
        break

      if self._last_target_open_failed:
        self.logger(
          f"[Search] Desktop: stopping scan after failed target open for '{keyword}'"
        )
        break

      if not self._has_next_serp_page(page, profile, log=log):
        detected_end = self._detect_serp_last_page(page, served_page_num, log=log)
        if detected_end and detected_end <= served_page_num:
          serp_last_page = min(serp_last_page or served_page_num, served_page_num)
          pending_pages = pending_desktop_scan_pages(
            search_order, visited_result_pages, effective_cap,
          )
          if pending_pages:
            self.logger(
              f"[Search] Google SERP ends at page {serp_last_page} for '{keyword}' "
              f"— continuing planned scan for page(s) {pending_pages}"
            )
          else:
            self.logger(
              f"[Search] No further Google pages after page {serp_last_page} for '{keyword}'"
            )
            break

    final_cap = min(max_pages, serp_last_page) if serp_last_page else max_pages
    reached_configured_max = max_pages in visited_result_pages
    scanned_all_planned = not pending_desktop_scan_pages(
      search_order, visited_result_pages, final_cap,
    )
    self._last_search_exhaustion_eligible = (
      pages_with_results > 0
      and (reached_configured_max or scanned_all_planned)
      and not self._last_target_open_failed
    )
    self._last_search_exhausted = self._last_search_exhaustion_eligible
    if self._last_search_exhaustion_eligible and total_clicks == 0:
      limit_note = (
        f"configured page limit {max_pages}"
        if reached_configured_max
        else f"planned pages through {final_cap}"
      )
      self.logger(
        f"[Search] {limit_note} reached for '{keyword}' "
        "but no target domains were clicked."
      )
    return total_clicks

  @staticmethod
  def _mobile_effective_page_cap(
    *,
    served_page_num: int,
    configured_max: int,
    pagination_available: bool,
  ) -> int:
    """Mobile page bound: config max while 더보기/> exist, else current page is Google's last."""
    configured_max = max(1, int(configured_max))
    served_page_num = max(1, int(served_page_num))
    if pagination_available:
      return configured_max
    return min(configured_max, served_page_num)

  def _estimate_mobile_serp_cap(
    self,
    page: Page,
    served_page_num: int,
    configured_max: int,
    profile: ProfileSpec,
  ) -> int:
    pagination_available = self._mobile_serp_pagination_available(page, profile, strict=True)
    cap = self._mobile_effective_page_cap(
      served_page_num=served_page_num,
      configured_max=configured_max,
      pagination_available=pagination_available,
    )
    return cap

  def _scan_keyword_serp_mobile(
    self,
    page: Page,
    profile: ProfileSpec,
    keyword: str,
    max_pages: int,
    stop_event: Optional[threading.Event],
    stopped: Callable[[], bool],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    on_failure: Optional[FailureCallback],
    log: Callable[[str], None],
    *,
    submit_search: bool,
    clicked_keys: set[str],
    clicked_domains: set[str],
    work_ref: Optional[dict],
    network: Optional[NetworkOptimizer],
    handle_response: Optional[Callable],
    on_target_click: Optional[TargetClickCallback],
    end_session_after_dwell: bool = False,
  ) -> int:
    self._mobile_serp_end_reached = False
    self._clear_mobile_manual_target_signal()
    configured_max = max(1, int(max_pages))
    total_clicks = 0
    self.logger(
      f"[Search] Mobile sequential scan for '{keyword}' → {self._session_target_domain}: "
      f"pages 1→{configured_max} (stop early only when Google has no 더보기/>)"
    )

    pages_with_results = 0
    page = self._wait_for_serp_stable(page, log, timeout_seconds=10.0)
    confirmed_hrefs: list[str] = []
    scan_incomplete = False

    page_num = 1
    pagination_failures = 0
    max_pagination_failures = 5
    while page_num <= configured_max:
      if stopped():
        return total_clicks

      if self._mobile_serp_end_reached:
        self.logger(
          f"[Search] Mobile: SERP end reached before page {page_num} "
          f"(config={configured_max})"
        )
        break

      page = self._wait_for_serp_stable(page, log, timeout_seconds=8.0)
      manual_clicks, page = self._try_finish_mobile_manual_target(
        page,
        profile,
        keyword,
        page_num,
        stop_event=stop_event,
        stopped=stopped,
        set_status=set_status,
        on_ui_status=on_ui_status,
        on_failure=on_failure,
        work_ref=work_ref,
        network=network,
        handle_response=handle_response,
        on_target_click=on_target_click,
        log=log,
        end_session_after_dwell=end_session_after_dwell,
      )
      if manual_clicks > 0:
        if work_ref is not None:
          work_ref["page"] = page
        return manual_clicks

      all_result_hrefs, result_hrefs, hunt_found = self._hunt_mobile_serp_for_target(
        page,
        profile,
        keyword,
        page_num,
        confirmed_hrefs,
        stopped=stopped,
        timeout_seconds=42.0,
      )

      if not result_hrefs and not hunt_found:
        scan_incomplete = True
        self.logger(
          f"[Search] Mobile page {page_num}: current result batch could not be "
          "confirmed after loading — pagination stopped"
        )
        break

      pages_with_results += 1
      page_hrefs = result_hrefs or all_result_hrefs
      self.logger(
        f"[Search] Mobile page {page_num}/{configured_max}: confirmed "
        f"{len(page_hrefs)} current-batch organic link(s) for '{keyword}'"
        + (" (target signal during hunt)" if hunt_found else "")
      )
      page_clicks, page = self._process_page_target_matches(
        page,
        profile,
        keyword,
        page_num,
        all_result_hrefs,
        clicked_keys,
        stop_event=stop_event,
        stopped=stopped,
        set_status=set_status,
        on_ui_status=on_ui_status,
        on_failure=on_failure,
        work_ref=work_ref,
        network=network,
        handle_response=handle_response,
        on_target_click=on_target_click,
        log=log,
        clicked_domains=clicked_domains,
        mobile_page_hrefs=page_hrefs,
        end_session_after_dwell=end_session_after_dwell,
      )
      total_clicks += page_clicks
      if work_ref is not None:
        work_ref["page"] = page

      if page_clicks > 0:
        self.logger(
          f"[Search] Mobile: target visited on page {page_num} for '{keyword}' "
          "— stopping SERP scan"
        )
        return total_clicks

      if self._last_target_open_failed:
        self.logger(
          f"[Search] Mobile: stopping scan after failed target open for '{keyword}'"
        )
        break

      if hunt_found and page_clicks == 0:
        self._last_target_open_failed = True
        log(
          f"[Target] Failed open target site: '{keyword}' → {self._session_target_domain}"
        )
        break

      if stopped():
        return total_clicks

      if page_num >= configured_max:
        self.logger(
          f"[Search] Mobile: reached configured page limit {configured_max} for '{keyword}'"
        )
        break

      # Stabilize leaves the viewport mid-batch; scroll footer into view before
      # deciding Google has no more pages (lazy 더보기/> often appears only then).
      self._scroll_to_serp_pagination(page, mobile=True, fast=True)
      if not self._mobile_serp_pagination_available(page, profile, strict=True):
        self._mobile_serp_end_reached = True
        self.logger(
          f"[Search] Mobile: no footer 더보기/> after page {page_num} for '{keyword}' "
          f"(config={configured_max})"
        )
        break

      pagination_baseline_hrefs = all_result_hrefs
      before_pagination = self._snapshot_mobile_pagination_state(page)
      tap_result = self._tap_mobile_more_once(
        page,
        profile,
        keyword,
        page_num,
        baseline_hrefs=pagination_baseline_hrefs,
        stopped=stopped,
        on_failure=on_failure,
      )
      if stopped():
        return total_clicks
      if tap_result == "target_landed":
        if self._mobile_landed_on_target(page):
          manual_clicks, page = self._try_finish_mobile_manual_target(
            page,
            profile,
            keyword,
            page_num,
            stop_event=stop_event,
            stopped=stopped,
            set_status=set_status,
            on_ui_status=on_ui_status,
            on_failure=on_failure,
            work_ref=work_ref,
            network=network,
            handle_response=handle_response,
            on_target_click=on_target_click,
            log=log,
            end_session_after_dwell=end_session_after_dwell,
          )
          if manual_clicks > 0:
            if work_ref is not None:
              work_ref["page"] = page
            return total_clicks + manual_clicks
        self.logger(
          f"[Search] Mobile: accidental navigation during pagination on page {page_num} "
          f"for '{keyword}' — recovering SERP (no target dwell)"
        )
        page = self._escape_mobile_target_to_serp(
          page,
          keyword,
          profile,
          on_failure,
          log,
          before_serp_url=str(before_pagination.get("url") or ""),
        )
        if work_ref is not None:
          work_ref["page"] = page
        self._sync_mobile_serp_page(page, profile)
        continue

      if not tap_result:
        if stopped():
          return total_clicks
        if self._force_mobile_scroll_next_batch(
          page, before_pagination, self._next_serp_start_offset(page, profile),
          stopped=stopped,
        ):
          pagination_failures = 0
          confirmed_hrefs = pagination_baseline_hrefs
          page_num += 1
          if work_ref is not None:
            work_ref["page"] = page
          self._advance_mobile_serp_page()
          continue
        pagination_failures += 1
        try:
          after_retry = self._snapshot_mobile_pagination_state(page)
          if self._pagination_advanced(
            before_pagination, after_retry, self._next_serp_start_offset(page, profile),
          ):
            self.logger(
              f"[Search] Mobile: pagination advanced during retry wait for '{keyword}' "
              f"(manual or delayed append)"
            )
            pagination_failures = 0
            confirmed_hrefs = pagination_baseline_hrefs
            page_num += 1
            if work_ref is not None:
              work_ref["page"] = page
            self._advance_mobile_serp_page()
            continue
        except Exception:
          pass
        if (
          pagination_failures < max_pagination_failures
          and self._mobile_serp_pagination_available(page, profile, strict=True)
        ):
          self.logger(
            f"[Search] Mobile: pagination retry {pagination_failures}/"
            f"{max_pagination_failures} for page {page_num} → {page_num + 1} "
            f"('{keyword}')"
          )
          continue
        if pagination_failures >= max_pagination_failures:
          self.logger(
            f"[Search] Mobile: pagination gave up after {pagination_failures} attempt(s) "
            f"on page {page_num} for '{keyword}' (no direct URL)"
          )
        if not self._mobile_serp_pagination_available(page, profile, strict=True):
          self._mobile_serp_end_reached = True
        else:
          self.logger(
            f"[Search] Mobile: pagination exhausted after {pagination_failures} "
            f"attempt(s) on page {page_num} for '{keyword}'"
          )
        break

      pagination_failures = 0
      # The next loop excludes this confirmed snapshot and scans only URLs
      # appended by the successful pagination action.
      confirmed_hrefs = pagination_baseline_hrefs
      page_num += 1

    self._last_search_exhaustion_eligible = (
      not scan_incomplete
      and total_clicks == 0
      and not self._last_target_open_failed
      and page_num >= configured_max
    )
    self._last_search_exhausted = self._last_search_exhaustion_eligible
    if self._last_search_exhaustion_eligible and total_clicks == 0:
      self.logger(
        f"[Search] Mobile reached configured page limit {configured_max} for '{keyword}' "
        "but no target domains were clicked."
      )
    elif total_clicks > 0 and pages_with_results > 0:
      self.logger(
        f"[Search] Mobile finished full scan for '{keyword}' "
        f"({pages_with_results} page(s), {total_clicks} target visit(s))"
      )
    return total_clicks

  def _tap_mobile_more_once(
    self,
    page: Page,
    profile: ProfileSpec,
    keyword: str,
    current_page_num: int,
    *,
    baseline_hrefs: Optional[list[str]] = None,
    stopped: Optional[Callable[[], bool]] = None,
    on_failure: Optional[FailureCallback] = None,
  ) -> Union[bool, Literal["target_landed"]]:
    _stopped = stopped or (lambda: False)

    def _finish_pagination_success() -> bool:
      # Next hunt pass positions the viewport; skip duplicate scroll reset here.
      return True
    self._scroll_to_serp_pagination(page, mobile=True, fast=True)
    self.logger(
      f"[Search] Mobile: tapping 더보기 page {current_page_num} → {current_page_num + 1} "
      f"('{keyword}')"
    )
    before_state = self._snapshot_mobile_pagination_state(page)
    target_start = self._next_serp_start_offset(page, profile)
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
      if _stopped():
        return False
      if not self._is_google_serp_url(page.url):
        if self._mobile_landed_on_target(page):
          return "target_landed"
        outcome = self._recover_google_serp_after_mistap(page, before_state.get("url") or "")
        if outcome == "target":
          return "target_landed"
      try:
        if self._click_mobile_pagination_next(
          page,
          profile,
          before_state,
          keyword=keyword,
          target_start=target_start,
          stopped=_stopped,
        ):
          try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
          except Exception:
            pass
          page.wait_for_timeout(random.randint(450, 950))
          if _stopped():
            return False
          after_state = self._snapshot_mobile_pagination_state(page)
          if self._pagination_advanced(
            before_state,
            after_state,
            target_start,
            numeric_only=False,
          ):
            url_page = self._current_search_results_page_num(page)
            if url_page > self._mobile_serp_page:
              self._mobile_serp_page = url_page
            elif after_state.get("start", 0) >= target_start:
              self._mobile_serp_page = max(self._mobile_serp_page, (target_start // 10) + 1)
            elif after_state.get("ip_index", 0) > before_state.get("ip_index", 0):
              self._mobile_serp_page = max(
                self._mobile_serp_page,
                after_state.get("ip_index", 0) + 1,
              )
            else:
              self._advance_mobile_serp_page()
            self._serp_pause()
            return _finish_pagination_success()
          if self._wait_mobile_scroll_append(
            page, before_state, target_start, stopped=_stopped, keyword=keyword,
          ):
            self._advance_mobile_serp_page()
            self._serp_pause()
            return _finish_pagination_success()
          self.logger(
            f"[Search] Mobile: tap attempt {attempt}/{max_attempts} did not advance pagination "
            f"(links {before_state.get('organic_count')}→{after_state.get('organic_count')}, "
            f"start {before_state.get('start')}→{after_state.get('start')}, "
            f"ip {before_state.get('ip_index')}→{after_state.get('ip_index')})"
          )
          if not self._is_google_serp_url(after_state.get("url") or ""):
            if self._mobile_landed_on_target(page):
              return "target_landed"
            outcome = self._recover_google_serp_after_mistap(page, before_state.get("url") or "")
            if outcome == "target":
              return "target_landed"
      except Exception as exc:
        if self._is_connection_error(exc):
          self.logger(
            f"[Search] Mobile 더보기 tap retry {attempt}/{max_attempts} after navigation: {exc}"
          )
          page.wait_for_timeout(random.randint(700, 1400))
          try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
          except Exception:
            pass
          continue
        raise
      if attempt < max_attempts and not _stopped():
        scroll_page(page, random.randint(350, 700), mobile=True)
        page.wait_for_timeout(random.randint(200, 450))

    if _stopped():
      return False
    return False

  def _navigate_to_search_page(
    self,
    page: Page,
    keyword: str,
    page_num: int,
    profile: ProfileSpec,
    stop_event: Optional[threading.Event],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    on_failure: Optional[FailureCallback],
    log: Callable[[str], None],
  ) -> bool:
    if page_num <= 1:
      if not self._is_mobile_profile(profile):
        current_page_num = self._serp_page_num(page, profile)
        if current_page_num != 1:
          self.logger(
            f"[Search] Desktop: returning to page 1 via pagination for '{keyword}' "
            f"(was on page {current_page_num})"
          )
          if not self._click_desktop_serp_page(page, 1):
            self.logger(
              f"[Search] Desktop: page-1 pagination failed for '{keyword}' "
              "(no direct URL fallback)"
            )
            return False
    elif self._is_mobile_profile(profile):
      current_page_num = self._serp_page_num(page, profile)
      if page_num > current_page_num:
        taps_needed = page_num - current_page_num
        self.logger(
          f"[Search] Mobile: tapping 더보기 {taps_needed} time(s) "
          f"for page {current_page_num} → {page_num} ('{keyword}')"
        )
        if not self._go_to_next_search_results_page_mobile(page, page_num, profile):
          self.logger(
            f"[Search] Mobile: could not reach page {page_num} via 더보기 for '{keyword}' "
            "(no direct URL fallback on mobile)"
          )
          return False
        self._serp_micro_scroll(page, profile, times=1)
    else:
      if not self._click_desktop_serp_page(page, page_num):
        self.logger(
          f"[Search] Desktop pagination (number/next) to page {page_num} failed for "
          f"'{keyword}' (no direct URL fallback)"
        )
        return False

    guard, page = self._resolve_captcha_after_navigation(
      page,
      stop_event,
      set_status,
      on_ui_status,
      profile,
      log,
      context=f"post-search-page-{page_num}",
    )
    return guard not in self._captcha_abort_values()

  def _collect_organic_result_hrefs_with_retry(
    self,
    page: Page,
    served_page_num: int,
    profile: ProfileSpec,
    stop_event: Optional[threading.Event],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    log: Callable[[str], None],
  ) -> Optional[list[str]]:
    hrefs = self._collect_organic_result_hrefs(page, profile)
    if hrefs:
      return hrefs

    for attempt in range(1, 4):
      page.wait_for_timeout(random.randint(500, 1000))
      if self._is_mobile_profile(profile):
        self._scroll_through_mobile_serp_for_scan(page, profile)
      else:
        self._serp_micro_scroll(page, profile, times=1)
      hrefs = self._collect_organic_result_hrefs(page, profile)
      if hrefs:
        return hrefs
    page.wait_for_timeout(random.randint(400, 900))
    if self._is_mobile_profile(profile):
      self._scroll_through_mobile_serp_for_scan(page, profile)
    else:
      self._serp_micro_scroll(page, profile, times=1)
    hrefs = self._collect_organic_result_hrefs(page, profile)
    if hrefs:
      return hrefs
    if served_page_num == 1 and self._is_google_block_page(page):
      log("[Search] Google block/captcha page detected instead of SERP results")

    guard, page = self._resolve_captcha_after_navigation(
      page,
      stop_event,
      set_status,
      on_ui_status,
      profile,
      log,
      context=f"no-results-p{served_page_num}",
    )
    if guard in self._captcha_abort_values():
      return None
    page = self._wait_for_serp_stable(page, log)
    page.wait_for_timeout(random.randint(500, 1000))
    self._serp_micro_scroll(page, profile, times=1)
    hrefs = self._collect_organic_result_hrefs(page, profile)
    if hrefs:
      return hrefs
    if served_page_num == 1:
      page.wait_for_timeout(random.randint(400, 800))
      hrefs = self._collect_organic_result_hrefs(page, profile)
    return hrefs

  @staticmethod
  def _is_google_block_page(page: Page) -> bool:
    try:
      snippet = (page.content() or "")[:12000].lower()
    except Exception:
      return False
    markers = (
      "unusual traffic",
      "비정상적인 트래픽",
      "not a robot",
      "로봇이 아닙니다",
      "recaptcha",
      "g-recaptcha",
      "/sorry/",
    )
    return any(marker in snippet for marker in markers)

  @staticmethod
  def _is_ios_profile(profile: ProfileSpec) -> bool:
    return (profile.os_type or "").strip().lower().startswith("ios")

  @staticmethod
  def _is_mobile_profile(profile: ProfileSpec) -> bool:
    os_type = (profile.os_type or "").strip().lower()
    return os_type.startswith("android") or os_type.startswith("ios")

  def _scroll_through_mobile_serp_for_scan(self, page: Page, profile: ProfileSpec) -> None:
    """Scroll top→bottom through result cards to expose links (not pagination footer)."""
    mobile = self._is_mobile_profile(profile)
    try:
      page.evaluate("window.scrollTo(0, 0)")
      page.wait_for_timeout(random.randint(150, 380))
    except Exception:
      pass
    try:
      steps = page.evaluate(
        """() => {
          const h = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
          const vh = window.innerHeight || 800;
          return Math.min(8, Math.max(3, Math.ceil(h / Math.max(vh * 0.5, 320))));
        }"""
      )
      steps = max(3, min(8, int(steps or 4)))
    except Exception:
      steps = 4
    for _ in range(steps):
      scroll_page(page, random.randint(360, 740), mobile=mobile)
      page.wait_for_timeout(random.randint(220, 480))
    self._serp_micro_scroll(page, profile, times=1)

  @classmethod
  def _merge_organic_href_lists(cls, *href_lists: list[str]) -> list[str]:
    """Keep first-seen order while unioning organic href snapshots across scrolls."""
    merged: list[str] = []
    seen: set[str] = set()
    for href_list in href_lists:
      for href in href_list or []:
        key = cls._serp_click_dedupe_key_static(href) or (href or "").strip().lower()
        if not key or key in seen:
          continue
        seen.add(key)
        merged.append(href)
    return merged

  def _mobile_target_present_in_hrefs(self, hrefs: list[str]) -> bool:
    targets = self._get_target_domains()
    if not targets:
      return False
    for href in hrefs or []:
      resolved = self._resolve_result_href(href) or href
      if self._href_match_target_domain(resolved, targets):
        return True
      if self._href_match_target_domain(href, targets):
        return True
    return False

  def _mobile_target_visible_in_dom(self, page: Page) -> bool:
    """True when assigned domain text/href is present anywhere in the SERP DOM."""
    targets = self._get_target_domains()
    target_keys = [
      self._normalize_domain(target)
      for target in targets
      if self._normalize_domain(target)
    ]
    if not target_keys:
      return False
    try:
      return bool(
        page.evaluate(
          """(targetKeys) => {
            const clean = (value) => (value || '')
              .toLowerCase()
              .replace(/[\\u200b-\\u200d\\ufeff]/g, '')
              .replace(/\\s+/g, '');
            const keys = targetKeys.map(clean).filter(Boolean);
            if (!keys.length) return false;
            const hasTarget = (value) => {
              const text = clean(value);
              return keys.some((key) => text.includes(key));
            };
            if (hasTarget(document.body.innerText || document.body.textContent || '')) {
              return true;
            }
            for (const anchor of document.querySelectorAll('a[href]')) {
              const href = anchor.getAttribute('href') || '';
              if (hasTarget(href)) return true;
            }
            return false;
          }""",
          target_keys,
        )
      )
    except Exception:
      return False

  def _scroll_target_into_view_if_present(self, page: Page) -> bool:
    """Bring a visible assigned-domain card into view for a reliable click."""
    targets = self._get_target_domains()
    target_keys = [
      self._normalize_domain(target)
      for target in targets
      if self._normalize_domain(target)
    ]
    if not target_keys:
      return False
    try:
      return bool(
        page.evaluate(
          """(targetKeys) => {
            const clean = (value) => (value || '')
              .toLowerCase()
              .replace(/[\\u200b-\\u200d\\ufeff]/g, '')
              .replace(/\\s+/g, '');
            const keys = targetKeys.map(clean).filter(Boolean);
            const hasTarget = (value) => {
              const text = clean(value);
              return keys.some((key) => text.includes(key));
            };
            const nodes = document.querySelectorAll(
              '#rso a[href], div#search a[href], cite, a[href] h3, a[href] .LC20lb, span, div'
            );
            for (const node of nodes) {
              const text = (node.innerText || node.textContent || '').trim();
              const href = (node.getAttribute && node.getAttribute('href')) || '';
              if (!hasTarget(text) && !hasTarget(href)) continue;
              const target = node.closest('a[href]') || node;
              target.scrollIntoView({ block: 'center', behavior: 'instant' });
              return true;
            }
            return false;
          }""",
          target_keys,
        )
      )
    except Exception:
      return False

  def _nudge_mobile_viewport_up_for_new_batch(self, page: Page) -> None:
    """Lift the viewport slightly above the footer without jumping to page top."""
    page.evaluate(
      """() => {
        const vh = window.innerHeight || 800;
        const fraction = 0.30 + Math.random() * 0.20;
        const step = Math.min(Math.max(vh * fraction, 240), 620);
        window.scrollBy(0, -step);
      }"""
    )
    page.wait_for_timeout(random.randint(280, 520))

  def _prepare_mobile_hunt_scroll_position(
    self,
    page: Page,
    baseline_hrefs: list[str],
    *,
    page_num: int = 1,
    keyword: str = "",
  ) -> None:
    """Move viewport to the start of the current mobile batch before slow hunt.

    After 더보기 the view is usually stuck on the footer; page 2+ must not hunt
    from there or the newly appended batch is skipped.
    """
    try:
      if not baseline_hrefs:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(random.randint(280, 520))
        return

      hints = [href for href in baseline_hrefs[-5:] if href]
      anchored = False
      if hints:
        anchored = bool(
          page.evaluate(
            """(hints) => {
              const hosts = hints.map((h) => {
                try { return new URL(h, location.href).hostname.replace(/^www\\./, ''); }
                catch (e) { return ''; }
              }).filter(Boolean);
              const paths = hints.map((h) => {
                try {
                  const p = new URL(h, location.href).pathname || '';
                  return p.length > 1 ? p : '';
                } catch (e) { return ''; }
              }).filter(Boolean);
              const links = document.querySelectorAll('a[href]');
              let lastMatch = null;
              for (const a of links) {
                const href = a.href || '';
                if (!href) continue;
                let host = '';
                let path = '';
                try {
                  const u = new URL(href, location.href);
                  host = u.hostname.replace(/^www\\./, '');
                  path = u.pathname || '';
                } catch (e) { continue; }
                const hostHit = hosts.some((h) => host.includes(h) || h.includes(host));
                const pathHit = paths.some((p) => path.includes(p) || p.includes(path));
                if (!hostHit && !pathHit) continue;
                const rect = a.getBoundingClientRect();
                const absTop = rect.top + (window.scrollY || 0);
                if (!lastMatch || absTop > lastMatch.top) {
                  lastMatch = { el: a, top: absTop };
                }
              }
              if (lastMatch && lastMatch.el) {
                const vh = window.innerHeight || 800;
                const targetY = Math.max(0, lastMatch.top - Math.floor(vh * 0.12));
                window.scrollTo(0, targetY);
                return true;
              }
              return false;
            }""",
            hints,
          )
        )
      if not anchored:
        self._nudge_mobile_viewport_up_for_new_batch(page)
      else:
        page.wait_for_timeout(random.randint(320, 580))
      mode = (
        "batch anchor after pagination"
        if anchored
        else "footer nudge (anchor fallback)"
      )
      self.logger(
        f"[Search] Mobile page {page_num}: hunt scroll reset — {mode}"
        + (f" for '{keyword}'" if keyword else "")
      )
    except Exception:
      try:
        if baseline_hrefs:
          self._nudge_mobile_viewport_up_for_new_batch(page)
        else:
          page.evaluate("window.scrollTo(0, 0)")
      except Exception:
        pass

  def _hunt_mobile_serp_for_target(
    self,
    page: Page,
    profile: ProfileSpec,
    keyword: str,
    page_num: int,
    baseline_hrefs: list[str],
    *,
    stopped: Optional[Callable[[], bool]] = None,
    timeout_seconds: float = 42.0,
  ) -> tuple[list[str], list[str], bool]:
    """Slowly scroll the current mobile batch top→bottom while hunting the target.

    Unions every organic-href snapshot so early results are not lost when Google
    virtualizes/replaces the DOM. Stops early once the assigned domain is found.
    """
    _stopped = stopped or (lambda: False)
    self._prepare_mobile_hunt_scroll_position(
      page, baseline_hrefs, page_num=page_num, keyword=keyword,
    )

    union_hrefs = self._collect_organic_result_hrefs(page, profile)
    deadline = time.time() + max(12.0, float(timeout_seconds))
    found = False
    steps = 0
    max_steps = 28

    self.logger(
      f"[Search] Mobile page {page_num}: slow hunt for "
      f"'{self._session_target_domain or 'target'}' under '{keyword}'"
    )

    while not _stopped() and steps < max_steps and time.time() < deadline:
      steps += 1
      fresh = self._collect_organic_result_hrefs(page, profile)
      union_hrefs = self._merge_organic_href_lists(union_hrefs, fresh)
      current_batch, _ = self._mobile_hrefs_after_baseline(union_hrefs, baseline_hrefs)

      if baseline_hrefs:
        target_hit = (
          self._mobile_target_present_in_hrefs(current_batch)
          or self._mobile_target_visible_in_dom(page)
        )
      else:
        target_hit = (
          self._mobile_target_present_in_hrefs(current_batch)
          or self._mobile_target_present_in_hrefs(union_hrefs)
          or self._mobile_target_visible_in_dom(page)
        )
      if target_hit:
        self._scroll_target_into_view_if_present(page)
        page.wait_for_timeout(random.randint(220, 480))
        # Re-collect after bringing the card into view — href may hydrate late.
        fresh = self._collect_organic_result_hrefs(page, profile)
        union_hrefs = self._merge_organic_href_lists(union_hrefs, fresh)
        found = True
        self.logger(
          f"[Search] Mobile page {page_num}: target signal found during slow hunt "
          f"(step {steps}, union={len(union_hrefs)})"
        )
        break

      try:
        metrics = page.evaluate(
          """() => {
            const height = Math.max(
              document.body.scrollHeight,
              document.documentElement.scrollHeight
            );
            const viewport = window.innerHeight || 800;
            const y = window.scrollY || 0;
            const bottom = Math.max(0, height - viewport);
            const atBottom = y >= bottom - 48;
            if (!atBottom) {
              const step = Math.max(140, Math.min(260, Math.floor(viewport * 0.28)));
              window.scrollBy(0, step);
            }
            return {
              atBottom,
              y: window.scrollY || 0,
              height,
              viewport,
            };
          }"""
        ) or {}
      except Exception:
        metrics = {"atBottom": True}

      page.wait_for_timeout(random.randint(420, 780))
      if bool(metrics.get("atBottom")):
        # Small bounce to wake lazy cards near the footer, then stop.
        try:
          page.evaluate(
            "(distance) => window.scrollBy(0, -distance)",
            random.randint(120, 220),
          )
          page.wait_for_timeout(random.randint(280, 480))
          page.evaluate(
            """() => window.scrollTo(0, Math.max(
              document.body.scrollHeight,
              document.documentElement.scrollHeight
            ))"""
          )
          page.wait_for_timeout(random.randint(350, 650))
        except Exception:
          pass
        fresh = self._collect_organic_result_hrefs(page, profile)
        union_hrefs = self._merge_organic_href_lists(union_hrefs, fresh)
        current_batch, _ = self._mobile_hrefs_after_baseline(union_hrefs, baseline_hrefs)
        if baseline_hrefs:
          footer_hit = (
            self._mobile_target_present_in_hrefs(current_batch)
            or self._mobile_target_visible_in_dom(page)
          )
        else:
          footer_hit = (
            self._mobile_target_present_in_hrefs(union_hrefs)
            or self._mobile_target_visible_in_dom(page)
          )
        if footer_hit:
          self._scroll_target_into_view_if_present(page)
          found = True
          self.logger(
            f"[Search] Mobile page {page_num}: target signal found near footer "
            f"(union={len(union_hrefs)})"
          )
        break

    current_batch, cumulative = self._mobile_hrefs_after_baseline(
      union_hrefs, baseline_hrefs,
    )
    self.logger(
      f"[Search] Mobile result batch hunt done: current={len(current_batch)}, "
      f"all={len(union_hrefs)}, found={found}, "
      f"mode={'append' if cumulative else 'replace'}"
    )
    return union_hrefs, current_batch, found

  @classmethod
  def _mobile_hrefs_after_baseline(
    cls,
    current_hrefs: list[str],
    baseline_hrefs: list[str],
  ) -> tuple[list[str], bool]:
    """Return only the newly appended result batch; bool indicates cumulative DOM."""
    if not baseline_hrefs:
      return list(current_hrefs), False
    baseline_keys = {
      cls._serp_click_dedupe_key_static(href)
      for href in baseline_hrefs
      if cls._serp_click_dedupe_key_static(href)
    }
    current_pairs = [
      (href, cls._serp_click_dedupe_key_static(href))
      for href in current_hrefs
    ]
    overlap = sum(1 for _, key in current_pairs if key and key in baseline_keys)
    overlap_floor = min(3, max(1, len(baseline_keys)))
    cumulative = overlap >= overlap_floor
    if not cumulative:
      # start=10/20 navigation replaces the old DOM: every visible href is current.
      return list(current_hrefs), False
    return [
      href
      for href, key in current_pairs
      if not key or key not in baseline_keys
    ], True

  def _stabilize_mobile_serp_segment(
    self,
    page: Page,
    profile: ProfileSpec,
    baseline_hrefs: list[str],
    *,
    stopped: Optional[Callable[[], bool]] = None,
    timeout_seconds: float = 14.0,
  ) -> tuple[list[str], list[str]]:
    """Load the current mobile result batch fully, then return (all, current batch)."""
    initial_hrefs = self._collect_organic_result_hrefs(page, profile)
    union_hrefs = list(initial_hrefs)
    _, cumulative = self._mobile_hrefs_after_baseline(initial_hrefs, baseline_hrefs)
    try:
      if not baseline_hrefs or not cumulative:
        page.evaluate("window.scrollTo(0, 0)")
      page.wait_for_timeout(random.randint(300, 650))
    except Exception:
      pass

    deadline = time.time() + max(5.0, float(timeout_seconds))
    stable_rounds = 0
    last_signature: Optional[tuple[tuple[str, ...], int]] = None
    all_hrefs = initial_hrefs
    bounce_due = True

    while time.time() < deadline:
      if stopped and stopped():
        break
      try:
        metrics = page.evaluate(
          """() => {
            const height = Math.max(
              document.body.scrollHeight,
              document.documentElement.scrollHeight
            );
            const viewport = window.innerHeight || 800;
            const y = window.scrollY || 0;
            const bottom = Math.max(0, height - viewport);
            if (y < bottom - 80) {
              window.scrollTo(0, Math.min(bottom, y + Math.max(320, viewport * 0.68)));
            } else {
              window.scrollTo(0, bottom);
            }
            return { height, viewport, y: window.scrollY || 0, bottom };
          }"""
        ) or {}
      except Exception:
        metrics = {}
      page.wait_for_timeout(random.randint(420, 760))

      try:
        height = int(metrics.get("height") or 0)
        viewport = int(metrics.get("viewport") or 800)
        y = int(page.evaluate("() => window.scrollY || 0"))
        latest_height = int(page.evaluate(
          "() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
        ))
        at_bottom = y + viewport >= latest_height - 120
      except Exception:
        height = 0
        latest_height = 0
        viewport = 800
        at_bottom = False

      if at_bottom and bounce_due:
        # Trigger lazy observers around the last few cards, then return to the footer.
        try:
          page.evaluate(
            "(distance) => window.scrollBy(0, -distance)",
            random.randint(max(180, viewport // 4), max(260, viewport // 2)),
          )
          page.wait_for_timeout(random.randint(280, 520))
          page.evaluate(
            """() => window.scrollTo(0, Math.max(
              document.body.scrollHeight,
              document.documentElement.scrollHeight
            ))"""
          )
          page.wait_for_timeout(random.randint(420, 760))
        except Exception:
          pass
        bounce_due = False

      all_hrefs = self._collect_organic_result_hrefs(page, profile)
      # Keep every snapshot — mobile Google may drop early cards from the live DOM.
      union_hrefs = self._merge_organic_href_lists(union_hrefs, all_hrefs)
      signature = (
        tuple(self._serp_click_dedupe_key_static(href) for href in union_hrefs),
        latest_height or height,
      )
      if at_bottom and signature == last_signature:
        stable_rounds += 1
      else:
        stable_rounds = 0
        if not at_bottom or (last_signature and signature != last_signature):
          bounce_due = True
      last_signature = signature
      if at_bottom and stable_rounds >= 2:
        break

    current_hrefs, cumulative = self._mobile_hrefs_after_baseline(
      union_hrefs, baseline_hrefs,
    )
    self.logger(
      f"[Search] Mobile result batch stabilized: current={len(current_hrefs)}, "
      f"all={len(union_hrefs)}, mode={'append' if cumulative else 'replace'}"
    )
    return union_hrefs, current_hrefs

  def _scroll_to_serp_pagination(self, page: Page, *, mobile: bool, fast: bool = False) -> None:
    if mobile:
      scroll_loops = 1 if fast else random.randint(3, 5)
      delta_lo, delta_hi = (320, 620) if fast else (450, 950)
      wait_lo, wait_hi = (80, 180) if fast else (200, 500)
      for _ in range(scroll_loops):
        scroll_page(page, random.randint(delta_lo, delta_hi), mobile=True)
        page.wait_for_timeout(random.randint(wait_lo, wait_hi))
    try:
      page.evaluate(
        """(isMobile) => {
          window.scrollTo(0, Math.max(
            document.body.scrollHeight,
            document.documentElement.scrollHeight
          ));
          if (!isMobile) return;
          const footerSelectors = [
            'a[jsname="oHxHid"]',
            'a[aria-label="검색결과 더보기"]',
            'a#pnnext',
            '#pnnext',
            'a[rel="next"]',
            '#foot',
            '#navd',
            '#botstuff',
            'nav[role="navigation"]',
          ];
          let best = null;
          let bestBottom = -1;
          for (const selector of footerSelectors) {
            document.querySelectorAll(selector).forEach((el) => {
              const rect = el.getBoundingClientRect();
              if (rect.width < 4 || rect.height < 4) return;
              if (rect.bottom > bestBottom) {
                bestBottom = rect.bottom;
                best = el;
              }
            });
          }
          if (best) {
            best.scrollIntoView({ block: 'center', behavior: 'instant' });
          }
        }""",
        mobile,
      )
    except Exception:
      pass
    footer_wait = (120, 280) if fast else (400, 900)
    page.wait_for_timeout(random.randint(*footer_wait))
    if not mobile:
      for selector in ("#botstuff", 'nav[role="navigation"]', "#pnnext", 'a[rel="next"]'):
        try:
          marker = page.locator(selector).first
          if marker.count():
            marker.scroll_into_view_if_needed(timeout=3000)
            break
        except Exception:
          continue

  def _nudge_mobile_serp_footer_for_retry(self, page: Page, profile: ProfileSpec) -> None:
    """Nudge up from the footer zone and re-approach pagination (no jump to page top)."""
    mobile = self._is_mobile_profile(profile)
    try:
      page.evaluate(
        """() => {
          const vh = window.innerHeight || 800;
          const step = Math.min(Math.max(vh * 0.85, 420), 780);
          window.scrollBy(0, -step);
        }"""
      )
      page.wait_for_timeout(random.randint(220, 450))
    except Exception:
      pass
    self._scroll_to_serp_pagination(page, mobile=mobile, fast=True)

  def _click_desktop_serp_page_number(self, page: Page, page_num: int) -> bool:
    current_page_num = self._current_search_results_page_num(page)
    target_page = max(1, int(page_num))
    if current_page_num == target_page:
      return True

    self._scroll_to_serp_pagination(page, mobile=False, fast=True)
    start = (target_page - 1) * 10
    delay_lo, delay_hi = self._serp_delay_bounds()
    start_selectors = (
      f'#botstuff a[href*="start={start}"]',
      f'nav[role="navigation"] a[href*="start={start}"]',
      f'table a[href*="start={start}"]',
      f'a[href*="start={start}"][aria-label*="Page" i]',
    )
    for selector in start_selectors:
      try:
        link = page.locator(selector).first
        if link.count() == 0 or not link.is_visible():
          continue
        self.logger(f"[Search] Desktop: clicking pagination link for page {target_page}")
        human_click(
          link,
          delay_lo,
          delay_hi,
          page=page,
          mobile=False,
        )
        try:
          page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
          pass
        self._serp_pause()
        if self._current_search_results_page_num(page) == target_page:
          return True
      except Exception:
        continue

    pagination = page.locator('#botstuff a, nav[role="navigation"] a, table a[href*="start="]')
    for index in range(min(pagination.count(), 24)):
      try:
        link = pagination.nth(index)
        if not link.is_visible():
          continue
        text = (link.inner_text() or "").strip()
        if text != str(target_page):
          continue
        self.logger(f"[Search] Desktop: clicking page number '{target_page}' in footer")
        human_click(
          link,
          delay_lo,
          delay_hi,
          page=page,
          mobile=False,
        )
        try:
          page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
          pass
        self._serp_pause()
        return self._current_search_results_page_num(page) == target_page
      except Exception:
        continue
    return self._current_search_results_page_num(page) == target_page

  def _click_desktop_serp_next(self, page: Page) -> bool:
    self._scroll_to_serp_pagination(page, mobile=False, fast=True)
    delay_lo, delay_hi = self._serp_delay_bounds()
    before = self._current_search_results_page_num(page)
    next_selectors = (
      "a#pnnext",
      "#pnnext a",
      'a[rel="next"]',
      'a[aria-label*="Next" i]',
      'a[aria-label*="다음" i]',
      'span#pnnext a',
    )
    for selector in next_selectors:
      try:
        link = page.locator(selector).first
        if link.count() == 0 or not link.is_visible():
          continue
        self.logger("[Search] Desktop: clicking Next pagination control")
        human_click(
          link,
          delay_lo,
          delay_hi,
          page=page,
          mobile=False,
        )
        try:
          page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
          pass
        self._serp_pause()
        after = self._current_search_results_page_num(page)
        return after > before
      except Exception:
        continue
    return False

  def _click_desktop_serp_page(self, page: Page, page_num: int) -> bool:
    target_page = max(1, int(page_num))
    current_page_num = self._current_search_results_page_num(page)
    if target_page <= 1:
      if current_page_num <= 1:
        return True
      return self._click_desktop_serp_page_number(page, 1)

    if current_page_num == target_page:
      return True

    if self._click_desktop_serp_page_number(page, target_page):
      return True

    hops = 0
    max_hops = min(max(target_page - current_page_num, 0) + 2, 14)
    while current_page_num < target_page and hops < max_hops:
      if self._click_desktop_serp_page_number(page, target_page):
        return True
      if not self._click_desktop_serp_next(page):
        break
      hops += 1
      current_page_num = self._current_search_results_page_num(page)
      if current_page_num == target_page:
        return True

    return self._current_search_results_page_num(page) == target_page

  def _go_to_next_search_results_page_mobile(
    self,
    page: Page,
    target_page_num: int,
    profile: ProfileSpec,
  ) -> bool:
    current_page_num = self._serp_page_num(page, profile)
    if target_page_num <= current_page_num:
      return True

    hops_needed = target_page_num - current_page_num
    max_hops = min(16, hops_needed + 3)

    for hop in range(max_hops):
      current_page_num = self._serp_page_num(page, profile)
      if current_page_num >= target_page_num:
        return True

      self._scroll_to_serp_pagination(page, mobile=True, fast=False)
      self.logger(
        f"[Search] Mobile: tapping 더보기 ({hop + 1}/{hops_needed}) "
        f"page {current_page_num} → {target_page_num}"
      )
      if not self._click_mobile_pagination_next(page, profile):
        scroll_page(page, random.randint(350, 650), mobile=True)
        page.wait_for_timeout(random.randint(200, 450))
        if not self._click_mobile_pagination_next(page, profile):
          self.logger(
            f"[Search] Mobile: pagination controls not found after scroll "
            f"(at page {current_page_num})"
          )
          continue

      self._advance_mobile_serp_page()
      try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
      except Exception:
        pass
      page.wait_for_timeout(random.randint(400, 900))
      url_page = self._current_search_results_page_num(page)
      if url_page > self._mobile_serp_page:
        self._mobile_serp_page = url_page
      self._serp_pause()
      self._serp_micro_scroll(page, profile, times=1)

    return self._serp_page_num(page, profile) >= target_page_num

  _MOBILE_SERP_MORE_PRIMARY_LABELS = (
    "검색결과 더보기",
    "검색결과 더 보기",
    "more search results",
  )
  _MOBILE_SERP_MORE_EXACT_LABELS = frozenset({
    "검색결과 더보기",
    "검색결과 더 보기",
    "더보기",
    "다음",
    "next",
    "more search results",
    "more results",
  })
  _MOBILE_SERP_MORE_BLOCK_TOKENS = (
    "비지니스", "비즈니스", "business", "지도", "maps", "리뷰", "review", "장소", "place",
    "전화", "영업", "store", "local", "이전", "previous", "접기", "less",
    "사진", "photo", "길찾기", "directions",
  )

  @classmethod
  def _normalize_mobile_more_label(cls, text: str) -> str:
    collapsed = re.sub(r"\s+", "", (text or "").strip().lower())
    return collapsed.replace("…", "").replace("...", "").strip()

  @classmethod
  def _is_mobile_search_results_more_label(cls, text: str) -> bool:
    normalized = cls._normalize_mobile_more_label(text)
    if not normalized:
      return False
    if any(token in normalized for token in cls._MOBILE_SERP_MORE_BLOCK_TOKENS):
      return False
    primary = {cls._normalize_mobile_more_label(label) for label in cls._MOBILE_SERP_MORE_PRIMARY_LABELS}
    return normalized in primary

  @classmethod
  def _is_primary_mobile_footer_more_control(
    cls,
    *,
    aria: str = "",
    jsname: str = "",
    text: str = "",
  ) -> bool:
    if (jsname or "").strip() == "oHxHid":
      return True
    return cls._is_mobile_search_results_more_label(aria) or cls._is_mobile_search_results_more_label(text)

  @classmethod
  def _is_mobile_serp_footer_more_label(cls, text: str) -> bool:
    normalized = cls._normalize_mobile_more_label(text)
    if not normalized:
      return False
    if any(token in normalized for token in cls._MOBILE_SERP_MORE_BLOCK_TOKENS):
      return False
    exact = {cls._normalize_mobile_more_label(label) for label in cls._MOBILE_SERP_MORE_EXACT_LABELS}
    return normalized in exact

  @staticmethod
  def _current_serp_start_offset(page: Page) -> int:
    try:
      start_raw = parse_qs(urlparse(page.url).query).get("start", ["0"])[0]
      return max(0, int(start_raw))
    except Exception:
      return 0

  def _next_serp_start_offset(self, page: Page, profile: ProfileSpec) -> int:
    url_start = self._current_serp_start_offset(page)
    if self._is_mobile_profile(profile):
      tracked_start = max(0, self._mobile_serp_page - 1) * 10
      base_start = max(url_start, tracked_start)
      visible_start = self._visible_mobile_more_href_start(page)
      ip_index = self._mobile_url_ip_index(page.url or "")
      # #ip= infinite-scroll SERP: footer href often stays at the previous start=.
      if visible_start is not None and visible_start > base_start:
        return visible_start
      if ip_index > 0 and visible_start is not None and visible_start <= base_start:
        return visible_start
      return base_start + 10
    tracked_start = max(0, self._mobile_serp_page - 1) * 10
    return max(url_start, tracked_start) + 10

  def _visible_mobile_more_href_start(self, page: Page) -> Optional[int]:
    """On mobile infinite-scroll SERP, read start= from the visible 더보기 button."""
    try:
      raw = page.evaluate(
        """() => {
          const isTapVisible = (el) => {
            if (!el) return false;
            if (el.getAttribute('aria-hidden') === 'true') return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 6 && rect.height > 6;
          };
          const normalizeQ = (value) => (value || '').replace(/\\s+/g, '').trim().toLowerCase();
          const currentQ = normalizeQ(new URL(window.location.href).searchParams.get('q') || '');
          const parseStart = (href) => {
            if (!href) return null;
            try {
              const u = new URL(href, window.location.href);
              const raw = u.searchParams.get('start');
              if (raw === null || raw === '') return 0;
              const n = parseInt(raw, 10);
              return Number.isFinite(n) ? n : null;
            } catch (e) {
              const m = href.match(/[?&]start=(\\d+)/i);
              return m ? parseInt(m[1], 10) : null;
            }
          };
          const hrefMatchesCurrentQuery = (href) => {
            if (!href) return true;
            try {
              const u = new URL(href, window.location.href);
              const host = (u.hostname || '').toLowerCase();
              if (host && !host.includes('google.')) return false;
              const linkQ = normalizeQ(u.searchParams.get('q') || '');
              if (currentQ && linkQ && linkQ !== currentQ) return false;
              if (u.pathname.includes('/privacy') || u.pathname.includes('/policies')) return false;
              return true;
            } catch (e) {
              return false;
            }
          };
          let best = null;
          let bestBottom = -1;
          const selectors = ['a[jsname="oHxHid"]', 'a[aria-label="검색결과 더보기"]'];
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((el) => {
              if (!isTapVisible(el)) return;
              const href = el.getAttribute('href') || '';
              if (!hrefMatchesCurrentQuery(href)) return;
              const hrefStart = parseStart(href);
              if (hrefStart === null || hrefStart <= 0) return;
              const rect = el.getBoundingClientRect();
              if (rect.bottom <= bestBottom) return;
              bestBottom = rect.bottom;
              best = hrefStart;
            });
          }
          return best;
        }"""
      )
      if raw is None:
        return None
      return int(raw)
    except Exception:
      return None

  @staticmethod
  def _href_has_serp_start(href: str, start: int) -> bool:
    if not href:
      return False
    lowered = href.lower()
    if "tbm=" in lowered or "/maps" in lowered or "/url?" in lowered:
      return False
    if (
      "/search" not in lowered
      and "google." not in lowered
      and not lowered.startswith("?")
      and "start=" not in lowered
    ):
      return False
    tokens = (f"start={start}", f"start%3d{start}")
    return any(token in lowered for token in tokens)

  @staticmethod
  def _parse_href_start(href: str) -> Optional[int]:
    if not href:
      return None
    try:
      query = urlparse(href).query
      if query:
        raw = parse_qs(query).get("start", [""])[0]
        if str(raw).isdigit():
          return int(raw)
    except Exception:
      pass
    match = re.search(r"[?&]start=(\d+)", href, flags=re.IGNORECASE)
    if match:
      return int(match.group(1))
    return None

  @classmethod
  def _is_expected_mobile_pagination_href(cls, href: str, next_start: int) -> bool:
    """Reject stale mobile footer controls left in a cumulative SERP DOM."""
    return bool(
      href
      and cls._is_mobile_more_button_href(href)
      and cls._parse_href_start(href) == int(next_start)
    )

  @classmethod
  def _is_usable_mobile_more_href(
    cls,
    href: str,
    next_start: int,
    *,
    min_start: int = 0,
    allow_soft: bool = False,
  ) -> bool:
    """Accept exact next start, newer starts, or (soft) any footer more-results href.

    After #ip=1 append, Google often keeps a footer 더보기 whose href still shows
    the previous start=. Exact-only matching then falsely claims pagination ended.
    Soft mode is for availability / last-resort tap; advance is still verified.
    """
    if not href or not cls._is_mobile_more_button_href(href):
      return False
    href_start = cls._parse_href_start(href)
    if href_start is None:
      return False
    if href_start == int(next_start):
      return True
    if href_start > max(0, int(min_start)):
      return True
    if allow_soft and href_start > 0:
      return True
    return False

  @classmethod
  def _is_safe_mobile_next_href(cls, href: str, next_start: int) -> bool:
    """Validate a DOM next control without requiring it to expose start=."""
    raw = (href or "").strip()
    if not raw:
      return True
    lowered = raw.lower()
    if lowered.startswith(("javascript:", "data:")):
      return False
    if any(token in lowered for token in (
      "/maps", "tbm=", "udm=", "/url?", "/privacy", "/policies",
    )):
      return False
    try:
      parsed = urlparse(raw)
      host = (parsed.hostname or "").lower()
      if host and "google." not in host:
        return False
      if parsed.path and parsed.path not in ("", "/", "/search"):
        return False
    except Exception:
      return False
    href_start = cls._parse_href_start(raw)
    if href_start is None:
      return True
    if href_start == int(next_start):
      return True
    # Soft: after #ip append the footer > may still expose the previous start=.
    return href_start > 0 and href_start >= max(0, int(next_start) - 10)

  @classmethod
  def _is_mobile_next_control_semantics(
    cls,
    text: str,
    aria: str,
    rel: str,
    element_id: str,
  ) -> bool:
    """Accept only controls whose DOM semantics explicitly mean next page."""
    normalized_text = re.sub(r"\s+", "", (text or "").strip().lower())
    normalized_aria = re.sub(r"\s+", "", (aria or "").strip().lower())
    rel_tokens = {token.lower() for token in re.split(r"\s+", rel or "") if token}
    if (element_id or "").strip().lower() == "pnnext" or "next" in rel_tokens:
      return True
    if normalized_text in {">", "›", "»", "→"}:
      return True
    return any(token in normalized_aria for token in (
      "다음", "next", "moresearchresults", "검색결과더보기",
    ))

  @staticmethod
  def _mobile_url_ip_index(url: str) -> int:
    match = re.search(r"#ip=(\d+)", url or "", flags=re.IGNORECASE)
    if not match:
      return 0
    try:
      return max(0, int(match.group(1)))
    except Exception:
      return 0

  def _count_mobile_organic_links(self, page: Page) -> int:
    return len(self._mobile_organic_href_keys(page))

  def _mobile_organic_href_keys(self, page: Page) -> list[str]:
    try:
      keys = page.evaluate(
        """() => {
          const seen = new Set();
          const selectors = [
            '#rso a[data-ved][href]',
            'div#search a[data-ved][href]',
            '#rso a:has(h3)[href]',
            'div#search a:has(h3)[href]',
            'h3 a[href]',
            '#rso a[href]',
            'div#search a[href]',
            '[data-sokoban-container] a[href]',
            'a[href*="/url?q="]',
            'div.g a[href]',
            'a[data-ved][href^="http"]',
            'a[data-ved][href^="/url"]',
          ];
          const keyFor = (href) => {
            try {
              const url = new URL(href, window.location.href);
              const host = url.hostname.toLowerCase().replace(/^www\\./, '');
              const path = url.pathname.replace(/\\/$/, '') || '/';
              return host + path;
            } catch (e) {
              return href;
            }
          };
          const nodes = new Set();
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((el) => nodes.add(el));
          }
          for (const anchor of nodes) {
            const href = anchor.getAttribute('href') || '';
            if (!href || href.startsWith('#') || href.startsWith('javascript:')) continue;
            try {
              const host = new URL(href, window.location.href).hostname.toLowerCase();
              if (host.includes('google.')) continue;
            } catch (e) {
              continue;
            }
            seen.add(keyFor(href));
          }
          for (const cite of document.querySelectorAll('#rso cite, div#search cite')) {
            const anchor = cite.closest('a[href]');
            if (!anchor) continue;
            const href = anchor.getAttribute('href') || '';
            if (!href || href.startsWith('#')) continue;
            try {
              const host = new URL(href, window.location.href).hostname.toLowerCase();
              if (host.includes('google.')) continue;
            } catch (e) {
              continue;
            }
            seen.add(keyFor(href));
          }
          return Array.from(seen);
        }"""
      )
      return [str(key) for key in (keys or []) if str(key)]
    except Exception:
      return []

  @staticmethod
  def _is_google_serp_url(url: str) -> bool:
    lowered = (url or "").lower()
    if "google." not in lowered:
      return False
    return "/search" in lowered or "#ip=" in lowered

  @staticmethod
  def _is_mobile_more_button_href(href: str) -> bool:
    if not href:
      return True
    lowered = href.lower().strip()
    if lowered.startswith("#"):
      return True
    if lowered.startswith("/search") or lowered.startswith("?"):
      return True
    return "google." in lowered and "/search" in lowered

  def _recover_mobile_ai_overview_mistap(self, page: Page) -> bool:
    """Collapse AI 개요 if a plain 더보기 tap expanded it instead of paginating."""
    try:
      collapsed = page.evaluate(
        """() => {
          const isPlainMore = (el) => {
            const aria = (el.getAttribute('aria-label') || '').replace(/\\s+/g, '');
            const text = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
            const label = aria || text;
            if (!label || label.includes('검색결과')) return false;
            return label === '더보기' || label.startsWith('더보기') || label === '더보기∨';
          };
          const inFooter = (el) => Boolean(
            el.closest('a[jsname="oHxHid"], a[aria-label="검색결과 더보기"], #foot, #botstuff, #navd, nav[role="navigation"]')
          );
          const findAiSection = () => {
            const nodes = document.querySelectorAll('div, section, article');
            for (const node of nodes) {
              const t = (node.innerText || '').trim().slice(0, 60);
              if (t === 'AI 개요' || /^AI\\s*개요/i.test(t)) return node;
            }
            return null;
          };
          const section = findAiSection();
          if (!section) return '';
          const collapse = [...section.querySelectorAll('button, [role="button"], span, div[jsaction]')].find(
            (el) => /^(접기|show less|less)$/i.test((el.innerText || el.textContent || '').trim())
          );
          if (collapse) {
            collapse.click();
            return 'collapse';
          }
          const expanded = section.querySelector('[aria-expanded="true"]');
          if (expanded && isPlainMore(expanded) && !inFooter(expanded)) {
            expanded.click();
            return 'toggle';
          }
          const more = [...section.querySelectorAll('button, [role="button"], span, div[jsaction], a')].find(
            (el) => isPlainMore(el) && !inFooter(el)
          );
          if (more && section.querySelector('[aria-expanded="true"]')) {
            more.click();
            return 'more-toggle';
          }
          return '';
        }"""
      )
      if collapsed:
        self.logger(f"[Search] Mobile: collapsed AI 개요 after mistap ({collapsed})")
        page.wait_for_timeout(random.randint(400, 800))
        return True
    except Exception as exc:
      self.logger(f"[Search] Mobile: AI 개요 recovery warning — {exc}")
    return False

  def _recover_google_serp_after_mistap(
    self,
    page: Page,
    before_url: str = "",
  ) -> Literal["serp", "target", "failed"]:
    if self._is_google_serp_url(page.url):
      return "serp"
    if self._mobile_landed_on_target(page):
      self.logger(
        f"[Search] Mobile: on target site ({(page.url or '')[:120]}) — not treating as mistap"
      )
      return "target"
    wrong = page.url or ""
    self.logger(
      f"[Search] Mobile: left Google SERP after mistap ({wrong[:120]}) — going back"
    )
    page = self._history_back(
      page,
      self.logger,
      timeout_ms=15000,
      success_check=lambda: self._is_google_serp_url(page.url or ""),
      warning_label="Mobile go_back after mistap",
    )
    if self._is_google_serp_url(page.url):
      return "serp"
    if before_url and self._is_google_serp_url(before_url):
      try:
        page.goto(before_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(random.randint(300, 600))
      except Exception as exc:
        self.logger(f"[Search] Mobile: SERP URL restore failed — {exc}")
    if self._is_google_serp_url(page.url):
      return "serp"
    if self._mobile_landed_on_target(page):
      return "target"
    return "failed"

  def _snapshot_mobile_pagination_state(self, page: Page) -> dict:
    doc_height = 0
    organic_keys = self._mobile_organic_href_keys(page)
    try:
      doc_height = int(page.evaluate(
        "() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
      ))
    except Exception:
      pass
    return {
      "url": page.url,
      "start": self._current_serp_start_offset(page),
      "ip_index": self._mobile_url_ip_index(page.url),
      "organic_keys": organic_keys,
      "organic_count": len(organic_keys),
      "scroll_y": page.evaluate("() => window.scrollY || 0"),
      "doc_height": doc_height,
    }

  @staticmethod
  def _pagination_advanced(
    before: dict,
    after: dict,
    next_start: int,
    *,
    numeric_only: bool = False,
  ) -> bool:
    before_url = before.get("url") or ""
    after_url = after.get("url") or ""
    if not SerpBot._is_google_serp_url(after_url):
      return False
    if SerpBot._is_google_serp_url(before_url) and not SerpBot._is_google_serp_url(after_url):
      return False

    start_advanced = (
      after.get("start", 0) >= next_start
      and after.get("start", 0) > before.get("start", 0)
    ) or (
      after_url != before_url
      and after.get("start", 0) > before.get("start", 0)
    )
    if numeric_only:
      return start_advanced

    if start_advanced:
      return True
    if next_start > before.get("start", 0) and after.get("start", 0) >= next_start:
      return True
    if after_url != before_url and after.get("start", 0) >= next_start:
      return True
    if after.get("ip_index", 0) > before.get("ip_index", 0):
      return True
    if (
      after_url != before_url
      and "#ip=" in after_url
      and "#ip=" not in before_url
    ):
      return True
    if "filter=0" in after_url and "filter=0" not in before_url:
      return True
    before_keys = set(before.get("organic_keys") or [])
    after_keys = set(after.get("organic_keys") or [])
    organic_delta = after.get("organic_count", 0) - before.get("organic_count", 0)
    if before_keys and after_keys:
      new_key_count = len(after_keys - before_keys)
      retained_count = len(after_keys & before_keys)
      # Some mobile Google layouts replace one ten-result batch in-place:
      # count stays 10→10 and URL/#ip may stay unchanged, but result URLs change.
      if new_key_count >= 5:
        return True
      if new_key_count >= 3 and retained_count <= max(1, len(before_keys) // 2):
        return True
      before_ip = int(before.get("ip_index", 0) or 0)
      before_url_lower = before_url.lower()
      if before_ip > 0 or "#ip=" in before_url_lower:
        if new_key_count >= 2:
          return True
        if new_key_count >= 1 and organic_delta >= 1:
          return True
    if organic_delta >= 1:
      doc_grew = after.get("doc_height", 0) > before.get("doc_height", 0) + 180
      scroll_moved = after.get("scroll_y", 0) > before.get("scroll_y", 0)
      # AI 개요 펼침은 링크 1~2개만 늘어날 수 있음 — start/ip 무변화 시 대량 증가만 인정.
      if organic_delta >= 5:
        return True
      if organic_delta >= 3 and doc_grew and scroll_moved:
        return True
    return False

  def _wait_mobile_scroll_append(
    self,
    page: Page,
    before_state: dict,
    next_start: int,
    *,
    max_rounds: int = 5,
    stopped: Optional[Callable[[], bool]] = None,
    keyword: str = "",
  ) -> bool:
    """Scroll down after 더보기 tap and wait for infinite-scroll results to load."""
    try:
      immediate = self._snapshot_mobile_pagination_state(page)
      if self._pagination_advanced(before_state, immediate, next_start, numeric_only=False):
        self.logger(
          f"[Search] Mobile: pagination advanced before scroll wait "
          f"(start {before_state.get('start')}→{immediate.get('start')}, "
          f"links {before_state.get('organic_count')}→{immediate.get('organic_count')})"
          + (f" for '{keyword}'" if keyword else "")
        )
        return True
    except Exception:
      pass
    for round_idx in range(1, max_rounds + 1):
      if stopped and stopped():
        return False
      try:
        page.evaluate(
          """() => window.scrollTo(0, Math.max(
            document.body.scrollHeight,
            document.documentElement.scrollHeight
          ))"""
        )
      except Exception:
        scroll_page(page, random.randint(500, 950), mobile=True)
      page.wait_for_timeout(random.randint(700, 1200))
      after_state = self._snapshot_mobile_pagination_state(page)
      if self._pagination_advanced(
        before_state, after_state, next_start, numeric_only=False,
      ):
        return True
    return False

  def _force_mobile_scroll_next_batch(
    self,
    page: Page,
    before_state: dict,
    next_start: int,
    *,
    stopped: Optional[Callable[[], bool]] = None,
  ) -> bool:
    """Last-resort footer scroll to load the next mobile batch without direct URL."""
    try:
      page.evaluate(
        """() => {
          const vh = window.innerHeight || 800;
          const step = Math.min(Math.max(vh * 0.75, 380), 680);
          window.scrollBy(0, -step);
        }"""
      )
      page.wait_for_timeout(random.randint(280, 520))
    except Exception:
      pass
    self._scroll_to_serp_pagination(page, mobile=True, fast=True)
    if self._wait_mobile_scroll_append(
      page, before_state, next_start, max_rounds=10, stopped=stopped,
    ):
      return True
    return False

  def _mobile_serp_pagination_available(
    self,
    page: Page,
    profile: ProfileSpec,
    *,
    strict: bool = True,
  ) -> bool:
    """True only when a real mobile SERP next control exists (avoids stale 더보기 text)."""
    next_start = self._next_serp_start_offset(page, profile)
    min_start = max(0, int(next_start) - 10)
    if self._mobile_serp_more_button_available(
      page, next_start, min_start=min_start, soft=True,
    ):
      return True
    if self._mobile_serp_next_button_available(page, next_start):
      return True
    if strict:
      return False
    return self._has_next_serp_page(page, profile)

  def _mobile_serp_next_button_available(self, page: Page, next_start: int) -> bool:
    try:
      return bool(
        page.evaluate(
          """(nextStart) => {
            const isVisible = (el) => {
              if (!el) return false;
              if (el.getAttribute('aria-hidden') === 'true') return false;
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') return false;
              const rect = el.getBoundingClientRect();
              return rect.width > 6 && rect.height > 6;
            };
            const isInFooterZone = (el) => {
              if (el.closest('#foot, #botstuff, #navd, nav[role="navigation"]')) return true;
              const rect = el.getBoundingClientRect();
              const docBottom = Math.max(
                document.body.scrollHeight,
                document.documentElement.scrollHeight,
              );
              const absoluteBottom = rect.bottom + (window.scrollY || 0);
              // Near document bottom counts even when still slightly off-screen.
              if (absoluteBottom >= docBottom - 220) return true;
              const vh = window.innerHeight || 800;
              return rect.top >= vh * 0.55 && rect.bottom >= vh * 0.62;
            };
            const isNext = (el) => {
              const text = (el.innerText || el.textContent || '').replace(/\\s+/g, '').toLowerCase();
              const aria = (el.getAttribute('aria-label') || '').replace(/\\s+/g, '').toLowerCase();
              const rel = (el.getAttribute('rel') || '').toLowerCase().split(/\\s+/);
              const id = (el.id || '').toLowerCase();
              return id === 'pnnext' || rel.includes('next')
                || ['>', '›', '»', '→'].includes(text)
                || ['다음', 'next', 'moresearchresults', '검색결과더보기']
                  .some((token) => aria.includes(token));
            };
            const safeHref = (el) => {
              const href = (el.getAttribute('href') || '').trim();
              if (!href) return true;
              if (/^(javascript:|data:)/i.test(href)) return false;
              try {
                const u = new URL(href, window.location.href);
                const host = (u.hostname || '').toLowerCase();
                if (host && !host.includes('google.')) return false;
                if (u.pathname && !['', '/', '/search'].includes(u.pathname)) return false;
                if (u.pathname.includes('/maps') || u.searchParams.has('tbm')
                    || u.searchParams.has('udm')) return false;
                const rawStart = u.searchParams.get('start');
                if (rawStart !== null) {
                  const hrefStart = Number.parseInt(rawStart, 10);
                  if (Number.isFinite(hrefStart) && hrefStart > 0 && hrefStart < nextStart - 10) {
                    return false;
                  }
                }
                const currentQ = new URL(window.location.href).searchParams.get('q') || '';
                const linkQ = u.searchParams.get('q') || '';
                if (currentQ && linkQ && currentQ !== linkQ) return false;
                return true;
              } catch (e) {
                return false;
              }
            };
            const selectors = [
              'a#pnnext',
              '#pnnext a',
              '#pnnext',
              'a[rel="next"]',
              'a[aria-label*="다음"]',
              'a[aria-label*="Next" i]',
              '[role="button"][aria-label*="다음"]',
              '[role="button"][aria-label*="Next" i]',
              '#foot a, #foot button, #foot [role="button"]',
              '#botstuff a, #botstuff button, #botstuff [role="button"]',
              '#navd a, #navd button, #navd [role="button"]',
              'nav[role="navigation"] a, nav[role="navigation"] button, nav[role="navigation"] [role="button"]',
            ];
            for (const selector of selectors) {
              for (const el of document.querySelectorAll(selector)) {
                if (isVisible(el) && isInFooterZone(el) && isNext(el) && safeHref(el)) {
                  return true;
                }
              }
            }
            return false;
          }""",
          next_start,
        )
      )
    except Exception:
      return False

  def _mobile_viewport_footer_min_y(self, page: Page, ratio: float = 0.55) -> float:
    try:
      vh = float(page.evaluate("() => window.innerHeight || 800"))
      return vh * ratio
    except Exception:
      return 440.0

  def _mobile_serp_more_button_available(
    self,
    page: Page,
    next_start: int,
    *,
    min_start: int = 0,
    soft: bool = False,
  ) -> bool:
    try:
      found = page.evaluate(
        """(args) => {
          const nextStart = Number(args.nextStart) || 0;
          const minStart = Number(args.minStart) || 0;
          const soft = Boolean(args.soft);
          const vh = window.innerHeight || 800;
          const docBottom = Math.max(
            document.body.scrollHeight,
            document.documentElement.scrollHeight,
          );
          const isInFooterZone = (el) => {
            if (!el) return false;
            if (el.closest('#foot, #botstuff, #navd, nav[role="navigation"], .umwSD')) {
              return true;
            }
            const rect = el.getBoundingClientRect();
            const absoluteBottom = rect.bottom + (window.scrollY || 0);
            if (absoluteBottom >= docBottom - 240) return true;
            return rect.top >= vh * 0.55 && rect.bottom >= vh * 0.62;
          };
          const matchesUsablePage = (el) => {
            const jsname = el.getAttribute('jsname') || '';
            const aria = (el.getAttribute('aria-label') || '').replace(/\\s+/g, '');
            if (jsname === 'oHxHid' || aria === '검색결과더보기') return true;
            const href = (el.getAttribute('href') || '').trim();
            if (!href) return false;
            try {
              const u = new URL(href, window.location.href);
              const host = (u.hostname || '').toLowerCase();
              if (host && !host.includes('google.')) return false;
              if (u.pathname !== '/search') return false;
              if (u.searchParams.has('tbm') || u.searchParams.has('udm')) return false;
              const rawStart = u.searchParams.get('start');
              if (rawStart === null) return false;
              const hrefStart = Number.parseInt(rawStart, 10);
              if (!Number.isFinite(hrefStart) || hrefStart <= 0) return false;
              const currentQ = new URL(window.location.href).searchParams.get('q') || '';
              const linkQ = u.searchParams.get('q') || '';
              if (currentQ && linkQ && currentQ !== linkQ) return false;
              if (hrefStart === nextStart) return true;
              if (hrefStart > minStart) return true;
              return soft;
            } catch (e) {
              return false;
            }
          };
          const selectors = [
            'a[jsname="oHxHid"]',
            'a[aria-label="검색결과 더보기"]',
            'a[href*="start="][aria-label="검색결과 더보기"]',
          ];
          for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
              if (!el || el.getAttribute('aria-hidden') === 'true') continue;
              if (!isInFooterZone(el)) continue;
              if (!matchesUsablePage(el)) continue;
              const rect = el.getBoundingClientRect();
              if (rect.width > 4 && rect.height > 4) return true;
            }
          }
          return false;
        }""",
        {
          "nextStart": int(next_start),
          "minStart": int(min_start),
          "soft": bool(soft),
        },
      )
      return bool(found)
    except Exception:
      return False

  def _tap_google_mobile_more_js(
    self,
    page: Page,
    before_state: dict,
    next_start: int,
    delay_lo: float,
    delay_hi: float,
    *,
    numeric_only: bool = False,
    allow_no_href: bool = False,
  ) -> bool:
    """Tap known Google mobile control: a[jsname=oHxHid] / aria-label=검색결과 더보기."""
    min_start = max(0, int(next_start) - 10)
    try:
      clicked = page.evaluate(
        """(args) => {
          const nextStart = Number(args.nextStart) || 0;
          const minStart = Number(args.minStart) || 0;
          const allowNoHref = Boolean(args.allowNoHref);
          const isTapVisible = (el) => {
            if (!el) return false;
            if (el.getAttribute('aria-hidden') === 'true') return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 6 && rect.height > 6;
          };
          const normalizeQ = (value) => (value || '').replace(/\\s+/g, '').trim().toLowerCase();
          const currentQ = normalizeQ(new URL(window.location.href).searchParams.get('q') || '');
          const parseStart = (href) => {
            if (!href) return null;
            try {
              const u = new URL(href, window.location.href);
              const raw = u.searchParams.get('start');
              if (raw === null || raw === '') return 0;
              const n = parseInt(raw, 10);
              return Number.isFinite(n) ? n : null;
            } catch (e) {
              const m = href.match(/[?&]start=(\\d+)/i);
              return m ? parseInt(m[1], 10) : null;
            }
          };
          const hrefRank = (href) => {
            if (!href) return -1;
            try {
              const u = new URL(href, window.location.href);
              const host = (u.hostname || '').toLowerCase();
              if (host && !host.includes('google.')) return -1;
              const linkQ = normalizeQ(u.searchParams.get('q') || '');
              if (currentQ && linkQ && linkQ !== currentQ) return -1;
              if (u.pathname.includes('/privacy') || u.pathname.includes('/policies')) return -1;
              if (u.pathname !== '/search' && u.pathname !== '' && u.pathname !== '/') return -1;
              if (u.searchParams.has('tbm') || u.searchParams.has('udm')) return -1;
              const hrefStart = parseStart(href);
              if (hrefStart === null || hrefStart <= 0) return -1;
              if (hrefStart === nextStart) return 3000;
              if (hrefStart > minStart) return 2000;
              // Soft: stale start= on the live footer control after #ip append.
              return 1000;
            } catch (e) {
              return -1;
            }
          };
          const isPrimaryFooterMore = (el) => {
            if (!el) return false;
            const jsname = el.getAttribute('jsname') || '';
            const aria = (el.getAttribute('aria-label') || '').replace(/\\s+/g, '');
            return jsname === 'oHxHid' || aria === '검색결과더보기';
          };
          const selectors = [
            'a[jsname="oHxHid"]',
            'a[aria-label="검색결과 더보기"]',
          ];
          const vh = window.innerHeight || 800;
          const docBottom = Math.max(
            document.body.scrollHeight,
            document.documentElement.scrollHeight,
          );
          const isInFooterZone = (el) => {
            if (!el) return false;
            if (el.closest('#foot, #botstuff, #navd, nav[role="navigation"], .umwSD')) {
              return true;
            }
            const rect = el.getBoundingClientRect();
            const absoluteBottom = rect.bottom + (window.scrollY || 0);
            if (absoluteBottom >= docBottom - 240) return true;
            return rect.top >= vh * 0.55 && rect.bottom >= vh * 0.62;
          };
          let best = null;
          let bestScore = -1;
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((el) => {
              if (!isTapVisible(el)) return;
              if (!isInFooterZone(el)) return;
              const href = el.getAttribute('href') || '';
              let rank = hrefRank(href);
              if (rank < 0 && allowNoHref && isPrimaryFooterMore(el)) rank = 2800;
              if (rank < 0) return;
              const rect = el.getBoundingClientRect();
              const score = rank + (rect.bottom || 0);
              if (score <= bestScore) return;
              bestScore = score;
              best = el;
            });
          }
          if (!best) return null;
          best.scrollIntoView({ block: 'center', behavior: 'instant' });
          const rect = best.getBoundingClientRect();
          return {
            jsname: best.getAttribute('jsname') || '',
            href: best.getAttribute('href') || '',
            aria: best.getAttribute('aria-label') || '',
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2,
          };
        }""",
        {
          "nextStart": int(next_start),
          "minStart": int(min_start),
          "allowNoHref": bool(allow_no_href),
        },
      )
      if not clicked:
        return False
      picked = clicked if isinstance(clicked, dict) else {}
      jsname = str(picked.get("jsname") or "").strip()
      href_hint = str(picked.get("href") or "").strip()
      aria = str(picked.get("aria") or "").strip()
      tapped = False
      if jsname:
        locator = page.locator(f'a[jsname="{jsname}"]')
        for index in range(min(locator.count(), 6)):
          candidate = locator.nth(index)
          try:
            if not candidate.is_visible():
              continue
            if href_hint and (candidate.get_attribute("href") or "") != href_hint:
              continue
            human_click(candidate, delay_lo, delay_hi, page=page, mobile=True)
            tapped = True
            break
          except Exception:
            continue
      if not tapped and aria:
        try:
          locator = page.locator(f'[aria-label="{aria}"]')
          if locator.count() > 0:
            human_click(locator.first, delay_lo, delay_hi, page=page, mobile=True)
            tapped = True
        except Exception:
          pass
      if not tapped:
        try:
          tap_x = float(picked.get("x") or 0)
          tap_y = float(picked.get("y") or 0)
          if tap_x > 0 and tap_y > 0:
            dispatch_touch_tap(
              page, tap_x, tap_y, label="mobile-more-results",
            )
            tapped = True
        except Exception:
          pass
      if not tapped:
        return False
      random_delay(delay_lo, delay_hi)
      page.wait_for_timeout(random.randint(500, 1100))
      after_state = self._snapshot_mobile_pagination_state(page)
      if self._pagination_advanced(
        before_state, after_state, next_start, numeric_only=numeric_only,
      ):
        return True
      if self._wait_mobile_scroll_append(page, before_state, next_start):
        return True
      if not self._is_google_serp_url(after_state.get("url") or ""):
        if self._mobile_landed_on_target(page):
          return False
        self._recover_google_serp_after_mistap(page, before_state.get("url") or "")
      else:
        self._recover_mobile_ai_overview_mistap(page)
      return False
    except Exception as exc:
      self.logger(f"[Search] Mobile: JS tap failed — {exc}")
      return False

  def _save_mobile_more_capture(self, payload: dict) -> None:
    try:
      probe_path = data_dir() / "mobile_more_manual_capture.jsonl"
      with probe_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
      pass

  def _record_mobile_more_probe(
    self,
    *,
    keyword: str,
    next_start: int,
    before_state: Optional[dict],
    probe_data: Optional[dict],
    phase: str,
    extra: Optional[dict] = None,
  ) -> None:
    try:
      state = before_state or {}
      payload = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": phase,
        "keyword": keyword,
        "next_start": int(next_start),
        "url": state.get("url", ""),
        "scroll_y": state.get("scroll_y"),
        "doc_height": state.get("doc_height"),
        "organic_count": state.get("organic_count"),
        "probe_count": int((probe_data or {}).get("count") or 0),
        "probe_best": (probe_data or {}).get("best"),
        "probe_near_miss": (probe_data or {}).get("near_miss", [])[:5],
        "probe_candidates": (probe_data or {}).get("candidates", [])[:5],
      }
      if extra:
        payload.update(extra)
      probe_path = data_dir() / "mobile_more_button_probe.jsonl"
      with probe_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
      if phase == "before_tap" and int((probe_data or {}).get("count") or 0) == 0:
        near_miss = (probe_data or {}).get("near_miss") or []
        if near_miss:
          sample = near_miss[0]
          self.logger(
            f"[Search] Mobile probe: 0 footer matches for start={next_start} "
            f"('{keyword}') — near_miss={sample.get('reason')}: "
            f"jsname={sample.get('jsname') or '-'} "
            f"aria='{(sample.get('ariaLabel') or sample.get('text') or '')[:48]}' "
            f"href_start={sample.get('hrefStart')}"
          )
    except Exception:
      pass

  def _tap_mobile_footer_more_by_label(
    self,
    page: Page,
    profile: ProfileSpec,
    before_state: dict,
    next_start: int,
    delay_lo: float,
    delay_hi: float,
    *,
    keyword: str = "",
    stopped: Optional[Callable[[], bool]] = None,
  ) -> bool:
    """Tap footer 검색결과 더보기 even when href start= is missing (infinite-scroll SERP)."""
    _stopped = stopped or (lambda: False)
    if _stopped():
      return False
    self.logger(
      f"[Search] Mobile: footer label tap fallback for start={next_start} "
      f"('{keyword}')"
    )
    self._scroll_to_serp_pagination(page, mobile=True, fast=True)
    if self._tap_google_mobile_more_js(
      page, before_state, next_start, delay_lo, delay_hi, allow_no_href=True,
    ):
      return True
    if _stopped():
      return False
    locator_strategies: list[tuple[str, object]] = [
      ("role=link:검색결과 더보기", page.get_by_role("link", name="검색결과 더보기", exact=True)),
      ("role=button:검색결과 더보기", page.get_by_role("button", name="검색결과 더보기", exact=True)),
      ("text=검색결과 더보기", page.get_by_text("검색결과 더보기", exact=True)),
      ("aria-label=검색결과 더보기", page.locator('a[aria-label="검색결과 더보기"]')),
    ]
    for method, locator in locator_strategies:
      if self._try_tap_mobile_more_locator(
        page,
        method,
        locator,
        delay_lo,
        delay_hi,
        before_state,
        next_start,
        allow_no_href=True,
      ):
        return True
      if self._wait_mobile_scroll_append(
        page, before_state, next_start, stopped=_stopped, keyword=keyword,
      ):
        return True
    self._nudge_mobile_serp_footer_for_retry(page, profile)
    if self._tap_google_mobile_more_js(
      page, before_state, next_start, delay_lo, delay_hi, allow_no_href=True,
    ):
      return True
    return self._wait_mobile_scroll_append(
      page, before_state, next_start, stopped=_stopped, keyword=keyword,
    )

  def _dump_mobile_pagination_full_scan(self, page: Page, next_start: int) -> list[dict]:
    """Broad DOM dump for manual-tap diagnosis — every footer-ish '더보기' control."""
    try:
      items = page.evaluate(
        """(nextStart) => {
          const blocked = /비지니스|비즈니스|business|지도|maps|리뷰|review|장소|place/i;
          const rows = [];
          const nodes = document.querySelectorAll(
            'a, button, [role="button"], [role="link"], div[jsaction], span[jsaction]'
          );
          for (const el of nodes) {
            const text = (el.innerText || el.textContent || '').trim();
            const aria = (el.getAttribute('aria-label') || '').trim();
            const label = text || aria;
            if (!label || blocked.test(label)) continue;
            if (!/더보기|다음|more search results/i.test(label)) continue;
            const rect = el.getBoundingClientRect();
            if (rect.width < 4 || rect.height < 4) continue;
            const clickable = el.closest('a, button, [role="button"], [role="link"], [jsaction]') || el;
            rows.push({
              text: label.slice(0, 80),
              tag: clickable.tagName,
              id: clickable.id || '',
              className: (clickable.className || '').toString().slice(0, 160),
              role: clickable.getAttribute('role') || '',
              href: (clickable.getAttribute('href') || '').slice(0, 220),
              jsname: clickable.getAttribute('jsname') || '',
              jsaction: (clickable.getAttribute('jsaction') || '').slice(0, 100),
              ariaLabel: aria,
              bottom: rect.bottom,
              outerHTML: clickable.outerHTML.slice(0, 1000),
            });
          }
          rows.sort((a, b) => b.bottom - a.bottom);
          return rows.slice(0, 12);
        }""",
        next_start,
      )
    except Exception as exc:
      self.logger(f"[Search] Mobile dump: full scan failed — {exc}")
      return []

    self.logger(f"[Search] Mobile dump: found {len(items)} '더보기' controls on page")
    for index, item in enumerate(items, start=1):
      self.logger(
        f"[Search] Mobile dump #{index}: <{item.get('tag')}> "
        f"role={item.get('role') or '-'} jsname={item.get('jsname') or '-'} "
        f"aria='{(item.get('ariaLabel') or item.get('text') or '')[:50]}' "
        f"bottom={item.get('bottom')}"
      )
      self.logger(f"[Search] Mobile dump #{index} html: {(item.get('outerHTML') or '')[:700]}")
    return items

  def _wait_for_manual_mobile_more_capture(
    self,
    page: Page,
    profile: ProfileSpec,
    next_start: int,
    probe_before: Optional[dict],
    before_state: dict,
    wait_seconds: int = 20,
  ) -> bool:
    """Step 1 manual phase: user taps in AdsPower while we log before/after DOM."""
    full_scan = self._dump_mobile_pagination_full_scan(page, next_start)
    self.logger("=" * 72)
    self.logger(
      "[Search] Mobile MANUAL TAP >>> AdsPower 창에서 '검색결과 더보기' 버튼을 직접 터치해 주세요."
    )
    self.logger(
      f"[Search] Mobile MANUAL TAP >>> {wait_seconds}초 대기 "
      f"(현재 start={before_state.get('start')}, links={before_state.get('organic_count')})"
    )
    self.logger("=" * 72)

    capture = {
      "phase": "manual_wait",
      "next_start": next_start,
      "before": before_state,
      "probe_before": probe_before,
      "full_scan_before": full_scan,
    }
    self._save_mobile_more_capture(capture)

    for elapsed in range(1, wait_seconds + 1):
      page.wait_for_timeout(1000)
      try:
        after_state = self._snapshot_mobile_pagination_state(page)
      except Exception:
        continue
      if self._pagination_advanced(before_state, after_state, next_start):
        probe_after = self._probe_mobile_search_results_more_button(
          page,
          next_start + 10,
        )
        payload = {
          "phase": "manual_success",
          "next_start": next_start,
          "elapsed_sec": elapsed,
          "before": before_state,
          "after": after_state,
          "probe_before": probe_before,
          "probe_after": probe_after,
          "full_scan_before": full_scan,
          "full_scan_after": self._dump_mobile_pagination_full_scan(page, next_start + 10),
        }
        self._save_mobile_more_capture(payload)
        self.logger(
          f"[Search] Mobile MANUAL TAP >>> 성공 ({elapsed}s) "
          f"start {before_state.get('start')}→{after_state.get('start')}, "
          f"ip {before_state.get('ip_index')}→{after_state.get('ip_index')}, "
          f"links {before_state.get('organic_count')}→{after_state.get('organic_count')}"
        )
        url_page = self._current_search_results_page_num(page)
        if url_page > self._mobile_serp_page:
          self._mobile_serp_page = url_page
        elif after_state.get("ip_index", 0) > before_state.get("ip_index", 0):
          self._mobile_serp_page = max(
            self._mobile_serp_page,
            after_state.get("ip_index", 0) + 1,
          )
        else:
          self._advance_mobile_serp_page()
        return True
      if elapsed in (15, 30, 45, 60, 75):
        self.logger(
          f"[Search] Mobile MANUAL TAP >>> 아직 대기 중... ({elapsed}/{wait_seconds}s) "
          "— '검색결과 더보기'를 터치해 주세요"
        )

    self.logger("[Search] Mobile MANUAL TAP >>> 시간 초과 — 수동 터치가 감지되지 않았습니다.")
    self._save_mobile_more_capture({
      "phase": "manual_timeout",
      "next_start": next_start,
      "before": before_state,
      "probe_before": probe_before,
      "full_scan_before": full_scan,
    })
    return False

  def _try_tap_mobile_more_locator(
    self,
    page: Page,
    method: str,
    locator,
    delay_lo: float,
    delay_hi: float,
    before_state: dict,
    next_start: int,
    *,
    numeric_only: bool = False,
    allow_no_href: bool = False,
  ) -> bool:
    try:
      if locator.count() == 0:
        return False
      footer_min_y = self._mobile_viewport_footer_min_y(page)
      min_start = max(0, int(next_start) - 10)
      best = None
      best_score = -1.0
      for index in range(min(locator.count(), 12)):
        item = locator.nth(index)
        if not item.is_visible():
          continue
        try:
          if (item.get_attribute("aria-hidden") or "").lower() == "true":
            continue
        except Exception:
          pass
        item_href = item.get_attribute("href") or ""
        item_jsname = item.get_attribute("jsname") or ""
        item_aria = item.get_attribute("aria-label") or ""
        item_text = ""
        try:
          item_text = (item.inner_text(timeout=500) or "").strip()
        except Exception:
          pass
        exact = self._is_expected_mobile_pagination_href(item_href, next_start)
        usable = self._is_usable_mobile_more_href(
          item_href, next_start, min_start=min_start, allow_soft=True,
        )
        primary_footer = self._is_primary_mobile_footer_more_control(
          aria=item_aria, jsname=item_jsname, text=item_text,
        )
        if not usable and allow_no_href and primary_footer:
          usable = True
        if not usable:
          continue
        box = item.bounding_box()
        if not box:
          continue
        top = box["y"]
        bottom = box["y"] + box["height"]
        # Prefer footer-zone controls; still allow near-bottom soft matches.
        if top < footer_min_y and not exact:
          continue
        score = bottom + (10_000.0 if exact else 0.0)
        if score > best_score:
          best_score = score
          best = item
      if best is None:
        return False

      href = best.get_attribute("href") or ""
      jsname = best.get_attribute("jsname") or ""
      aria = best.get_attribute("aria-label") or ""
      best_text = ""
      try:
        best_text = (best.inner_text(timeout=500) or "").strip()
      except Exception:
        pass
      href_start = self._parse_href_start(href)
      primary_footer = self._is_primary_mobile_footer_more_control(
        aria=aria, jsname=jsname, text=best_text,
      )
      if not self._is_usable_mobile_more_href(
        href, next_start, min_start=min_start, allow_soft=True,
      ) and not (allow_no_href and primary_footer):
        self.logger(
          f"[Search] Mobile: skipping stale/non-pagination control "
          f"href_start={href_start} expected={next_start} "
          f"href='{href[:80]}' aria='{aria[:40]}'"
        )
        return False
      if aria and aria != "검색결과 더보기":
        if jsname != "oHxHid" and not primary_footer:
          return False
      best.scroll_into_view_if_needed(timeout=6000)
      page.wait_for_timeout(random.randint(150, 350))
      human_click(best, delay_lo, delay_hi, page=page, mobile=True)
      page.wait_for_timeout(random.randint(500, 1100))
      try:
        page.wait_for_load_state("domcontentloaded", timeout=12000)
      except Exception:
        pass
      after_state = self._snapshot_mobile_pagination_state(page)
      if self._pagination_advanced(
        before_state, after_state, next_start, numeric_only=numeric_only,
      ):
        return True
      if not self._is_google_serp_url(after_state.get("url") or ""):
        self._recover_google_serp_after_mistap(page, before_state.get("url") or "")
      elif not self._pagination_advanced(
        before_state, after_state, next_start, numeric_only=numeric_only,
      ):
        self._recover_mobile_ai_overview_mistap(page)
      self.logger(
        f"[Search] Mobile: tap via {method} did not advance pagination "
        f"(links {before_state.get('organic_count')}→{after_state.get('organic_count')})"
      )
    except Exception as exc:
      self.logger(f"[Search] Mobile: tap via {method} failed — {exc}")
    return False

  def _probe_mobile_search_results_more_button(
    self,
    page: Page,
    next_start: int,
  ) -> Optional[dict]:
    """Step 1: locate '검색결과 더보기', dump DOM attributes/HTML for tuning selectors."""
    try:
      min_start = max(0, int(next_start) - 10)
      probe = page.evaluate(
        """(args) => {
          const nextStart = Number(args.nextStart) || 0;
          const minStart = Number(args.minStart) || 0;
          const blockedText = /비지니스|비즈니스|business|지도|maps|리뷰|review|장소|place|전화|영업/i;
          const primaryLabels = ['검색결과 더보기', '검색결과 더 보기', 'more search results'];
          const normalize = (value) => (value || '').replace(/\\s+/g, '').trim().toLowerCase();
          const currentQ = normalize(new URL(window.location.href).searchParams.get('q') || '');
          const matchesPrimary = (text) => {
            const norm = normalize(text);
            if (!norm || blockedText.test(text || '')) return false;
            return primaryLabels.some((label) => norm === normalize(label));
          };
          const parseStart = (href) => {
            if (!href) return null;
            try {
              const u = new URL(href, window.location.href);
              const raw = u.searchParams.get('start');
              if (raw === null || raw === '') return 0;
              const n = parseInt(raw, 10);
              return Number.isFinite(n) ? n : null;
            } catch (e) {
              const m = href.match(/[?&]start=(\\d+)/i);
              return m ? parseInt(m[1], 10) : null;
            }
          };
          const hrefMatchesCurrentQuery = (href) => {
            if (!href) return true;
            try {
              const u = new URL(href, window.location.href);
              const host = (u.hostname || '').toLowerCase();
              if (host && !host.includes('google.')) return false;
              const linkQ = normalize(u.searchParams.get('q') || '');
              if (currentQ && linkQ && linkQ !== currentQ) return false;
              if (u.pathname.includes('/privacy') || u.pathname.includes('/policies')) return false;
              return true;
            } catch (e) {
              return false;
            }
          };
          const startRank = (hrefStart) => {
            if (hrefStart === nextStart) return 3200;
            if (hrefStart !== null && hrefStart > minStart) return 2400;
            if (hrefStart !== null && hrefStart > 0) return 1600;
            return -1;
          };
          const isPrimaryFooterMore = (el) => {
            if (!el) return false;
            const jsname = el.getAttribute('jsname') || '';
            const aria = normalize(el.getAttribute('aria-label') || '');
            const text = normalize((el.innerText || el.textContent || '').trim());
            if (jsname === 'oHxHid') return true;
            return primaryLabels.some((label) => aria === normalize(label) || text === normalize(label));
          };
          const scoreFooterControl = (el, href) => {
            const hrefStart = parseStart(href || el.getAttribute('href') || '');
            const rank = startRank(hrefStart);
            if (rank >= 0) return rank;
            if (isPrimaryFooterMore(el) && isInFooterZone(el)) return 2600;
            return -1;
          };
          const isVisible = (el) => {
            if (!el || el.getAttribute('aria-hidden') === 'true') return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 6 && rect.height > 6;
          };
          const clickableOf = (el) => {
            if (!el) return null;
            return el.closest('a, button, [role="button"], [role="link"], [jsaction]') || el;
          };
          const isFooterPagination = (el) => Boolean(
            el.closest('a[jsname="oHxHid"], a[aria-label="검색결과 더보기"], #foot, #botstuff, #navd, nav[role="navigation"], .umwSD')
          );
          const isAiOverviewControl = (el) => {
            if (!el || isFooterPagination(el)) return false;
            const aria = (el.getAttribute('aria-label') || '').replace(/\\s+/g, '');
            const text = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
            const label = aria || text;
            if (label && label.includes('검색결과')) return false;
            if (label === '더보기' || label.startsWith('더보기')) return true;
            let node = el;
            for (let i = 0; i < 14 && node; i++) {
              const heading = (node.innerText || '').trim().slice(0, 40);
              if (heading === 'AI 개요' || /^AI\\s*개요/i.test(heading)) return true;
              node = node.parentElement;
            }
            return false;
          };
          const vh = window.innerHeight || 800;
          const docBottom = Math.max(
            document.body.scrollHeight,
            document.documentElement.scrollHeight,
          );
          const isInFooterZone = (el) => {
            if (!el) return false;
            if (el.closest('#foot, #botstuff, #navd, nav[role="navigation"], .umwSD')) return true;
            const rect = el.getBoundingClientRect();
            const absoluteBottom = rect.bottom + (window.scrollY || 0);
            if (absoluteBottom >= docBottom - 240) return true;
            return rect.top >= vh * 0.55 && rect.bottom >= vh * 0.62;
          };
          const candidates = [];
          const nearMiss = [];
          const noteNearMiss = (el, reason, href) => {
            if (!el) return;
            const rect = el.getBoundingClientRect();
            nearMiss.push({
              reason,
              text: (el.getAttribute('aria-label') || el.innerText || el.textContent || '').trim().slice(0, 80),
              tag: el.tagName,
              jsname: el.getAttribute('jsname') || '',
              ariaLabel: (el.getAttribute('aria-label') || '').trim(),
              href: (href || el.getAttribute('href') || '').slice(0, 220),
              hrefStart: parseStart(href || el.getAttribute('href') || ''),
              bottom: rect.bottom,
            });
          };
          const push = (el, score, method, href) => {
            if (!el || isAiOverviewControl(el) || !isInFooterZone(el)) return;
            const rect = el.getBoundingClientRect();
            candidates.push({
              score: score + (rect.bottom || 0),
              text: (el.getAttribute('aria-label') || el.innerText || el.textContent || '').trim(),
              tag: el.tagName,
              id: el.id || '',
              className: (el.className || '').toString().slice(0, 180),
              role: el.getAttribute('role') || '',
              href: (href || el.getAttribute('href') || '').slice(0, 220),
              hrefStart: parseStart(href || el.getAttribute('href') || ''),
              jsaction: (el.getAttribute('jsaction') || '').slice(0, 120),
              jsname: el.getAttribute('jsname') || '',
              dataVed: el.getAttribute('data-ved') || '',
              ariaLabel: (el.getAttribute('aria-label') || '').trim(),
              outerHTML: el.outerHTML.slice(0, 900),
              parentTag: el.parentElement ? el.parentElement.tagName : '',
              parentId: el.parentElement ? (el.parentElement.id || '') : '',
              parentClass: el.parentElement
                ? (el.parentElement.className || '').toString().slice(0, 120)
                : '',
              x: rect.left + rect.width / 2,
              y: rect.top + rect.height / 2,
              bottom: rect.bottom,
              method,
            });
          };

          for (const selector of [
            'a[jsname="oHxHid"]',
            'a[aria-label="검색결과 더보기"]',
          ]) {
            document.querySelectorAll(selector).forEach((el) => {
              if (!el || el.getAttribute('aria-hidden') === 'true') return;
              if (!isVisible(el)) return;
              const href = el.getAttribute('href') || '';
              if (!hrefMatchesCurrentQuery(href)) {
                if (isPrimaryFooterMore(el)) noteNearMiss(el, 'href_query_mismatch', href);
                return;
              }
              const rank = scoreFooterControl(el, href);
              if (rank < 0) {
                if (isPrimaryFooterMore(el)) {
                  noteNearMiss(
                    el,
                    isInFooterZone(el) ? 'href_start_mismatch' : 'not_footer_zone',
                    href,
                  );
                }
                return;
              }
              push(el, rank, selector, href);
            });
          }

          const nodes = document.querySelectorAll(
            'a, button, [role="button"], [role="link"], div[jsaction], span[jsaction], div[role="button"]'
          );
          for (const el of nodes) {
            const text = (el.innerText || el.textContent || '').trim();
            const aria = (el.getAttribute('aria-label') || '').trim();
            if (!matchesPrimary(text) && !matchesPrimary(aria)) continue;
            const clickable = clickableOf(el);
            if (!clickable || !isVisible(clickable) || isAiOverviewControl(clickable)) continue;
            if (!isInFooterZone(clickable)) {
              if (isPrimaryFooterMore(clickable)) noteNearMiss(clickable, 'not_footer_zone', href);
              continue;
            }
            const rect = clickable.getBoundingClientRect();
            const href = clickable.getAttribute('href') || '';
            if (!hrefMatchesCurrentQuery(href)) {
              if (isPrimaryFooterMore(clickable)) noteNearMiss(clickable, 'href_query_mismatch', href);
              continue;
            }
            const rank = scoreFooterControl(clickable, href);
            if (rank < 0) {
              if (isPrimaryFooterMore(clickable)) noteNearMiss(clickable, 'href_start_mismatch', href);
              continue;
            }
            let score = rank + rect.bottom;
            if (clickable.closest('#foot, #navd, #botstuff, nav[role="navigation"]')) score += 600;
            candidates.push({
              score,
              text: text || aria,
              tag: clickable.tagName,
              id: clickable.id || '',
              className: (clickable.className || '').toString().slice(0, 180),
              role: clickable.getAttribute('role') || '',
              href: href.slice(0, 220),
              hrefStart: parseStart(href),
              jsaction: (clickable.getAttribute('jsaction') || '').slice(0, 120),
              jsname: clickable.getAttribute('jsname') || '',
              dataVed: clickable.getAttribute('data-ved') || '',
              ariaLabel: aria,
              outerHTML: clickable.outerHTML.slice(0, 900),
              parentTag: clickable.parentElement ? clickable.parentElement.tagName : '',
              parentId: clickable.parentElement ? (clickable.parentElement.id || '') : '',
              parentClass: clickable.parentElement
                ? (clickable.parentElement.className || '').toString().slice(0, 120)
                : '',
              x: rect.left + rect.width / 2,
              y: rect.top + rect.height / 2,
              bottom: rect.bottom,
            });
          }
          candidates.sort((a, b) => b.score - a.score);
          return {
            url: window.location.href,
            nextStart,
            minStart,
            count: candidates.length,
            best: candidates[0] || null,
            candidates: candidates.slice(0, 5),
            near_miss: nearMiss.slice(0, 8),
          };
        }""",
        {"nextStart": int(next_start), "minStart": int(min_start)},
      )
    except Exception as exc:
      self.logger(f"[Search] Mobile probe: DOM scan failed — {exc}")
      return None

    if not probe:
      return None

    return probe

  def _tap_mobile_search_results_more_button(
    self,
    page: Page,
    next_start: int,
    delay_lo: float,
    delay_hi: float,
    probe: Optional[dict] = None,
    before_state: Optional[dict] = None,
    *,
    numeric_only: bool = False,
  ) -> bool:
    """Step 2: tap '검색결과 더보기' via jsname/aria-label locators (from probe log)."""
    if before_state is None:
      before_state = self._snapshot_mobile_pagination_state(page)

    if self._tap_google_mobile_more_js(
      page, before_state, next_start, delay_lo, delay_hi, numeric_only=numeric_only,
      allow_no_href=True,
    ):
      return True

    locator_strategies: list[tuple[str, object]] = []
    if probe:
      jsname = (probe.get("jsname") or "").strip()
      if jsname:
        locator_strategies.append(
          (f"jsname={jsname}", page.locator(f'a[jsname="{jsname}"]'))
        )
      aria = (probe.get("ariaLabel") or probe.get("text") or "").strip()
      if aria:
        locator_strategies.append(
          (f'aria-label="{aria}"', page.locator(f'[aria-label="{aria}"]'))
        )

    locator_strategies.extend([
      (
        f'href-start={next_start}+aria',
        page.locator(f'a[href*="start={next_start}"][aria-label="검색결과 더보기"]'),
      ),
      ('aria-label=검색결과 더보기', page.locator('a[aria-label="검색결과 더보기"]')),
      ('role=button:검색결과 더보기', page.get_by_role("button", name="검색결과 더보기", exact=True)),
      ('role=link:검색결과 더보기', page.get_by_role("link", name="검색결과 더보기", exact=True)),
      ('text=검색결과 더보기', page.get_by_text("검색결과 더보기", exact=True)),
    ])

    seen_methods: set[str] = set()
    for method, locator in locator_strategies:
      if method in seen_methods:
        continue
      seen_methods.add(method)
      if self._try_tap_mobile_more_locator(
        page,
        method,
        locator,
        delay_lo,
        delay_hi,
        before_state,
        next_start,
        numeric_only=False,
      ):
        return True

    # Do not reuse probe coordinates after a DOM click attempt. Infinite-scroll
    # can replace the footer before this fallback runs, making the old point land
    # on an organic result (observed as an unintended manolja.co.kr navigation).
    return False

  def _click_mobile_serp_next(
    self,
    page: Page,
    profile: ProfileSpec,
    before_state: Optional[dict] = None,
    *,
    target_start: Optional[int] = None,
  ) -> bool:
    """Tap mobile SERP > / next pagination when 검색결과 더보기 is absent."""
    self._scroll_to_serp_pagination(page, mobile=True, fast=False)
    if before_state is None:
      before_state = self._snapshot_mobile_pagination_state(page)
    next_start = int(
      target_start if target_start is not None
      else self._next_serp_start_offset(page, profile)
    )
    delay_lo, delay_hi = self._serp_delay_bounds()
    footer_min_y = self._mobile_viewport_footer_min_y(page)
    next_selectors = (
      "a#pnnext",
      "#pnnext a",
      "#pnnext",
      'a[rel="next"]',
      'span#pnnext a',
      'a[aria-label*="다음"]',
      'a[aria-label*="Next" i]',
      'a[aria-label*="More search results" i]',
      '[role="button"][aria-label*="다음"]',
      '[role="button"][aria-label*="Next" i]',
      '#foot a, #foot button, #foot [role="button"]',
      '#botstuff a, #botstuff button, #botstuff [role="button"]',
      '#navd a, #navd button, #navd [role="button"]',
      'nav[role="navigation"] a, nav[role="navigation"] button, '
      'nav[role="navigation"] [role="button"]',
    )
    for selector in next_selectors:
      try:
        locator = page.locator(selector)
        if locator.count() == 0:
          continue
        best = None
        best_bottom = -1.0
        for index in range(min(locator.count(), 8)):
          link = locator.nth(index)
          if not link.is_visible():
            continue
          href = link.get_attribute("href") or ""
          text = link.inner_text(timeout=1000) or ""
          aria = link.get_attribute("aria-label") or ""
          rel = link.get_attribute("rel") or ""
          element_id = link.get_attribute("id") or ""
          if not self._is_mobile_next_control_semantics(
            text, aria, rel, element_id,
          ):
            continue
          if not self._is_safe_mobile_next_href(href, next_start):
            continue
          box = link.bounding_box()
          if not box:
            continue
          if box["y"] < footer_min_y:
            continue
          bottom = box["y"] + box["height"]
          if bottom > best_bottom:
            best_bottom = bottom
            best = link
        if best is None:
          continue
        best_href = best.get_attribute("href") or ""
        if not self._is_safe_mobile_next_href(best_href, next_start):
          continue
        self.logger("[Search] Mobile: tapping > (next) pagination control in footer")
        human_click(
          best,
          delay_lo,
          delay_hi,
          page=page,
          mobile=True,
        )
        try:
          page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
          pass
        page.wait_for_timeout(random.randint(450, 950))
        after_state = self._snapshot_mobile_pagination_state(page)
        if self._pagination_advanced(
          before_state, after_state, next_start, numeric_only=False,
        ):
          return True
        if self._wait_mobile_scroll_append(page, before_state, next_start):
          return True
      except Exception:
        continue
    return False

  def _click_mobile_pagination_next(
    self,
    page: Page,
    profile: ProfileSpec,
    before_state: Optional[dict] = None,
    *,
    keyword: str = "",
    target_start: Optional[int] = None,
    stopped: Optional[Callable[[], bool]] = None,
  ) -> bool:
    """Advance mobile SERP: 검색결과 더보기 first, then > / next control."""
    if before_state is None:
      before_state = self._snapshot_mobile_pagination_state(page)
    if self._click_mobile_more_button(
      page,
      profile,
      before_state,
      keyword=keyword,
      target_start=target_start,
      stopped=stopped,
    ):
      return True
    if stopped and stopped():
      return False
    next_start = int(
      target_start if target_start is not None
      else self._next_serp_start_offset(page, profile)
    )
    self.logger(
      f"[Search] Mobile: 더보기 unavailable/failed for start={next_start} "
      f"('{keyword}') — trying > pagination"
    )
    return self._click_mobile_serp_next(
      page, profile, before_state, target_start=next_start,
    )

  def _click_mobile_more_button(
    self,
    page: Page,
    profile: ProfileSpec,
    before_state: Optional[dict] = None,
    *,
    keyword: str = "",
    target_start: Optional[int] = None,
    stopped: Optional[Callable[[], bool]] = None,
  ) -> bool:
    """Tap mobile SERP next-page via 검색결과 더보기 (footer zone only)."""
    _stopped = stopped or (lambda: False)
    delay_lo, delay_hi = self._serp_delay_bounds()
    next_start = int(
      target_start if target_start is not None else self._next_serp_start_offset(page, profile)
    )
    if before_state is None:
      before_state = self._snapshot_mobile_pagination_state(page)

    self._scroll_to_serp_pagination(page, mobile=True, fast=False)
    probe_data = self._probe_mobile_search_results_more_button(page, next_start)
    probe_best: Optional[dict] = (probe_data or {}).get("best") if probe_data else None
    probe_count = int((probe_data or {}).get("count") or 0)
    self._record_mobile_more_probe(
      keyword=keyword,
      next_start=next_start,
      before_state=before_state,
      probe_data=probe_data,
      phase="before_tap",
    )

    if probe_count == 0 and not probe_best:
      self.logger(
        f"[Search] Mobile: 검색결과 더보기 not in DOM for start={next_start} "
        f"('{keyword}') — trying footer label tap before scroll fallback"
      )
      if self._tap_mobile_footer_more_by_label(
        page,
        profile,
        before_state,
        next_start,
        delay_lo,
        delay_hi,
        keyword=keyword,
        stopped=_stopped,
      ):
        self._record_mobile_more_probe(
          keyword=keyword,
          next_start=next_start,
          before_state=before_state,
          probe_data=probe_data,
          phase="label_tap_success",
        )
        return True
      if self._force_mobile_scroll_next_batch(
        page, before_state, next_start, stopped=_stopped,
      ):
        return True
      return False

    for tap_attempt in range(1, 3):
      if _stopped():
        return False
      if self._tap_mobile_search_results_more_button(
        page,
        next_start,
        delay_lo,
        delay_hi,
        probe_best,
        before_state,
        numeric_only=False,
      ):
        return True
      if self._wait_mobile_scroll_append(
        page, before_state, next_start, stopped=_stopped, keyword=keyword,
      ):
        return True
      if tap_attempt < 2 and not _stopped():
        self.logger(
          f"[Search] Mobile: 더보기 tap retry {tap_attempt}/2 — nudge footer (no top reset)"
        )
        self._nudge_mobile_serp_footer_for_retry(page, profile)
        probe_data = self._probe_mobile_search_results_more_button(page, next_start)
        if probe_data and probe_data.get("best"):
          probe_best = probe_data.get("best")
        elif int((probe_data or {}).get("count") or 0) == 0:
          if self._tap_mobile_footer_more_by_label(
            page,
            profile,
            before_state,
            next_start,
            delay_lo,
            delay_hi,
            keyword=keyword,
            stopped=_stopped,
          ):
            return True
          break

    if _stopped():
      return False

    if self._tap_mobile_footer_more_by_label(
      page,
      profile,
      before_state,
      next_start,
      delay_lo,
      delay_hi,
      keyword=keyword,
      stopped=_stopped,
    ):
      return True

    if self._force_mobile_scroll_next_batch(
      page, before_state, next_start, stopped=_stopped,
    ):
      return True

    self.logger(
      f"[Search] Mobile: 더보기 tap failed for start={next_start} ('{keyword}')"
    )
    return False

  def _collect_organic_result_hrefs(self, page: Page, profile: Optional[ProfileSpec] = None) -> list[str]:
    mobile = self._is_mobile_profile(profile) if profile else False
    hrefs: list[str] = []
    seen: set[str] = set()

    if mobile:
      for batch in (
        self._collect_organic_result_hrefs_js(page),
        self._collect_organic_result_hrefs_locator(page, mobile=True),
      ):
        for href in batch:
          if href in seen:
            continue
          seen.add(href)
          hrefs.append(href)
      if hrefs:
        return hrefs

    return self._collect_organic_result_hrefs_locator(page, mobile=mobile)

  def _collect_organic_result_hrefs_locator(self, page: Page, *, mobile: bool) -> list[str]:
    hrefs: list[str] = []
    seen: set[str] = set()
    for selector in self._organic_link_selectors(mobile):
      links = page.locator(selector)
      for index in range(min(links.count(), 140)):
        raw_href = links.nth(index).get_attribute("href") or ""
        resolved = self._resolve_result_href(raw_href)
        if not resolved or resolved in seen:
          continue
        if self._is_valid_organic_href(resolved):
          seen.add(resolved)
          hrefs.append(resolved)
    return hrefs

  @staticmethod
  def _is_valid_organic_href(resolved: str) -> bool:
    lowered = resolved.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "#")):
      return False
    host = urlparse(resolved).netloc.lower()
    return "google." not in host

  @staticmethod
  def _href_matches_target(href: str, target: str) -> bool:
    if not href or not target:
      return False
    normalized = SerpBot._normalize_domain(href)
    target_norm = SerpBot._normalize_domain(target)
    if not target_norm:
      return False
    if normalized and (normalized == target_norm or normalized.endswith(f".{target_norm}")):
      return True
    lowered = (href or "").lower()
    return target_norm in lowered

  @staticmethod
  def _collect_organic_result_hrefs_js(page: Page) -> list[str]:
    try:
      raw_hrefs = page.evaluate(
        """() => {
          const out = [];
          const seen = new Set();
          const selectors = [
            '#rso a[data-ved][href]',
            'div#search a[data-ved][href]',
            '#rso a:has(h3)[href]',
            'div#search a:has(h3)[href]',
            'h3 a[href]',
            '#rso a[href]',
            'div#search a[href]',
            '[data-sokoban-container] a[href]',
            'a[href*="/url?q="]',
            'div.g a[href]',
            'a[data-ved][href^="http"]',
            'a[data-ved][href^="/url"]',
          ];
          const nodes = new Set();
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((el) => nodes.add(el));
          }
          for (const anchor of nodes) {
            const href = anchor.getAttribute('href') || '';
            if (!href || href.startsWith('#') || href.startsWith('javascript:')) continue;
            out.push(href);
          }
          for (const cite of document.querySelectorAll('#rso cite, div#search cite')) {
            const anchor = cite.closest('a[href]');
            if (!anchor) continue;
            const href = anchor.getAttribute('href') || '';
            if (!href || href.startsWith('#') || href.startsWith('javascript:')) continue;
            out.push(href);
          }
          return out;
        }"""
      )
    except Exception:
      return []

    hrefs: list[str] = []
    seen: set[str] = set()
    for raw_href in raw_hrefs or []:
      resolved = SerpBot._resolve_result_href(str(raw_href))
      if not resolved or resolved in seen:
        continue
      if not SerpBot._is_valid_organic_href(resolved):
        continue
      seen.add(resolved)
      hrefs.append(resolved)
    return hrefs

  def _has_next_serp_page(
    self,
    page: Page,
    profile: Optional[ProfileSpec] = None,
    log: Optional[Callable[[str], None]] = None,
  ) -> bool:
    mobile = self._is_mobile_profile(profile) if profile else False
    try:
      self._scroll_to_serp_pagination(page, mobile=mobile, fast=True)
      return bool(
        self._evaluate_with_retry(
          page,
          """() => {
            const cssSelectors = [
              'a#pnnext',
              'a[rel="next"]',
              'a[aria-label*="Next" i]',
              'a[aria-label*="다음"]',
              'a[aria-label*="More search results" i]',
            ];
            const isVisible = (el) => {
              if (!el) return false;
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') return false;
              const rect = el.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            };
            for (const selector of cssSelectors) {
              const el = document.querySelector(selector);
              if (isVisible(el)) return true;
            }
            const nextLabels = [
              '검색결과더보기', '검색결과 더보기',
              'more search results', 'more results',
            ];
            const blocked = [
              '비지니스', '비즈니스', 'business', '지도', 'maps', '리뷰', 'review', '장소', 'place',
              '전화', '영업', 'store', 'local', '이전', 'previous',
            ];
            const normalize = (value) => (value || '').replace(/\\s+/g, '').trim().toLowerCase()
              .replace(/…/g, '').replace(/\\.\\.\\./g, '').trim();
            const isFooterPagination = (el) => Boolean(
              el.closest('a[jsname="oHxHid"], a[aria-label="검색결과 더보기"], #foot, #botstuff, #navd, nav[role="navigation"]')
            );
            const isAiOverviewControl = (el) => {
              if (!el || isFooterPagination(el)) return false;
              const aria = (el.getAttribute('aria-label') || '').replace(/\\s+/g, '');
              const text = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
              const label = aria || text;
              if (label && label.includes('검색결과')) return false;
              if (label === '더보기' || label.startsWith('더보기')) return true;
              let node = el;
              for (let i = 0; i < 14 && node; i++) {
                const heading = (node.innerText || '').trim().slice(0, 40);
                if (heading === 'AI 개요' || /^AI\\s*개요/i.test(heading)) return true;
                node = node.parentElement;
              }
              return false;
            };
            const isFooterMore = (text) => {
              const t = normalize(text);
              if (!t || blocked.some((token) => t.includes(token))) return false;
              return nextLabels.some((label) => t === normalize(label));
            };
            const vh = window.innerHeight || 800;
            const minTop = vh * 0.72;
            const clickables = document.querySelectorAll(
              'a, button, span, div[role="button"], span[role="link"]'
            );
            for (const el of clickables) {
              if (isAiOverviewControl(el)) continue;
              const text = (el.innerText || el.textContent || '').trim();
              if (!text) continue;
              if (!isFooterMore(text)) continue;
              const rect = el.getBoundingClientRect();
              if (rect.top < minTop) continue;
              if (isVisible(el)) return true;
            }
            return false;
          }""",
          log=log,
        )
      )
    except Exception as exc:
      self.logger(f"[Search] Next-page detection warning: {exc}")
      return False

  def _detect_serp_last_page(
    self,
    page: Page,
    served_page_num: int,
    log: Optional[Callable[[str], None]] = None,
  ) -> Optional[int]:
    detected = self._evaluate_with_retry(
      page,
      """(servedPage) => {
        let maxPage = servedPage || 1;

        const currentSelectors = [
          '#botstuff [aria-current="page"]',
          'a[aria-current="page"]',
          'span[aria-current="page"]',
        ];
        for (const selector of currentSelectors) {
          const el = document.querySelector(selector);
          const text = (el?.innerText || el?.textContent || '').trim();
          if (/^\\d+$/.test(text)) {
            maxPage = Math.max(maxPage, parseInt(text, 10));
          }
        }

        const pageLinks = document.querySelectorAll(
          '#botstuff a[href*="start="], nav[role="navigation"] a[href*="start="], ' +
          'a[href*="start="][aria-label*="Page" i], table a[href*="start="]'
        );
        for (const link of pageLinks) {
          const text = (link.innerText || link.textContent || '').trim();
          if (/^\\d+$/.test(text)) {
            maxPage = Math.max(maxPage, parseInt(text, 10));
          }
          const href = link.getAttribute('href') || '';
          const match = href.match(/[?&]start=(\\d+)/);
          if (match) {
            maxPage = Math.max(maxPage, Math.floor(parseInt(match[1], 10) / 10) + 1);
          }
        }

        const hasNext = !!document.querySelector(
          'a#pnnext, a[rel="next"], a[aria-label*="Next" i], a[aria-label*="다음"]'
        );
        if (!hasNext) {
          return maxPage;
        }
        return maxPage;
      }""",
      served_page_num,
      log=log,
    )
    try:
      value = int(detected)
      return value if value > 0 else None
    except (TypeError, ValueError):
      return None

  @staticmethod
  def _apply_serp_cap_floor(
    serp_last_page: Optional[int],
    history_page: Optional[int],
    max_pages: int,
  ) -> int:
    cap = serp_last_page or max_pages
    cap = min(max(1, int(cap)), max(1, int(max_pages)))
    if history_page and int(history_page) > 1:
      cap = max(cap, min(int(history_page), max_pages))
    return cap

  def _update_serp_last_page(
    self,
    page: Page,
    served_page_num: int,
    current_cap: Optional[int],
    configured_max: int,
    profile: Optional[ProfileSpec] = None,
    log: Optional[Callable[[str], None]] = None,
    *,
    history_page: Optional[int] = None,
  ) -> int:
    mobile = self._is_mobile_profile(profile) if profile else False
    if mobile:
      pagination_available = self._mobile_serp_pagination_available(page, profile, strict=True)
      cap = self._mobile_effective_page_cap(
        served_page_num=served_page_num,
        configured_max=configured_max,
        pagination_available=pagination_available,
      )
      cap = self._apply_serp_cap_floor(cap, history_page, configured_max)
      if current_cap != cap:
        self.logger(
          f"[Search] Mobile SERP cap: min(config={configured_max}, google) = {cap} "
          f"(pagination={'yes' if pagination_available else 'no'})"
        )
      return cap

    self._scroll_to_serp_pagination(page, mobile=mobile, fast=True)
    detected = self._detect_serp_last_page(page, served_page_num, log=log)
    has_next = self._has_next_serp_page(page, profile, log=log)

    if detected and detected > 0:
      cap = min(detected, configured_max)
    elif has_next:
      cap = configured_max
    else:
      floor = served_page_num
      if history_page and int(history_page) > 1:
        floor = min(int(history_page), configured_max)
      cap = min(
        configured_max,
        max(served_page_num, current_cap or floor, floor),
      )

    cap = max(cap, served_page_num)
    if current_cap:
      cap = max(min(current_cap, configured_max), cap)
      cap = min(cap, configured_max)

    cap = self._apply_serp_cap_floor(cap, history_page, configured_max)

    if current_cap != cap:
      label = "Mobile" if mobile else "Desktop"
      self.logger(f"[Search] {label} SERP cap: min(config={configured_max}, google) = {cap}")
    return cap

  def _reset_mobile_serp_page(self) -> None:
    self._mobile_serp_page = 1
    self._clear_mobile_manual_target_signal()

  def _sync_mobile_serp_page(self, page: Page, profile: ProfileSpec) -> None:
    url_page = self._current_search_results_page_num(page)
    if url_page > 1:
      self._mobile_serp_page = max(self._mobile_serp_page, url_page)
    ip_index = self._mobile_url_ip_index(page.url or "")
    if ip_index > 0:
      self._mobile_serp_page = max(self._mobile_serp_page, ip_index + 1)
    if self._mobile_serp_page < 1:
      self._mobile_serp_page = 1

  def _advance_mobile_serp_page(self) -> None:
    self._mobile_serp_page = max(1, int(self._mobile_serp_page)) + 1

  def _serp_page_num(self, page: Page, profile: ProfileSpec) -> int:
    if self._is_mobile_profile(profile):
      return max(1, int(self._mobile_serp_page))
    return self._current_search_results_page_num(page)

  @staticmethod
  def _current_search_results_page_num(page: Page) -> int:
    try:
      parsed = urlparse(page.url)
      start_raw = parse_qs(parsed.query).get("start", ["0"])[0]
      start = int(start_raw)
      if start >= 0:
        return (start // 10) + 1
    except Exception:
      pass

    selectors = (
      '#botstuff [aria-current="page"]',
      'a[aria-current="page"]',
      'span[aria-current="page"]',
    )
    for selector in selectors:
      try:
        marker = page.locator(selector).first
        if marker.count() == 0:
          continue
        text = (marker.inner_text() or "").strip()
        if text.isdigit():
          return int(text)
      except Exception:
        continue
    return 1

  def _goto_search_page(
    self,
    page: Page,
    keyword: str,
    page_num: int,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
  ) -> Page:
    target_page = max(1, int(page_num))
    current_page = self._current_search_results_page_num(page)
    if current_page == target_page and target_page > 1:
      return page
    start = 0 if target_page <= 1 else (target_page - 1) * 10
    search_url = self._google_search_url(keyword, profile, start=start)
    try:
      page = self._safe_goto(
        page,
        search_url,
        profile,
        self.logger,
        wait_until="domcontentloaded",
        timeout=60000,
      )
      self._serp_pause()
    except Exception as exc:
      self._report_failure(page, profile, f"goto_search_page_{page_num}", exc, on_failure)
      raise
    return page

  @staticmethod
  def _desktop_page_should_skip(
    page_num: int,
    *,
    max_pages: int,
    visible_serp_cap: Optional[int],
    history_page: Optional[int],
  ) -> bool:
    configured_max = max(1, int(max_pages))
    if page_num < 1 or page_num > configured_max:
      return True
    visible_limit = min(configured_max, max(1, int(visible_serp_cap or configured_max)))
    if page_num <= visible_limit:
      return False
    if history_page and page_num == int(history_page):
      return False
    return True

  @staticmethod
  def _build_search_order(
    max_pages: int,
    history_page: Optional[int],
    visible_serp_cap: Optional[int] = None,
  ) -> list[int]:
    configured_max = max(1, int(max_pages))
    visible_cap = configured_max
    if visible_serp_cap and int(visible_serp_cap) > 0:
      visible_cap = min(configured_max, int(visible_serp_cap))

    history_hint = None
    if history_page is not None:
      try:
        parsed = int(history_page)
      except (TypeError, ValueError):
        parsed = 0
      if parsed > 1:
        history_hint = min(parsed, configured_max)

    if history_hint is None:
      if visible_cap <= 1:
        return [1]
      return list(range(1, visible_cap + 1))

    order: list[int] = []
    seen: set[int] = set()

    def add(page: int) -> None:
      if page < 1 or page > configured_max or page in seen:
        return
      seen.add(page)
      order.append(page)

    if history_hint <= visible_cap:
      add(history_hint)
      if history_hint > 1:
        add(history_hint - 1)
      if history_hint + 1 <= visible_cap:
        add(history_hint + 1)
      for page in range(history_hint - 2, 0, -1):
        add(page)
      for page in range(history_hint + 2, visible_cap + 1):
        add(page)
    else:
      for page in range(visible_cap, 0, -1):
        add(page)
      add(history_hint)

    return order or [1]

  def _dwell_on_site(
    self,
    page: Page,
    total_seconds: float,
    stopped: Callable[[], bool],
    profile: ProfileSpec,
    *,
    network: Optional[NetworkOptimizer] = None,
  ) -> None:
    mobile = self._is_mobile_profile(profile)
    dwell_start = time.monotonic()
    dwell_end = dwell_start + max(5.0, float(total_seconds))
    hard_deadline = dwell_start + max(5.0, float(total_seconds) + 25.0)
    link_lo = max(0, int(getattr(self.config, "internal_link_min", 1) or 0))
    link_hi = max(link_lo, int(getattr(self.config, "internal_link_max", 1) or link_lo))
    target_internal_clicks = (
      random.randint(link_lo, link_hi) if link_hi > 0 else 0
    )
    if network is not None and network.is_target_storm_active():
      target_internal_clicks = 0
      self.logger(
        "[Target] Skipping internal links during dwell (target image-block storm detected)"
      )
    internal_clicks = 0
    next_internal_at = dwell_start + random.uniform(
      float(total_seconds) * 0.15,
      max(float(total_seconds) * 0.40, 10.0),
    )
    self.logger(
      f"[Target] Dwell plan: human read — scroll up/down, text select"
      f"{', mobile touch scroll' if mobile else ''}"
      f", internal links {target_internal_clicks}x"
    )
    try:
      page.evaluate("window.scrollTo(0, 0)")
      page.wait_for_timeout(random.randint(450, 950))
    except Exception:
      pass

    # Gentle first pass down the page (not a burst to the footer).
    for _ in range(random.randint(2, 4) if mobile else random.randint(2, 3)):
      self._human_scroll_cycle(
        page, stopped, mobile=mobile, intensity="normal", dwell=True,
      )
      if stopped():
        return

    while time.monotonic() < dwell_end and not stopped():
      if time.monotonic() >= hard_deadline:
        self.logger(
          f"[Target] Dwell hard-stop reached ({int(total_seconds)}s budget). "
          "Proceeding to close profile flow."
        )
        break
      wall_elapsed = time.monotonic() - dwell_start
      self.logger(
        f"[Target] Dwell progress {wall_elapsed:.0f}/{total_seconds:.0f}s"
      )

      for _ in range(random.randint(2, 4) if mobile else random.randint(1, 3)):
        self._human_scroll_cycle(
          page, stopped, mobile=mobile, intensity="normal", dwell=True,
        )
        if stopped():
          return

      if random.random() < 0.44:
        self._perform_warmup_text_hold(page, profile)

      now = time.monotonic()
      if (
        internal_clicks < target_internal_clicks
        and now >= next_internal_at
        and (dwell_end - now) > 10.0
        and not stopped()
        and not (network is not None and network.is_target_storm_active())
      ):
        return_to_landing = random.random() < 0.5
        if self._click_internal_link(
          page,
          stopped,
          post_click_read=True,
          profile=profile,
          return_to_landing=return_to_landing,
        ):
          internal_clicks += 1
          where = "landing" if return_to_landing else "internal page"
          self.logger(
            f"[Target] Internal link click {internal_clicks}/{target_internal_clicks} "
            f"(continuing on {where})"
          )
          next_internal_at = now + random.uniform(
            max(8.0, float(total_seconds) * 0.12),
            max(16.0, float(total_seconds) * 0.28),
          )
        else:
          next_internal_at = now + random.uniform(5.0, 10.0)

      remaining_ms = int(min(4500, max(0.0, dwell_end - time.monotonic()) * 1000))
      if remaining_ms >= 500:
        self._dwell_active_wait(page, remaining_ms, stopped, profile)

    while internal_clicks < target_internal_clicks and not stopped():
      if network is not None and network.is_target_storm_active():
        break
      if time.monotonic() >= hard_deadline:
        break
      return_to_landing = random.random() < 0.5
      if self._click_internal_link(
        page,
        stopped,
        post_click_read=False,
        profile=profile,
        return_to_landing=return_to_landing,
      ):
        internal_clicks += 1
        self.logger(
          f"[Target] Internal link click {internal_clicks}/{target_internal_clicks} "
          f"(final pass)"
        )
      else:
        break

  @staticmethod
  def _page_scroll_metrics(page: Page) -> dict:
    try:
      metrics = page.evaluate(
        """() => {
          const height = Math.max(
            document.body.scrollHeight,
            document.documentElement.scrollHeight
          );
          const viewport = window.innerHeight || 800;
          const y = window.scrollY || 0;
          const bottom = Math.max(0, height - viewport);
          return {
            y,
            height,
            viewport,
            atBottom: y >= bottom - 48,
            atTop: y <= 48,
          };
        }"""
      )
      return metrics if isinstance(metrics, dict) else {}
    except Exception:
      return {}

  def _dwell_scroll_delta(self, page: Page, *, mobile: bool) -> int:
    metrics = self._page_scroll_metrics(page)
    at_bottom = bool(metrics.get("atBottom"))
    at_top = bool(metrics.get("atTop"))
    down_range = (220, 620) if mobile else (180, 720)
    up_range = (140, 420) if mobile else (120, 480)
    if at_bottom:
      go_up = random.random() < 0.82
    elif at_top:
      go_up = random.random() < 0.10
    else:
      go_up = random.random() < 0.44
    if go_up:
      return -random.randint(*up_range)
    return random.randint(*down_range)

  def _dwell_active_wait(
    self,
    page: Page,
    wait_ms: int,
    stopped: Callable[[], bool],
    profile: ProfileSpec,
  ) -> None:
    mobile = self._is_mobile_profile(profile)
    elapsed = 0
    next_action_at = random.randint(650, 1300)
    while elapsed < wait_ms and not stopped():
      if page.is_closed():
        return
      step = min(250, wait_ms - elapsed)
      if not self._safe_page_wait(page, step):
        return
      elapsed += step
      if elapsed < next_action_at:
        continue
      action = random.random()
      if action < 0.72:
        scroll_page(page, self._dwell_scroll_delta(page, mobile=mobile), mobile=mobile)
      elif action < 0.86:
        self._perform_warmup_text_hold(page, profile)
      next_action_at = elapsed + random.randint(700, 1500)

  def _human_scroll_cycle(
    self,
    page: Page,
    stopped: Callable[[], bool],
    *,
    mobile: bool = False,
    intensity: str = "normal",
    dwell: bool = False,
  ) -> None:
    if dwell:
      scroll_count = random.randint(3, 6) if mobile else random.randint(2, 5)
      delay_range = (180, 520) if mobile else (300, 900)
    elif intensity == "high":
      scroll_count = random.randint(5, 9) if mobile else random.randint(3, 6)
      delay_range = (120, 450) if mobile else (250, 800)
    else:
      scroll_count = random.randint(2, 5)
      delay_range = (350, 1400)

    for _ in range(scroll_count):
      if stopped():
        return
      if dwell:
        delta = self._dwell_scroll_delta(page, mobile=mobile)
      else:
        delta_range_down = (220, 620) if mobile else (180, 720)
        delta_range_up = (100, 280) if mobile else (120, 360)
        go_up = random.random() < (0.18 if mobile else 0.25)
        delta = (
          -random.randint(*delta_range_up)
          if go_up
          else random.randint(*delta_range_down)
        )
      scroll_page(page, delta, mobile=mobile)
      self._interruptible_wait(page, random.randint(*delay_range), stopped)

  def _select_some_text(self, page: Page) -> bool:
    selectors = (
      "main p, article p, p",
      "main li, article li, li",
      "main h2, main h3, article h2, article h3",
    )
    try:
      for selector in selectors:
        blocks = page.locator(selector)
        count = min(blocks.count(), 24)
        if count == 0:
          continue
        candidates = list(range(count))
        random.shuffle(candidates)
        for index in candidates[:8]:
          block = blocks.nth(index)
          box = block.bounding_box()
          if not box or box["width"] < 80 or box["height"] < 10:
            continue
          start_x = box["x"] + random.uniform(8, min(box["width"] * 0.35, 90))
          end_x = min(box["x"] + box["width"] - 8, start_x + random.uniform(40, 180))
          y = box["y"] + min(box["height"] * 0.6, box["height"] - 4)
          page.mouse.move(start_x, y, steps=random.randint(4, 9))
          page.mouse.down()
          page.mouse.move(end_x, y + random.uniform(-2.0, 2.0), steps=random.randint(8, 18))
          page.mouse.up()
          page.wait_for_timeout(random.randint(350, 1200))
          page.mouse.click(end_x + random.uniform(-12, 12), y + random.uniform(-4, 4))
          return True
    except Exception:
      return False
    return False

  def _interruptible_wait(
    self,
    page: Page,
    wait_ms: int,
    stopped: Callable[[], bool],
    *,
    scroll_mobile: bool = False,
  ) -> None:
    elapsed = 0
    next_scroll_at = (
      random.randint(700, 1400) if scroll_mobile else wait_ms + 1
    )
    while elapsed < wait_ms and not stopped():
      if page.is_closed():
        return
      step = min(250, wait_ms - elapsed)
      if not self._safe_page_wait(page, step):
        return
      elapsed += step
      if scroll_mobile and elapsed >= next_scroll_at:
        scroll_page(page, random.randint(160, 420), mobile=True)
        next_scroll_at = elapsed + random.randint(700, 1400)

  def _click_internal_link(
    self,
    page: Page,
    stopped: Callable[[], bool],
    post_click_read: bool = True,
    profile: Optional[ProfileSpec] = None,
    *,
    return_to_landing: bool = True,
  ) -> bool:
    if stopped():
      return False
    mobile = self._is_mobile_profile(profile) if profile else False
    domain = self._normalize_domain(
      self._session_target_domain or self.config.primary_target_domain
    )
    selector = (
      f'a[href*="{domain}"], a[href^="/"], a[href^="./"], a[href^="../"]'
      if mobile
      else f'a[href*="{domain}"]:visible, a[href^="/"]:visible'
    )
    links = page.locator(selector)
    link_count = min(links.count(), 30)
    if link_count == 0:
      self.logger("[Target] No internal links found")
      return False

    candidates = list(range(link_count))
    random.shuffle(candidates)
    for index in candidates:
      if stopped():
        return False
      link = links.nth(index)
      try:
        href = link.get_attribute("href", timeout=4000) or ""
      except Exception as exc:
        self.logger(
          f"[Target] Internal link #{index + 1}/{link_count} skipped "
          f"({exc.__class__.__name__})"
        )
        continue
      if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
        continue
      href_lower = href.lower()
      is_internal = (
        domain in href_lower
        or href.startswith(("/", "./", "../"))
        or href_lower.startswith(domain)
      )
      if not is_internal:
        continue
      target_url = self._resolve_internal_href(page, href)
      landing_url = (page.url or "").strip()
      clicked = False
      try:
        human_click(
          link,
          self.config.action_delay_min,
          self.config.action_delay_max,
          page=page,
          mobile=mobile,
        )
        clicked = True
      except Exception:
        clicked = False
      if clicked:
        try:
          page.wait_for_load_state("domcontentloaded", timeout=8000 if mobile else 10000)
        except Exception:
          pass
      elif target_url:
        try:
          page = self._safe_goto(
            page,
            target_url,
            profile,
            self.logger,
            wait_until="domcontentloaded",
            timeout=10000,
          )
        except Exception:
          continue
      else:
        continue

      if post_click_read:
        for _ in range(random.randint(1, 3)):
          self._human_scroll_cycle(
            page, stopped, mobile=mobile, intensity="normal", dwell=True,
          )
          if stopped():
            return True
        if random.random() < 0.35:
          self._perform_warmup_text_hold(page, profile) if profile else False
        wait_ms = random.randint(4_000, 9_000) if mobile else random.randint(6_000, 14_000)
        if profile is not None:
          self._dwell_active_wait(page, wait_ms, stopped, profile)
        else:
          self._interruptible_wait(page, wait_ms, stopped)

      if return_to_landing:
        page = self._go_back_after_internal_link(page, stopped, landing_url=landing_url)
      return True
    self.logger("[Target] Internal links existed but none were clickable")
    return False

  @staticmethod
  def _resolve_internal_href(page: Page, href: str) -> str:
    href = (href or "").strip()
    if not href:
      return ""
    if href.startswith(("http://", "https://")):
      return href
    try:
      return str(page.evaluate(
        "(path) => new URL(path, window.location.href).href",
        href,
      ))
    except Exception:
      parsed = urlparse(page.url)
      origin = f"{parsed.scheme}://{parsed.netloc}"
      if href.startswith("/"):
        return f"{origin}{href}"
      base = page.url.rsplit("/", 1)[0] + "/"
      return f"{base}{href.lstrip('./')}"

  def _micro_scroll_during_wait(
    self,
    page: Page,
    wait_ms: int,
    stopped: Callable[[], bool],
    profile: Optional[ProfileSpec] = None,
  ) -> None:
    mobile = self._is_mobile_profile(profile) if profile else False
    elapsed = 0
    interval = min(30_000, max(2_000, wait_ms // 3))
    while elapsed < wait_ms and not stopped():
      if page.is_closed():
        return
      sleep_for = min(interval, wait_ms - elapsed)
      end = time.time() + sleep_for / 1000
      while time.time() < end and not stopped():
        if page.is_closed():
          return
        if not self._safe_page_wait(page, 250):
          return
      if stopped():
        return
      try:
        scroll_page(
          page,
          random.choice((-1, 1)) * random.randint(120, 420),
          mobile=mobile,
        )
        random_delay(self.config.action_delay_min, self.config.action_delay_max)
      except Exception:
        return
      elapsed += sleep_for

  @staticmethod
  def _normalize_domain(value: str) -> str:
    if "://" in value:
      host = urlparse(value).netloc
    else:
      host = value.split("/")[0]
    return host.lower().removeprefix("www.")
