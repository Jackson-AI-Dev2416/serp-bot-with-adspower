import json
import random
import re
import threading
import time
from typing import Callable, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from playwright.sync_api import Browser, Page, sync_playwright

from config.bot_config import BotConfig
from core.profile_status import ProfileStatus, UiStatusKey
from services.adspower_manager import ProfileSpec
from services.captcha_solver import CaptchaSolver
from services.google_consent import dismiss_google_consent, is_google_consent_present, seed_google_consent_cookies
from services.network_optimizer import NetworkOptimizer, PagePhase, classify_network_error
from utils.crash_reporter import capture_exception
from utils.csv_logger import CsvRankLogger, KeywordHistoryLogger
from utils.human import enable_mobile_touch, human_click, human_type, micro_scroll, random_delay, scroll_page, dispatch_touch_tap

StatusCallback = Callable[[ProfileStatus], None]
UiStatusCallback = Callable[[str, str], None]
FailureCallback = Callable[[ProfileSpec, str, BaseException], None]
TrafficCallback = Callable[[int, int], None]
KeywordExhaustedCallback = Callable[[str], None]


class SerpBot:
  def __init__(self, config: BotConfig, logger: Callable[[str], None]):
    self.config = config
    self.logger = logger
    self.captcha = CaptchaSolver(config.capsolver_api_key, logger)
    self.csv = CsvRankLogger("data/results.csv")
    self.keyword_history = KeywordHistoryLogger(config.target_domain)
    self._last_search_exhausted = False
    self._last_search_exhaustion_eligible = False
    self._session_network: Optional[NetworkOptimizer] = None
    self._mobile_serp_page = 1
    self._mobile_more_probe_done = False
    self._mobile_serp_end_reached = False

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
    mobile = self._is_mobile_profile(profile)
    if not network.attach(page, mobile=mobile):
      log("[Network] Route optimizer unavailable — Save-Data headers only")
    try:
      network.apply_phase_headers(page)
      network.monitor.set_baseline()
    except Exception as exc:
      log(f"[Network] Phase header setup warning: {exc}")

  def _cleanup_session(
    self,
    browser: Optional[Browser],
    log: Callable[[str], None],
    network: Optional[NetworkOptimizer],
    on_session_cleanup: Optional[Callable[[], None]] = None,
  ) -> None:
    if network:
      network.report_traffic(force=True, include_baseline=True)
      network.monitor.append_session_log(network.profile_name, network._current_keyword)

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
    on_status: Optional[StatusCallback] = None,
    on_ui_status: Optional[UiStatusCallback] = None,
    on_traffic: Optional[TrafficCallback] = None,
    on_failure: Optional[FailureCallback] = None,
    on_keyword_exhausted: Optional[KeywordExhaustedCallback] = None,
    on_session_cleanup: Optional[Callable[[], None]] = None,
  ) -> str:
    def set_status(status: ProfileStatus) -> None:
      if on_status:
        on_status(status)
      if on_ui_status:
        key, text = status.to_ui()
        on_ui_status(key, text)

    def stopped() -> bool:
      return bool(stop_event and stop_event.is_set())

    def log(msg: str) -> None:
      self.logger(f"[{profile.name}] {msg}")

    set_status(ProfileStatus.RUNNING)
    log("[Session] Starting")
    self.captcha.reset_session_state()
    self.captcha.set_session_logger(log)
    self.captcha.update_api_key(self.config.capsolver_api_key)
    proxy_label = self._proxy_label(profile)
    self.captcha.set_session_context(
      profile_id=profile.profile_id,
      profile_name=profile.name,
      proxy=proxy_label,
    )
    target_host = self._normalize_domain(self.config.target_domain)
    network = NetworkOptimizer(target_host, log, profile_name=profile.name)
    network.set_phase(PagePhase.GOOGLE_SERP)
    self._session_network = network
    if self.captcha.automated_mode:
      log(f"[CapSolver] Automated mode enabled ({self.captcha._mask_api_key()})")
    else:
      log("[CapSolver] No API key configured — manual captcha mode")

    with sync_playwright() as playwright:
      browser: Browser = playwright.chromium.connect_over_cdp(ws_endpoint)
      page: Optional[Page] = None
      session_download_bytes = 0
      pending_report_bytes = 0
      report_threshold_bytes = 128 * 1024

      def report_traffic(force: bool = False) -> None:
        nonlocal pending_report_bytes
        if force:
          network.report_traffic(force=True)
        if not on_traffic:
          return
        if not force and pending_report_bytes < report_threshold_bytes:
          return
        delta = pending_report_bytes
        pending_report_bytes = 0
        if delta > 0 or force:
          on_traffic(session_download_bytes, delta)

      def handle_response(response) -> None:
        nonlocal session_download_bytes, pending_report_bytes
        size_bytes = self._response_size_bytes(response)
        if size_bytes <= 0:
          return
        session_download_bytes += size_bytes
        pending_report_bytes += size_bytes
        network.monitor.record_allowed(size_bytes)
        report_traffic(force=False)

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
        page.on("response", handle_response)
        network.reattach_page(page, mobile=self._is_mobile_profile(profile))
        if self._is_mobile_profile(profile):
          self._prepare_mobile_session(page, profile, log)
        log(f"[Captcha] Rotated tab due to captcha ({reason})")
        return page

      try:
        page = self._open_work_page(browser, profile, log)
        work_ref = None
        if not self._is_ios_profile(profile):
          work_ref = self._attach_tab_guard(page.context, page, log)
        if page.is_closed():
          page = self._recover_work_page(page.context, profile, log)
          if work_ref is not None:
            work_ref["page"] = page
        seed_google_consent_cookies(page.context, log)
        if page.is_closed():
          page = self._recover_work_page(page.context, profile, log)
          if work_ref is not None:
            work_ref["page"] = page
        self._configure_network_optimizer(page, profile, log, network)
        if work_ref is not None and not work_ref["page"].is_closed():
          page = work_ref["page"]
        page.on("response", handle_response)
        if self._is_mobile_profile(profile):
          self._prepare_mobile_session(page, profile, log)

        if stopped():
          return "stopped"

        self._warm_up_browser_proxy(page, profile, log)

        log("[IP] Checking proxy IP...")
        session_baseline_ip = self._capture_session_ip(page, log, stopped=stopped)
        if session_baseline_ip:
          log(f"[IP] Session baseline IP captured: {session_baseline_ip}")
        else:
          log(
            "[IP] Could not capture session baseline IP "
            "(continuing; keyword-2 IP compare will be skipped)."
          )

        opened_google, page = self._open_url_with_retry(
          page,
          "https://www.google.co.kr",
          stopped=stopped,
          profile=profile,
          log=log,
          max_wait_seconds=45.0,
          purpose="initial-google",
        )
        if work_ref is not None:
          work_ref["page"] = page
        if not opened_google:
          log("[Start] Could not open Google within 45s after IP check.")
          set_status(ProfileStatus.ERROR)
          if self._is_mobile_profile(profile):
            return "error"
          return "tunnel_error"
        self._dismiss_google_consent(page, log)
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
        page = self._warmup(page, profile, stop_event, stopped, set_status, on_ui_status, on_failure, log)
        if work_ref is not None:
          work_ref["page"] = page
        if stopped():
          return "stopped"

        raw_keywords = keywords_override if keywords_override is not None else (self.config.keywords or [])
        keywords = [keyword.strip() for keyword in raw_keywords if keyword and keyword.strip()]
        if not keywords:
          log("No keywords configured")
          set_status(ProfileStatus.ERROR)
          return "error"
        log(f"Keyword batch size for this profile: {len(keywords)}")

        found_any = False
        for index, keyword in enumerate(keywords, start=1):
          if stopped():
            return "stopped"

          self._set_network_phase(PagePhase.GOOGLE_SERP, keyword, page)

          # Rotating residential proxy policy:
          # - Capture IP once at session start (session_baseline_ip).
          # - Re-check only before the 2nd keyword; stop if IP changed mid-session.
          if index == 2 and session_baseline_ip:
            current_ip = self._capture_session_ip(page, log, stopped=stopped, attempts=2)
            if current_ip and current_ip.strip() != session_baseline_ip.strip():
              log(
                f"[IP] Session IP changed before keyword 2 "
                f"({session_baseline_ip} -> {current_ip}). Stopping profile for deletion."
              )
              set_status(ProfileStatus.ERROR)
              return "ip_changed"
            if current_ip:
              log(f"[IP] Session IP unchanged before keyword 2 ({current_ip})")
          keyword_grace_deadline = time.time() + 120.0 if index <= 2 else 0.0
          while True:
            try:
              if work_ref is not None and not work_ref["page"].is_closed():
                page = work_ref["page"]
              page = self._sync_work_page(page, profile, log)
              guard, page = self._guard_captcha(
                page, stop_event, set_status, on_ui_status, profile, log,
                context=f"before-keyword-{index}",
              )
              if guard == "stopped":
                return "stopped"
              if guard in ("blocked", "error"):
                set_status(ProfileStatus.ERROR)
                return guard

              page = self._sync_work_page(page, profile, log)
              on_keyword_serp = self._is_on_serp_for_keyword(page, keyword)
              on_any_serp = self._is_on_google_serp(page)
              if on_keyword_serp:
                set_status(ProfileStatus.SEARCHING)
                log(
                  f"Resuming keyword ({index}/{len(keywords)}): {keyword} "
                  "(SERP already open — skipping re-type)"
                )
                submit_search = False
              elif on_any_serp:
                set_status(ProfileStatus.SEARCHING)
                log(
                  f"Searching keyword ({index}/{len(keywords)}): {keyword} "
                  "(from SERP search box after warm-up)"
                )
                submit_search = True
              else:
                page = self._safe_goto(
                  page,
                  "https://www.google.co.kr",
                  profile,
                  log,
                  wait_until="domcontentloaded",
                  timeout=60000,
                )
                if work_ref is not None:
                  work_ref["page"] = page
                self._dismiss_google_consent(page, log)
                set_status(ProfileStatus.SEARCHING)
                log(f"Searching keyword ({index}/{len(keywords)}): {keyword}")
                submit_search = True
              match = self._search_target(
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
              )
              if stopped():
                return "stopped"

              if not match:
                if (
                  not self._last_search_exhaustion_eligible
                  and keyword_grace_deadline > 0
                  and time.time() < keyword_grace_deadline
                ):
                  remaining = max(0, int(keyword_grace_deadline - time.time()))
                  log(
                    f"[Retry] Keyword '{keyword}' ended before full SERP scan "
                    f"(transient). Retrying for up to {remaining}s."
                  )
                  time.sleep(2.0)
                  continue
                log(f"Target not found for '{keyword}'")
                if on_keyword_exhausted and (
                  self._last_search_exhaustion_eligible or self._mobile_serp_end_reached
                ):
                  on_keyword_exhausted(keyword)
                elif self._last_search_exhausted:
                  log(
                    f"[Search] '{keyword}' ended early before all SERP pages were checked; "
                    "keyword kept in list (not marked exhausted)."
                  )
                break

              page_num, rank, href = match
              self._set_network_phase(PagePhase.TARGET_SITE, keyword, page)
              opened = self._open_target_from_serp_click(
                page,
                href,
                keyword=keyword,
                profile=profile,
                stop_event=stop_event,
                stopped=stopped,
                set_status=set_status,
                on_ui_status=on_ui_status,
                max_wait_seconds=45.0,
              )
              if stopped():
                return "stopped"
              if not opened:
                log(
                  f"[Target] Target site did not open within 45s after SERP click for '{keyword}'. "
                  "Skipping keyword without re-search."
                )
                break

              guard, page = self._ensure_captcha_clear(
                page, stop_event, set_status, on_ui_status, profile, log, context="post-target-open",
              )
              if guard in self._captcha_abort_values():
                set_status(ProfileStatus.ERROR)
                return guard

              self.csv.log(keyword, self.config.target_domain, page_num, rank, profile.name)
              total_rank = ((page_num - 1) * 10) + rank
              self.keyword_history.log(keyword, page_num, rank, total_rank)
              log(f"Found {self.config.target_domain} at page {page_num}, rank {rank} for '{keyword}'")
              found_any = True
              set_status(ProfileStatus.SUCCESS)
              dwell_seconds = random.uniform(self.config.dwell_min, self.config.dwell_max)
              log(f"[Target] Dwelling on site for {dwell_seconds:.0f}s")
              self._dwell_on_site(page, dwell_seconds, stopped, profile)
              self._set_network_phase(PagePhase.GOOGLE_SERP, keyword, page)
              if stopped():
                return "stopped"
              break
            except Exception as exc:
              page = self._sync_work_page(page, profile, log)
              guard, page = self._guard_captcha(
                page, stop_event, set_status, on_ui_status, profile, log,
                context=f"retry-keyword-{index}",
              )
              if guard == "stopped":
                return "stopped"
              if guard in ("blocked", "error"):
                set_status(ProfileStatus.ERROR)
                return guard
              if self._should_retry_connection_until_deadline(exc, keyword_grace_deadline, stopped):
                remaining = max(0, int(keyword_grace_deadline - time.time()))
                log(
                  f"[Retry] Connection unstable before keyword {index} flow ('{keyword}'). "
                  f"Retrying for up to {remaining}s: {exc}"
                )
                time.sleep(2.0)
                continue
              raise

        if not found_any:
          set_status(ProfileStatus.IDLE)
          report_traffic(force=True)
          return "not_found"

        set_status(ProfileStatus.SUCCESS)
        report_traffic(force=True)
        return "success"
      except Exception as exc:
        if self._is_browser_closed_error(exc):
          log(f"Browser closed during session; treating as stopped. ({type(exc).__name__}: {exc})")
          return "stopped"
        log(f"Session error: {exc}")
        self._report_failure(page, profile, "run_session", exc, on_failure)
        set_status(ProfileStatus.ERROR)
        error_kind = classify_network_error(exc)
        if error_kind in ("tunnel", "dns"):
          log(f"[Network] {error_kind} error — profile will retry with proxy rotation policy")
          return "tunnel_error"
        if error_kind == "timeout":
          log("[Network] Timeout error after retries")
        return "error"
      finally:
        report_traffic(force=True)
        self._cleanup_session(browser, log, network, on_session_cleanup)

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
    endpoints = (
      "https://api.ipify.org?format=text",
      "https://checkip.amazonaws.com/",
      "https://ipv4.icanhazip.com/",
    )
    for endpoint in endpoints:
      try:
        response = page.context.request.get(endpoint, timeout=15000)
        if not response.ok:
          continue
        ip = (response.text() or "").strip()
        if ip and self._looks_like_ip(ip):
          return ip
      except Exception:
        continue
    return ""

  def _capture_session_ip(
    self,
    page: Page,
    log: Callable[[str], None],
    *,
    attempts: int = 3,
    stopped: Optional[Callable[[], bool]] = None,
  ) -> str:
    """Single-shot IP capture for rotating residential proxies (no continuous polling)."""
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
  ) -> bool:
    target_host = self._normalize_domain(self.config.target_domain)
    mobile = self._is_mobile_profile(profile)

    guard, page = self._guard_captcha(
      page, stop_event, set_status, on_ui_status, profile, self.logger,
    )
    if guard == "stopped":
      return False
    if guard in ("error", "blocked"):
      return False

    clicked = False
    link = self._find_result_link_for_href(page, href, mobile=mobile)
    if link:
      try:
        raw_href = link.get_attribute("href") or href
        resolved = self._resolve_result_href(raw_href)
        if not self._href_matches_target(resolved or href, self.config.target_domain):
          self.logger(
            f"[Target] Refusing SERP click — host mismatch "
            f"({self._normalize_domain(resolved or href)} vs "
            f"{self._normalize_domain(self.config.target_domain)})"
          )
          link = None
      except Exception:
        pass
    if link:
      try:
        human_click(
          link,
          self.config.action_delay_min,
          self.config.action_delay_max,
          page=page,
          mobile=mobile,
        )
        clicked = True
      except Exception as exc:
        self.logger(
          f"[Target] SERP click failed for '{keyword}'; trying direct URL: {exc}"
        )

    if not clicked:
      try:
        page = self._safe_goto(
          page,
          href,
          profile,
          self.logger,
          wait_until="domcontentloaded",
          timeout=int(max_wait_seconds * 1000),
        )
        host = self._normalize_domain(page.url or "")
        return bool(target_host and (host == target_host or host.endswith(f".{target_host}")))
      except Exception:
        return False

    deadline = time.time() + max(5.0, float(max_wait_seconds))
    self.logger(
      f"[Target] Waiting up to {max_wait_seconds:.0f}s for target site after SERP click"
    )
    while time.time() < deadline:
      if stopped():
        return False
      page = self._sync_work_page(page, profile, self.logger)
      try:
        host = self._normalize_domain(page.url or "")
        if target_host and (host == target_host or host.endswith(f".{target_host}")):
          try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
          except Exception:
            pass
          return True
      except Exception:
        pass
      time.sleep(0.5)

    self.logger(
      f"[Target] Target site did not open within {max_wait_seconds:.0f}s "
      f"after SERP click for '{keyword}'"
    )
    return False

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

  def _warm_up_browser_proxy(
    self,
    page: Page,
    profile: ProfileSpec,
    log: Callable[[str], None],
  ) -> None:
    if page.is_closed():
      return
    try:
      page.goto("about:blank", wait_until="commit", timeout=15000)
    except Exception as exc:
      if self._is_proxy_connection_error(exc):
        log(f"[Start] Proxy warmup navigation retrying: {exc}")
        time.sleep(2.0)
        try:
          page = self._recover_work_page(page.context, profile, log)
          page.goto("about:blank", wait_until="commit", timeout=15000)
        except Exception:
          return
      else:
        return
    page.wait_for_timeout(random.randint(900, 1600))

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
  def _is_blank_tab_url(url: str) -> bool:
    lowered = (url or "").strip().lower()
    return (
      not lowered
      or lowered == "about:blank"
      or lowered.startswith(("chrome://", "devtools://"))
    )

  @staticmethod
  def _pick_work_page(tabs: list[Page], profile: ProfileSpec) -> Page:
    if not tabs:
      raise ValueError("no tabs available")
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
    page.goto(url, **goto_kwargs)
    return page

  def _recover_work_page(
    self,
    context,
    profile: ProfileSpec,
    log: Callable[[str], None],
    preferred: Optional[Page] = None,
  ) -> Page:
    if preferred is not None and not preferred.is_closed():
      return preferred
    alive = [tab for tab in list(context.pages) if not tab.is_closed()]
    if not alive:
      for _ in range(4):
        time.sleep(0.6)
        alive = [tab for tab in list(context.pages) if not tab.is_closed()]
        if alive:
          break
    if not alive:
      if self._is_mobile_profile(profile):
        raise RuntimeError("no open tabs available on mobile profile")
      page = context.new_page()
      log("[Tab] No open tabs — opened new work tab")
      return page
    page = self._pick_work_page(alive, profile)
    log(f"[Tab] Recovered work tab ({len(alive)} open, url={(page.url or '')[:80]})")
    return page

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
    alive = [tab for tab in list(context.pages) if not tab.is_closed()]
    for candidate in alive:
      try:
        if self.captcha.requires_captcha_clear(candidate):
          if candidate != page:
            log(f"[Captcha] Active on browser tab: {(candidate.url or '')[:100]}")
          try:
            candidate.bring_to_front()
          except Exception:
            pass
          return candidate
      except Exception:
        continue
    if page is not None and not page.is_closed():
      return page
    return self._recover_work_page(context, profile, log)

  @staticmethod
  def _open_work_page(browser: Browser, profile: ProfileSpec, log: Callable[[str], None]) -> Page:
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    pages = [tab for tab in list(context.pages) if not tab.is_closed()]
    mobile = SerpBot._is_mobile_profile(profile)
    if pages:
      page = SerpBot._pick_work_page(pages, profile)
      closed_extras = 0
      if mobile:
        for extra in pages:
          if extra == page or extra.is_closed():
            continue
          if SerpBot._is_blank_tab_url(extra.url or ""):
            try:
              extra.close()
              closed_extras += 1
            except Exception:
              pass
        log(
          f"Reusing AdsPower tab ({profile.os_browser_label}: active tab, "
          f"{len(pages)} open, closed {closed_extras} blank)"
        )
      else:
        for extra in pages[1:]:
          try:
            if not extra.is_closed() and extra != page:
              extra.close()
              closed_extras += 1
          except Exception:
            pass
        log(f"Reusing AdsPower tab (closed {closed_extras} extra tab(s))")
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
  def _attach_tab_guard(context, work_page: Page, log: Callable[[str], None]) -> dict:
    work_ref = {"page": work_page}

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
  ) -> None:
    if not self._is_mobile_profile(profile):
      return
    try:
      enable_mobile_touch(page)
      log(f"[Mobile] CDP touch emulation ready ({profile.os_browser_label})")
    except Exception as exc:
      log(f"[Mobile] Session prep warning: {exc}")

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
      if status_key == UiStatusKey.CAPTCHA_MANUAL.value:
        set_status(ProfileStatus.CAPTCHA_MANUAL)
      else:
        set_status(ProfileStatus.CAPTCHA_WAIT)
      if on_ui_status:
        on_ui_status(status_key, display_text)

    def finish_captcha_flow(result: str) -> tuple[str, Page]:
      nonlocal page
      if result == "ok":
        page = self._sync_work_page(page, profile, log)
        log(f"[Captcha] Post-solve page synced ({context}) — locators must be recreated")
      return result, page

    try:
      captcha_present = self.captcha.requires_captcha_clear(page)
    except Exception as exc:
      if self.captcha.is_awaiting_clear():
        log(f"[Captcha] Check failed during {context}; still waiting for solve: {exc}")
        return finish_captcha_flow(
          self.captcha.handle_before_action(page, stop_event, captcha_status)
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
    log(
      f"[Captcha] Detected during {context} "
      f"(automated={'yes' if self.captcha.automated_mode else 'no'}, url={(page.url or '')[:100]})"
    )

    try:
      return finish_captcha_flow(
        self.captcha.handle_before_action(page, stop_event, captcha_status)
      )
    except Exception as exc:
      if self.captcha.is_awaiting_clear() or self._is_connection_error(exc):
        log(f"[Captcha] Solver flow interrupted during {context}; waiting for solve: {exc}")
        try:
          return finish_captcha_flow(
            self.captcha.handle_before_action(page, stop_event, captcha_status)
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
  ) -> Page:
    if not self.config.warmup_queries:
      return page

    count_lo = max(1, int(self.config.warmup_count_min))
    count_hi = max(count_lo, int(self.config.warmup_count_max))
    query_count = min(len(self.config.warmup_queries), random.randint(count_lo, count_hi))
    queries = random.sample(self.config.warmup_queries, k=query_count)
    log(
      f"[Warmup] Running {query_count} warm-up quer{'y' if query_count == 1 else 'ies'} "
      f"(configured {count_lo}-{count_hi}, SERP scroll only — no result clicks)"
    )

    for query in queries:
      if stopped():
        return page
      self._set_network_phase(PagePhase.GOOGLE_SERP, query, page)
      guard, page = self._guard_captcha(
        page, stop_event, set_status, on_ui_status, profile, log, context="warmup",
      )
      if guard in ("stopped", "error", "blocked"):
        return page
      set_status(ProfileStatus.WARMING_UP)
      warmup_min_ms = max(1000, int(self.config.warmup_dwell_min * 1000))
      warmup_max_ms = max(warmup_min_ms, int(self.config.warmup_dwell_max * 1000))
      stay_ms = random.randint(warmup_min_ms, warmup_max_ms)
      mobile = self._is_mobile_profile(profile)
      try:
        page = self._google_search(page, query, profile, on_failure)
        guard, page = self._ensure_captcha_clear(
          page, stop_event, set_status, on_ui_status, profile, log, context="post-warmup-search",
        )
        if guard in self._captcha_abort_values():
          return page
        page = self._wait_for_serp_stable(page, log, timeout_seconds=8.0)
        for _ in range(random.randint(2, 5)):
          scroll_page(
            page,
            random.choice((-1, 1)) * random.randint(120, 420),
            mobile=mobile,
          )
          random_delay(self.config.action_delay_min, self.config.action_delay_max)
      except Exception as exc:
        self.logger(f"[Warmup] Search phase warning: {exc}")
        guard, page = self._ensure_captcha_clear(
          page, stop_event, set_status, on_ui_status, profile, log, context="warmup-search-error",
        )
        if guard in ("stopped", "error", "blocked"):
          return page

      self.logger(
        f"[Warmup] SERP dwell started ({stay_ms // 1000}s, scroll only — staying on results page)"
      )
      self._micro_scroll_during_wait(page, stay_ms, stopped, profile)
      if stopped():
        return page

    page = self._sync_work_page(page, profile, log)
    return page

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
      return self._url_on_serp_or_sorry(page.url or "")
    except Exception:
      return False

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
    deadline = time.time() + max(2.0, float(timeout_seconds))
    while time.time() < deadline:
      try:
        url = page.url or ""
        if self._url_on_serp_or_sorry(url):
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

  def _submit_search_query(
    self,
    page: Page,
    query: str,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
  ) -> Page:
    type_lo, type_hi = self._search_type_delay_bounds()
    random_delay(type_lo * 0.35, type_hi * 0.55)
    page.keyboard.press("Enter")

    enter_deadline = time.time() + 3.0
    while time.time() < enter_deadline:
      if self._url_on_serp_or_sorry(page.url or ""):
        break
      try:
        page.wait_for_load_state("domcontentloaded", timeout=1200)
      except Exception:
        pass
      time.sleep(0.12)
    else:
      self.logger("[Search] Enter did not open SERP within 3s — using direct search URL")
      return self._google_search_via_url(page, query, profile, on_failure)

    return self._wait_for_serp_stable(page, self.logger, timeout_seconds=8.0)

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

  def _google_search(self, page: Page, query: str, profile: ProfileSpec, on_failure: Optional[FailureCallback]) -> Page:
    self._dismiss_google_consent(page, self.logger)
    mobile = self._is_mobile_profile(profile)
    type_lo, type_hi = self._search_type_delay_bounds()

    try:
      if "google." not in (urlparse(page.url).netloc or "").lower():
        page = self._safe_goto(
          page,
          "https://www.google.co.kr",
          profile,
          self.logger,
          wait_until="domcontentloaded",
          timeout=45000,
        )
        self._dismiss_google_consent(page, self.logger)
    except Exception as exc:
      if self._is_connection_error(exc):
        raise
      self.logger(f"[Search] Could not open Google homepage before typing: {exc}")

    search_box = page.locator('textarea[name="q"], input[name="q"]').first
    try:
      search_box.wait_for(state="visible", timeout=5000)
    except Exception:
      try:
        page = self._safe_goto(
          page,
          "https://www.google.co.kr",
          profile,
          self.logger,
          wait_until="domcontentloaded",
          timeout=45000,
        )
        self._dismiss_google_consent(page, self.logger)
        search_box.wait_for(state="visible", timeout=5000)
      except Exception as exc:
        if self._is_connection_error(exc):
          raise
        self.logger(f"[Search] Search box not available, falling back to direct URL: {exc}")
        return self._google_search_via_url(page, query, profile, on_failure)

    self.logger(f"[Search] Typing query: {query}")
    human_type(
      search_box,
      query,
      type_lo,
      type_hi,
      typo_chance=0.03,
      min_length_for_typo=9,
      page=page,
      mobile=mobile,
    )
    page = self._submit_search_query(page, query, profile, on_failure)
    if self.captcha.requires_captcha_clear(page) or self._url_looks_like_captcha(page):
      self.logger(
        f"[Captcha] Post-search captcha signal detected (url={(page.url or '')[:120]})"
      )
    if mobile:
      page.wait_for_timeout(random.randint(350, 700))
      micro_scroll(page, times=1, delay_lo=0.15, delay_hi=0.35, mobile=True)
      page = self._wait_for_serp_stable(page, self.logger, timeout_seconds=10.0)
    return page

  def _google_search_via_url(
    self,
    page: Page,
    query: str,
    profile: ProfileSpec,
    on_failure: Optional[FailureCallback],
  ) -> Page:
    search_url = self._google_search_url(query, profile)
    try:
      page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
      if self._is_mobile_profile(profile):
        page.wait_for_timeout(random.randint(180, 380))
        micro_scroll(page, times=1, delay_lo=0.12, delay_hi=0.28, mobile=True)
    except Exception as exc:
      if self._is_connection_error(exc):
        raise
      self._report_failure(page, profile, "google_search_url", exc, on_failure)
      raise
    return self._wait_for_serp_stable(page, self.logger, timeout_seconds=8.0)

  def _dismiss_google_consent(self, page: Page, log: Callable[[str], None]) -> None:
    try:
      if not is_google_consent_present(page):
        return
      dismiss_google_consent(page, log)
    except Exception as exc:
      log(f"[Consent] Dismiss warning: {exc}")

  def _captcha_abort_values(self) -> tuple[str, ...]:
    return ("stopped", "error", "blocked")

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
      self._dismiss_google_consent(page, log)
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
    normalized = self._normalize_domain(target_href)
    for selector in self._organic_link_selectors(mobile):
      links = page.locator(selector)
      for index in range(min(links.count(), 30)):
        link = links.nth(index)
        raw_href = link.get_attribute("href") or ""
        resolved = self._resolve_result_href(raw_href)
        if not resolved:
          continue
        if resolved == target_href or self._normalize_domain(resolved) == normalized:
          return link
    return None

  @staticmethod
  def _organic_link_selectors(mobile: bool) -> tuple[str, ...]:
    if mobile:
      return (
        "#rso a[data-ved][href]",
        "div#search a[data-ved][href]",
        "#rso a:has(h3)",
        "div#search a:has(h3)",
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
    if "google." in parsed.netloc and parsed.path == "/url":
      query = parse_qs(parsed.query).get("q", [""])[0]
      return unquote(query) or href
    return href

  def _search_target(
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
  ) -> Optional[Tuple[int, int, str]]:
    self._last_search_exhausted = False
    self._last_search_exhaustion_eligible = False
    target = self._normalize_domain(self.config.target_domain)
    self._set_network_phase(PagePhase.GOOGLE_SERP, keyword, page)
    max_pages = max(1, int(self.config.max_search_pages))
    history_page = self.keyword_history.get_last_page_hint(keyword)
    visited_result_pages: set[int] = set()
    serp_last_page: Optional[int] = None
    pages_with_results = 0
    current_page: Optional[int] = None

    guard, page = self._guard_captcha(
      page, stop_event, set_status, on_ui_status, profile, log, context="search-results",
    )
    if guard in ("stopped", "error", "blocked"):
      return None
    set_status(ProfileStatus.SEARCHING)
    mobile = self._is_mobile_profile(profile)
    if submit_search:
      page = self._google_search(page, keyword, profile, on_failure)
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
      return None

    if mobile:
      return self._search_target_mobile(
        page,
        profile,
        keyword,
        target,
        max_pages,
        history_page,
        stop_event,
        stopped,
        set_status,
        on_ui_status,
        on_failure,
        log,
        submit_search=submit_search,
      )

    current_page = self._serp_page_num(page, profile)
    serp_last_page = self._update_serp_last_page(
      page, current_page, None, max_pages, profile, log, history_page=history_page,
    )
    serp_last_page = self._apply_serp_cap_floor(serp_last_page, history_page, max_pages)
    effective_cap = serp_last_page
    search_order = self._build_search_order(max_pages, history_page, serp_last_page)
    order_note = (f" (history page {history_page})" if history_page else " (no history)")
    planned_pages = [page_num for page_num in search_order if page_num <= effective_cap]
    self.logger(
      f"[Search] order for '{keyword}': {planned_pages}"
      + order_note
      + f", SERP cap {serp_last_page or 'unknown'}"
    )

    for page_num in search_order:
      if stopped():
        return None

      effective_cap = min(max_pages, serp_last_page) if serp_last_page else max_pages
      if page_num > effective_cap:
        self.logger(
          f"[Search] Skip page {page_num}; Google results end at page {serp_last_page} "
          f"(configured max {max_pages}) for '{keyword}'"
        )
        continue

      if current_page != page_num:
        if not self._navigate_to_search_page(
          page, keyword, page_num, profile, stop_event, set_status, on_ui_status, on_failure, log,
        ):
          return None
        current_page = self._serp_page_num(page, profile)
        if not mobile and current_page != page_num:
          self.logger(
            f"[Search] Desktop: landed on page {current_page}, expected {page_num} "
            f"— direct URL for '{keyword}'"
          )
          page = self._goto_search_page(page, keyword, page_num, profile, on_failure)
          current_page = self._serp_page_num(page, profile)

      served_page_num = current_page
      if served_page_num in visited_result_pages:
        if page_num > served_page_num:
          self.logger(
            f"[Search] Stuck on page {served_page_num} while requesting page {page_num} "
            f"for '{keyword}' — trying direct URL"
          )
          page = self._goto_search_page(page, keyword, page_num, profile, on_failure)
          current_page = self._serp_page_num(page, profile)
          served_page_num = current_page
        if served_page_num in visited_result_pages:
          if page_num > served_page_num:
            serp_last_page = min(serp_last_page or served_page_num, served_page_num)
            self.logger(
              f"[Search] Google has no page {page_num}; "
              f"results end at page {serp_last_page} for '{keyword}'"
            )
          continue
      visited_result_pages.add(served_page_num)

      self._serp_micro_scroll(page, profile, times=1)

      serp_last_page = self._update_serp_last_page(
        page, served_page_num, serp_last_page, max_pages, profile, log, history_page=history_page,
      )
      serp_last_page = self._apply_serp_cap_floor(serp_last_page, history_page, max_pages)

      result_hrefs = self._collect_organic_result_hrefs_with_retry(
        page,
        served_page_num,
        profile,
        stop_event,
        set_status,
        on_ui_status,
        log,
      )
      if result_hrefs is None:
        return None
      if not result_hrefs:
        if served_page_num == 1:
          self.logger(
            f"[Search] Page 1 returned no parseable results for '{keyword}' "
            "(transient/block — keyword kept in list)."
          )
          return None
        self.logger(f"[Search] No organic results on page {served_page_num}; stopping for '{keyword}'")
        serp_last_page = min(serp_last_page or max(served_page_num - 1, 1), max(served_page_num - 1, 1))
        break

      pages_with_results += 1
      self.logger(
        f"[Search] Page {served_page_num}: parsed {len(result_hrefs)} organic link(s) for '{keyword}'"
      )
      for rank, href in enumerate(result_hrefs, start=1):
        if self._href_matches_target(href, target):
          return served_page_num, rank, href

      requested_beyond_last = served_page_num < page_num
      if requested_beyond_last:
        serp_last_page = min(serp_last_page or served_page_num, served_page_num)
        self.logger(
          f"[Search] Requested page {page_num} but Google kept page {served_page_num}; "
          f"results end at page {serp_last_page} for '{keyword}'"
        )
        continue

      if not mobile and not self._has_next_serp_page(page, profile, log=log):
        detected_end = self._detect_serp_last_page(page, served_page_num, log=log)
        if detected_end and detected_end <= served_page_num:
          serp_last_page = min(serp_last_page or served_page_num, served_page_num)
          self.logger(
            f"[Search] No further Google pages after page {serp_last_page} for '{keyword}'"
          )
        elif not detected_end:
          self.logger(
            f"[Search] Next-page control not visible on page {served_page_num} "
            f"for '{keyword}' (keeping SERP cap {serp_last_page})"
          )

    effective_cap = min(max_pages, serp_last_page) if serp_last_page else max_pages
    planned_pages = [page_num for page_num in search_order if page_num <= effective_cap]
    self._last_search_exhaustion_eligible = (
      pages_with_results > 0
      and bool(planned_pages)
      and all(page_num in visited_result_pages for page_num in planned_pages)
    )
    self._last_search_exhausted = self._last_search_exhaustion_eligible

    if serp_last_page and serp_last_page < max_pages:
      self.logger(
        f"[Search] Keyword '{keyword}' SERP depth capped at page {serp_last_page} "
        f"(configured max {max_pages})"
      )
    if self._last_search_exhaustion_eligible:
      self.logger(
        f"[Search] All planned pages {planned_pages} checked with results for '{keyword}' "
        "but target was not found."
      )
    return None

  def _search_target_mobile(
    self,
    page: Page,
    profile: ProfileSpec,
    keyword: str,
    target: str,
    max_pages: int,
    history_page: Optional[int],
    stop_event: Optional[threading.Event],
    stopped: Callable[[], bool],
    set_status: StatusCallback,
    on_ui_status: Optional[UiStatusCallback],
    on_failure: Optional[FailureCallback],
    log: Callable[[str], None],
    *,
    submit_search: bool,
  ) -> Optional[Tuple[int, int, str]]:
    """Mobile SERP: scan accumulated results, tap 더보기 once per virtual page (no URL jumps)."""
    self._mobile_serp_end_reached = False
    self._mobile_link_offset = 0
    effective_cap = self._apply_serp_cap_floor(max_pages, history_page, max_pages)
    planned_pages = list(range(1, effective_cap + 1))
    start_page = self._serp_page_num(page, profile)
    if not submit_search and start_page > 1:
      planned_pages = [page_num for page_num in planned_pages if page_num >= start_page]
      self.logger(
        f"[Search] Mobile resume at virtual page {start_page} for '{keyword}' "
        f"(scan order {planned_pages})"
      )
    else:
      self.logger(
        f"[Search] Mobile scan for '{keyword}': pages 1→{effective_cap} via 더보기 taps"
      )

    pages_with_results = 0
    page = self._wait_for_serp_stable(page, log, timeout_seconds=10.0)

    for page_num in planned_pages:
      if stopped():
        return None

      page = self._wait_for_serp_stable(page, log, timeout_seconds=8.0)
      self._scroll_to_serp_pagination(page, mobile=True, fast=page_num > 1)
      self._serp_micro_scroll(page, profile, times=1)

      result_hrefs = self._collect_organic_result_hrefs_with_retry(
        page,
        page_num,
        profile,
        stop_event,
        set_status,
        on_ui_status,
        log,
      )
      if result_hrefs is None:
        return None

      if not result_hrefs:
        for attempt in range(1, 5):
          self.logger(
            f"[Search] Mobile page {page_num}: no links yet for '{keyword}' "
            f"(retry {attempt}/4)"
          )
          page.wait_for_timeout(random.randint(500, 1100))
          self._scroll_to_serp_pagination(page, mobile=True, fast=False)
          self._serp_micro_scroll(page, profile, times=1)
          result_hrefs = self._collect_organic_result_hrefs(page, profile)
          if result_hrefs:
            break

      if not result_hrefs:
        next_start = self._next_serp_start_offset(page, profile)
        more_available = self._mobile_serp_more_button_available(page, next_start)
        if page_num == 1:
          if not more_available:
            self.logger(
              f"[Search] Mobile: no organic results and no '검색결과 더보기' for '{keyword}' "
              "— moving to next keyword"
            )
            self._mobile_serp_end_reached = True
            self._last_search_exhaustion_eligible = True
            break
          self.logger(
            f"[Search] Mobile page 1 still empty for '{keyword}' after retries "
            "(continuing — may load after 더보기)"
          )
        else:
          self.logger(
            f"[Search] Mobile page {page_num}: no organic links for '{keyword}'; stopping scan"
          )
          self._mobile_serp_end_reached = True
          break

      if result_hrefs:
        pages_with_results += 1
        self.logger(
          f"[Search] Mobile page {page_num}: parsed {len(result_hrefs)} organic link(s) "
          f"for '{keyword}'"
        )
        page_hrefs = result_hrefs[self._mobile_link_offset:]
        for rank, href in enumerate(page_hrefs, start=1):
          if self._href_matches_target(href, target):
            return page_num, rank, href
        self._mobile_link_offset = len(result_hrefs)

      if page_num >= effective_cap:
        break

      if not self._tap_mobile_more_once(page, profile, keyword, page_num):
        next_start = self._next_serp_start_offset(page, profile)
        more_available = self._mobile_serp_more_button_available(page, next_start)
        if more_available:
          self.logger(
            f"[Search] Mobile: 더보기 present but tap failed after page {page_num} for '{keyword}'"
          )
        else:
          self.logger(
            f"[Search] Mobile: no '검색결과 더보기' after page {page_num} for '{keyword}' "
            "— SERP ended, moving to next keyword"
          )
        self._mobile_serp_end_reached = True
        break

    self._last_search_exhaustion_eligible = (
      pages_with_results > 0 and bool(planned_pages)
    ) or self._mobile_serp_end_reached
    self._last_search_exhausted = self._last_search_exhaustion_eligible
    if self._last_search_exhaustion_eligible:
      self.logger(
        f"[Search] Mobile scanned {pages_with_results} page(s) for '{keyword}' "
        "but target was not found."
      )
    return None

  def _tap_mobile_more_once(
    self,
    page: Page,
    profile: ProfileSpec,
    keyword: str,
    current_page_num: int,
  ) -> bool:
    self._scroll_to_serp_pagination(page, mobile=True, fast=False)
    self.logger(
      f"[Search] Mobile: tapping 더보기 page {current_page_num} → {current_page_num + 1} "
      f"('{keyword}')"
    )
    before_state = self._snapshot_mobile_pagination_state(page)
    for attempt in range(1, 4):
      if not self._is_google_serp_url(page.url):
        self._recover_google_serp_after_mistap(page, before_state.get("url") or "")
      try:
        if self._click_mobile_more_button(page, profile, before_state):
          try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
          except Exception:
            pass
          page.wait_for_timeout(random.randint(450, 950))
          after_state = self._snapshot_mobile_pagination_state(page)
          next_start = self._next_serp_start_offset(page, profile) - 10
          if self._pagination_advanced(before_state, after_state, next_start):
            url_page = self._current_search_results_page_num(page)
            if url_page > self._mobile_serp_page:
              self._mobile_serp_page = url_page
            elif after_state.get("ip_index", 0) > before_state.get("ip_index", 0):
              self._mobile_serp_page = max(self._mobile_serp_page, after_state.get("ip_index", 0) + 1)
            else:
              self._advance_mobile_serp_page()
            self._serp_pause()
            return True
          self.logger(
            f"[Search] Mobile: tap attempt {attempt}/3 did not advance pagination "
            f"(links {before_state.get('organic_count')}→{after_state.get('organic_count')}, "
            f"ip {before_state.get('ip_index')}→{after_state.get('ip_index')})"
          )
          if not self._is_google_serp_url(after_state.get("url") or ""):
            self._recover_google_serp_after_mistap(page, before_state.get("url") or "")
      except Exception as exc:
        if self._is_connection_error(exc):
          self.logger(
            f"[Search] Mobile 더보기 tap retry {attempt}/3 after navigation: {exc}"
          )
          page.wait_for_timeout(random.randint(700, 1400))
          try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
          except Exception:
            pass
          continue
        raise
      scroll_page(page, random.randint(350, 700), mobile=True)
      page.wait_for_timeout(random.randint(200, 450))
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
            f"[Search] Desktop: opening page 1 via direct URL for '{keyword}' "
            f"(was on page {current_page_num})"
          )
          page = self._goto_search_page(page, keyword, 1, profile, on_failure)
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
          f"[Search] Desktop pagination click to page {page_num} failed; "
          f"falling back to direct URL for '{keyword}'"
        )
        page = self._goto_search_page(page, keyword, page_num, profile, on_failure)

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

    if served_page_num == 1:
      for attempt in range(1, 4):
        page.wait_for_timeout(random.randint(500, 1000))
        if self._is_mobile_profile(profile):
          self._scroll_to_serp_pagination(page, mobile=True, fast=False)
        self._serp_micro_scroll(page, profile, times=1)
        hrefs = self._collect_organic_result_hrefs(page, profile)
        if hrefs:
          return hrefs
      page.wait_for_timeout(random.randint(400, 900))
      if self._is_mobile_profile(profile):
        self._scroll_to_serp_pagination(page, mobile=True, fast=True)
      self._serp_micro_scroll(page, profile, times=1)
      hrefs = self._collect_organic_result_hrefs(page, profile)
      if hrefs:
        return hrefs
      if self._is_google_block_page(page):
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
            'a#pnnext',
            '#pnnext',
            'a[rel="next"]',
            '#foot',
            '#navd',
            '#botstuff',
            'nav[role="navigation"]',
            'a[href*="start="]',
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

  def _click_desktop_serp_page(self, page: Page, page_num: int) -> bool:
    current_page_num = self._current_search_results_page_num(page)
    if page_num <= 1 or current_page_num == page_num:
      return True

    self._scroll_to_serp_pagination(page, mobile=False, fast=True)
    start = (page_num - 1) * 10
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
        self.logger(f"[Search] Desktop: clicking pagination link for page {page_num}")
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
        if self._current_search_results_page_num(page) == page_num:
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
        if text != str(page_num):
          continue
        self.logger(f"[Search] Desktop: clicking page number '{page_num}' in footer")
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
        return self._current_search_results_page_num(page) == page_num
      except Exception:
        continue
    return False

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
      if not self._click_mobile_more_button(page, profile):
        scroll_page(page, random.randint(350, 650), mobile=True)
        page.wait_for_timeout(random.randint(200, 450))
        if not self._click_mobile_more_button(page, profile):
          self.logger(
            f"[Search] Mobile: 더보기 button not found after scroll (at page {current_page_num})"
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
    "비지니스", "business", "지도", "maps", "리뷰", "review", "장소", "place",
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
    visible_start = self._visible_mobile_more_href_start(page)
    if self._is_mobile_profile(profile) and url_start == 0 and visible_start is not None:
      return visible_start
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
          let best = null;
          let bestBottom = -1;
          const selectors = ['a[jsname="oHxHid"]', 'a[aria-label="검색결과 더보기"]'];
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((el) => {
              if (!isTapVisible(el)) return;
              const rect = el.getBoundingClientRect();
              if (rect.bottom <= bestBottom) return;
              bestBottom = rect.bottom;
              best = parseStart(el.getAttribute('href') || '');
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

  def _find_mobile_serp_pagination_target(
    self,
    page: Page,
    next_start: int,
  ) -> Optional[dict]:
    """Locate mobile SERP next-page control by URL start= / #pnnext / rel=next — not text alone."""
    try:
      target = page.evaluate(
        """(nextStart) => {
          const blockedHref = /tbm=|\\/maps|\\/url\\?|\\/sorry\\//i;
          const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 8 && rect.height > 8;
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
          const isNextSearchLink = (el) => {
            const href = el.getAttribute('href') || '';
            if (!href || href.startsWith('#') || blockedHref.test(href)) return false;
            if (
              !href.includes('/search')
              && !href.includes('google.')
              && !href.startsWith('?')
              && !href.includes('start=')
            ) return false;
            return parseStart(href) === nextStart;
          };
          const candidates = [];
          const push = (el, score, method, href) => {
            if (!isVisible(el)) return;
            const rect = el.getBoundingClientRect();
            candidates.push({
              score: score + rect.bottom,
              x: rect.left + rect.width / 2,
              y: rect.top + rect.height / 2,
              method,
              href: (href || el.getAttribute('href') || '').slice(0, 160),
            });
          };

          for (const selector of ['a#pnnext', '#pnnext a', 'span#pnnext a', 'a[rel="next"]']) {
            const el = document.querySelector(selector);
            if (!el) continue;
            const href = el.getAttribute('href') || '';
            if (href && parseStart(href) !== null && parseStart(href) !== nextStart) continue;
            push(el, 3000, selector, href);
          }

          document.querySelectorAll('a[href]').forEach((el) => {
            if (!isNextSearchLink(el)) return;
            let score = 1800;
            if (el.id === 'pnnext' || el.closest('#pnnext')) score += 500;
            if (el.getAttribute('rel') === 'next') score += 400;
            if (el.closest('#foot, #navd, #botstuff, nav[role="navigation"], [role="navigation"]')) {
              score += 600;
            }
            push(el, score, 'start_param', el.getAttribute('href') || '');
          });

          if (!candidates.length) return null;
          candidates.sort((a, b) => b.score - a.score);
          return candidates[0];
        }""",
        next_start,
      )
      if target and target.get("x") is not None:
        return target
    except Exception:
      pass
    return None

  def _click_mobile_pagination_locator(
    self,
    page: Page,
    next_start: int,
    delay_lo: float,
    delay_hi: float,
  ) -> bool:
    selectors = (
      f'a#pnnext[href*="start={next_start}"]',
      "a#pnnext",
      f'a[rel="next"][href*="start={next_start}"]',
      'a[rel="next"]',
      f'#foot a[href*="start={next_start}"]',
      f'#botstuff a[href*="start={next_start}"]',
      f'nav[role="navigation"] a[href*="start={next_start}"]',
      f'a[href*="start={next_start}"]',
    )
    best_link = None
    best_bottom = -1.0
    best_href = ""
    for selector in selectors:
      try:
        links = page.locator(selector)
        for index in range(min(links.count(), 20)):
          link = links.nth(index)
          if not link.is_visible():
            continue
          href = link.get_attribute("href") or ""
          parsed = self._parse_href_start(href)
          if selector.endswith(f'start={next_start}"]') or parsed is not None:
            if parsed is not None and parsed != next_start:
              continue
            if parsed is None and not self._href_has_serp_start(href, next_start):
              if selector not in ("a#pnnext", 'a[rel="next"]'):
                continue
          box = link.bounding_box()
          if not box:
            continue
          bottom = box["y"] + box["height"]
          if bottom > best_bottom:
            best_bottom = bottom
            best_link = link
            best_href = href
      except Exception:
        continue
    if best_link is None:
      return False
    self.logger(
      f"[Search] Mobile: pagination link start={next_start} "
      f"({best_href[:100] if best_href else 'footer control'})"
    )
    best_link.scroll_into_view_if_needed(timeout=5000)
    human_click(best_link, delay_lo, delay_hi, page=page, mobile=True)
    return True

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
    try:
      count = page.evaluate(
        """() => {
          const seen = new Set();
          const selectors = ['#rso a[data-ved][href]', 'div#search a[data-ved][href]'];
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((anchor) => {
              const href = anchor.getAttribute('href') || '';
              if (!href || href.startsWith('#')) return;
              try {
                const host = new URL(href, window.location.href).hostname.toLowerCase();
                if (host.includes('google.')) return;
              } catch (e) {
                return;
              }
              seen.add(href);
            });
          }
          return seen.size;
        }"""
      )
      return int(count or 0)
    except Exception:
      return 0

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

  def _recover_google_serp_after_mistap(
    self,
    page: Page,
    before_url: str = "",
  ) -> bool:
    if self._is_google_serp_url(page.url):
      return True
    wrong = page.url or ""
    self.logger(
      f"[Search] Mobile: left Google SERP after mistap ({wrong[:120]}) — going back"
    )
    try:
      page.go_back(wait_until="domcontentloaded", timeout=15000)
      page.wait_for_timeout(random.randint(350, 750))
    except Exception as exc:
      self.logger(f"[Search] Mobile: go_back after mistap failed — {exc}")
    if self._is_google_serp_url(page.url):
      return True
    if before_url and self._is_google_serp_url(before_url):
      try:
        page.goto(before_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(random.randint(300, 600))
      except Exception as exc:
        self.logger(f"[Search] Mobile: SERP URL restore failed — {exc}")
    return self._is_google_serp_url(page.url)

  def _snapshot_mobile_pagination_state(self, page: Page) -> dict:
    doc_height = 0
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
      "organic_count": self._count_mobile_organic_links(page),
      "scroll_y": page.evaluate("() => window.scrollY || 0"),
      "doc_height": doc_height,
    }

  def _pagination_advanced(
    self,
    before: dict,
    after: dict,
    next_start: int,
  ) -> bool:
    before_url = before.get("url") or ""
    after_url = after.get("url") or ""
    if not self._is_google_serp_url(after_url):
      return False
    if self._is_google_serp_url(before_url) and not self._is_google_serp_url(after_url):
      return False
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
    if after.get("start", 0) >= next_start and after.get("start", 0) > before.get("start", 0):
      return True
    if after_url != before_url and after.get("start", 0) > before.get("start", 0):
      return True
    if after.get("organic_count", 0) >= before.get("organic_count", 0) + 5:
      return True
    if after.get("organic_count", 0) > before.get("organic_count", 0):
      return True
    if (
      after.get("doc_height", 0) >= before.get("doc_height", 0) + 250
      and after.get("organic_count", 0) >= before.get("organic_count", 0)
    ):
      return True
    if (
      after.get("organic_count", 0) > before.get("organic_count", 0)
      and after.get("scroll_y", 0) >= before.get("scroll_y", 0)
    ):
      return True
    return False

  def _mobile_serp_more_button_available(self, page: Page, next_start: int) -> bool:
    try:
      found = page.evaluate(
        """(nextStart) => {
          const selectors = [
            'a[jsname="oHxHid"]',
            'a[aria-label="검색결과 더보기"]',
            'a[href*="start="][aria-label="검색결과 더보기"]',
          ];
          for (const selector of selectors) {
            const el = document.querySelector(selector);
            if (!el) continue;
            if (el.getAttribute('aria-hidden') === 'true') continue;
            const rect = el.getBoundingClientRect();
            if (rect.width > 4 && rect.height > 4) return true;
            if (el.getAttribute('aria-label') === '검색결과 더보기') return true;
          }
          const nodes = document.querySelectorAll('a[href], [role="button"]');
          for (const el of nodes) {
            const aria = (el.getAttribute('aria-label') || '').trim();
            const text = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
            if (aria === '검색결과 더보기' || text === '검색결과더보기') return true;
            const href = el.getAttribute('href') || '';
            if (href.includes('start=' + nextStart) && aria.includes('더보기')) return true;
          }
          return false;
        }""",
        next_start,
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
  ) -> bool:
    """Tap known Google mobile control: a[jsname=oHxHid] / aria-label=검색결과 더보기."""
    try:
      clicked = page.evaluate(
        """() => {
          const isTapVisible = (el) => {
            if (!el) return false;
            if (el.getAttribute('aria-hidden') === 'true') return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 6 && rect.height > 6;
          };
          const selectors = [
            'a[jsname="oHxHid"]',
            'a[aria-label="검색결과 더보기"]',
          ];
          let best = null;
          let bestBottom = -1;
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((el) => {
              if (!isTapVisible(el)) return;
              const rect = el.getBoundingClientRect();
              if (rect.bottom <= bestBottom) return;
              bestBottom = rect.bottom;
              best = el;
            });
          }
          if (!best) return '';
          const href = best.getAttribute('href') || '';
          if (href && !href.includes('/search') && !href.startsWith('?') && !href.startsWith('#')) {
            return '';
          }
          best.scrollIntoView({ block: 'center', behavior: 'instant' });
          best.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
          best.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
          best.click();
          return best.getAttribute('jsname') || best.getAttribute('aria-label') || 'visible-more';
        }"""
      )
      if not clicked:
        return False
      self.logger(f"[Search] Mobile: JS tap via {clicked}")
      random_delay(delay_lo, delay_hi)
      page.wait_for_timeout(random.randint(500, 1100))
      after_state = self._snapshot_mobile_pagination_state(page)
      if self._pagination_advanced(before_state, after_state, next_start):
        return True
      if not self._is_google_serp_url(after_state.get("url") or ""):
        self._recover_google_serp_after_mistap(page, before_state.get("url") or "")
      return False
    except Exception as exc:
      self.logger(f"[Search] Mobile: JS tap failed — {exc}")
      return False

  def _save_mobile_more_capture(self, payload: dict) -> None:
    try:
      with open("data/mobile_more_manual_capture.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
      pass

  def _dump_mobile_pagination_full_scan(self, page: Page, next_start: int) -> list[dict]:
    """Broad DOM dump for manual-tap diagnosis — every footer-ish '더보기' control."""
    try:
      items = page.evaluate(
        """(nextStart) => {
          const blocked = /비지니스|business|지도|maps|리뷰|review|장소|place/i;
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
  ) -> bool:
    try:
      if locator.count() == 0:
        return False
      best = None
      best_bottom = -1.0
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
        if item_href and not self._is_mobile_more_button_href(item_href):
          continue
        box = item.bounding_box()
        if not box:
          continue
        bottom = box["y"] + box["height"]
        if bottom > best_bottom:
          best_bottom = bottom
          best = item
      if best is None:
        return False

      href = best.get_attribute("href") or ""
      jsname = best.get_attribute("jsname") or ""
      aria = best.get_attribute("aria-label") or ""
      if href and not self._is_mobile_more_button_href(href):
        self.logger(
          f"[Search] Mobile: skipping non-pagination control "
          f"href='{href[:80]}' aria='{aria[:40]}'"
        )
        return False
      if aria and aria != "검색결과 더보기" and "더보기" not in aria:
        return False
      self.logger(
        f"[Search] Mobile: auto tap via {method} "
        f"jsname={jsname or '-'} aria='{aria[:40]}' "
        f"href='{href[:80]}'"
      )
      best.scroll_into_view_if_needed(timeout=6000)
      page.wait_for_timeout(random.randint(150, 350))
      human_click(best, delay_lo, delay_hi, page=page, mobile=True)
      page.wait_for_timeout(random.randint(500, 1100))
      try:
        page.wait_for_load_state("domcontentloaded", timeout=12000)
      except Exception:
        pass
      after_state = self._snapshot_mobile_pagination_state(page)
      if self._pagination_advanced(before_state, after_state, next_start):
        self.logger(
          f"[Search] Mobile: tap verified "
          f"(links {before_state.get('organic_count')}→{after_state.get('organic_count')}, "
          f"start {before_state.get('start')}→{after_state.get('start')})"
        )
        return True
      if not self._is_google_serp_url(after_state.get("url") or ""):
        self._recover_google_serp_after_mistap(page, before_state.get("url") or "")
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
      probe = page.evaluate(
        """(nextStart) => {
          const blockedText = /비지니스|business|지도|maps|리뷰|review|장소|place|전화|영업/i;
          const primaryLabels = ['검색결과 더보기', '검색결과 더 보기', 'more search results'];
          const normalize = (value) => (value || '').replace(/\\s+/g, '').trim().toLowerCase();
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
          const candidates = [];
          const push = (el, score, method, href) => {
            if (!el) return;
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
            'a#pnnext',
            '#pnnext a',
            'span#pnnext a',
            'a[rel="next"]',
          ]) {
            document.querySelectorAll(selector).forEach((el) => {
              if (!el || el.getAttribute('aria-hidden') === 'true') return;
              if (!isVisible(el)) return;
              const href = el.getAttribute('href') || '';
              if (href && parseStart(href) !== null && parseStart(href) !== nextStart) return;
              push(el, 3200, selector, href);
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
            if (!clickable || !isVisible(clickable)) continue;
            const rect = clickable.getBoundingClientRect();
            const href = clickable.getAttribute('href') || '';
            const hrefStart = parseStart(href);
            let score = rect.bottom;
            if (hrefStart === nextStart) score += 2000;
            else if (hrefStart !== null && hrefStart > 0) score += 500;
            if (clickable.id === 'pnnext' || clickable.closest('#pnnext')) score += 800;
            if (clickable.closest('#foot, #navd, #botstuff, nav[role="navigation"]')) score += 600;
            candidates.push({
              score,
              text: text || aria,
              tag: clickable.tagName,
              id: clickable.id || '',
              className: (clickable.className || '').toString().slice(0, 180),
              role: clickable.getAttribute('role') || '',
              href: href.slice(0, 220),
              hrefStart,
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
            count: candidates.length,
            best: candidates[0] || null,
            candidates: candidates.slice(0, 5),
          };
        }""",
        next_start,
      )
    except Exception as exc:
      self.logger(f"[Search] Mobile probe: DOM scan failed — {exc}")
      return None

    if not probe:
      return None

    best = probe.get("best")
    self.logger(
      f"[Search] Mobile probe: '검색결과 더보기' candidates={probe.get('count', 0)} "
      f"next_start={next_start}"
    )
    for index, item in enumerate(probe.get("candidates") or [], start=1):
      self.logger(
        f"[Search] Mobile probe #{index}: <{item.get('tag')}> "
        f"id={item.get('id') or '-'} role={item.get('role') or '-'} "
        f"jsname={item.get('jsname') or '-'} hrefStart={item.get('hrefStart')} "
        f"text='{(item.get('text') or '')[:40]}'"
      )
      self.logger(
        f"[Search] Mobile probe #{index} html: {(item.get('outerHTML') or '')[:500]}"
      )

    try:
      with open("data/mobile_more_button_probe.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(probe, ensure_ascii=False) + "\n")
    except Exception:
      pass

    return best

  def _tap_mobile_search_results_more_button(
    self,
    page: Page,
    next_start: int,
    delay_lo: float,
    delay_hi: float,
    probe: Optional[dict] = None,
    before_state: Optional[dict] = None,
  ) -> bool:
    """Step 2: tap '검색결과 더보기' via jsname/aria-label locators (from probe log)."""
    if before_state is None:
      before_state = self._snapshot_mobile_pagination_state(page)

    if self._tap_google_mobile_more_js(page, before_state, next_start, delay_lo, delay_hi):
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

    # Known Google mobile pagination control (from session.log capture).
    locator_strategies.extend([
      ('aria-label=검색결과 더보기', page.locator('a[aria-label="검색결과 더보기"]')),
      ('jsname=oHxHid', page.locator('a[jsname="oHxHid"]')),
      (
        f'href-start={next_start}+aria',
        page.locator(f'a[href*="start={next_start}"][aria-label="검색결과 더보기"]'),
      ),
      ('role=button:검색결과 더보기', page.get_by_role("button", name="검색결과 더보기", exact=True)),
      ('role=link:검색결과 더보기', page.get_by_role("link", name="검색결과 더보기", exact=True)),
      ('text=검색결과 더보기', page.get_by_text("검색결과 더보기", exact=True)),
      (
        f'href-start={next_start}',
        page.locator(f'a[href*="start={next_start}"][role="button"]'),
      ),
    ])

    seen_methods: set[str] = set()
    for method, locator in locator_strategies:
      if method in seen_methods:
        continue
      seen_methods.add(method)
      if self._try_tap_mobile_more_locator(
        page, method, locator, delay_lo, delay_hi, before_state, next_start
      ):
        return True

    return False

  def _click_mobile_more_button(
    self,
    page: Page,
    profile: ProfileSpec,
    before_state: Optional[dict] = None,
  ) -> bool:
    """Tap mobile SERP next-page: probe → auto tap (verified) → manual capture fallback."""
    delay_lo, delay_hi = self._serp_delay_bounds()
    next_start = self._next_serp_start_offset(page, profile)
    self._scroll_to_serp_pagination(page, mobile=True, fast=False)
    if before_state is None:
      before_state = self._snapshot_mobile_pagination_state(page)

    probe_best = self._probe_mobile_search_results_more_button(page, next_start)
    if not probe_best:
      scroll_page(page, random.randint(500, 900), mobile=True)
      page.wait_for_timeout(random.randint(350, 700))
      probe_best = self._probe_mobile_search_results_more_button(page, next_start)

    if self._tap_mobile_search_results_more_button(
      page, next_start, delay_lo, delay_hi, probe_best, before_state
    ):
      return True

    if self._click_mobile_pagination_locator(page, next_start, delay_lo, delay_hi):
      page.wait_for_timeout(random.randint(500, 1100))
      if self._pagination_advanced(before_state, self._snapshot_mobile_pagination_state(page), next_start):
        return True

    target = self._find_mobile_serp_pagination_target(page, next_start)
    if target:
      self.logger(
        f"[Search] Mobile: pagination via {target.get('method', 'dom')} "
        f"start={next_start} ({(target.get('href') or '')[:100]})"
      )
      try:
        random_delay(delay_lo, delay_hi)
        dispatch_touch_tap(page, float(target["x"]), float(target["y"]))
        page.wait_for_timeout(random.randint(500, 1100))
        after_state = self._snapshot_mobile_pagination_state(page)
        if self._pagination_advanced(before_state, after_state, next_start):
          return True
        if not self._is_google_serp_url(after_state.get("url") or ""):
          self._recover_google_serp_after_mistap(page, before_state.get("url") or "")
      except Exception:
        pass

    exact_labels = ("검색결과 더보기", "검색결과 더 보기", "더보기", "다음", "Next", "More search results")
    for label in exact_labels:
      for role in ("button", "link"):
        locator = page.get_by_role(role, name=label, exact=True)
        if self._try_tap_mobile_more_locator(
          page, f"role={role}:{label}", locator, delay_lo, delay_hi, before_state, next_start
        ):
          return True

    self.logger(f"[Search] Mobile: auto pagination failed for start={next_start} — entering manual capture")
    if self._visible_mobile_more_href_start(page) is None:
      self.logger(
        "[Search] Mobile: no visible '검색결과 더보기' button (only hidden/stale controls) "
        "— treating as SERP end"
      )
      return False
    if (
      before_state.get("organic_count", 0) == 0
      and not self._mobile_serp_more_button_available(page, next_start)
    ):
      self.logger(
        "[Search] Mobile: no results and no '검색결과 더보기' in DOM — skipping manual wait"
      )
      return False
    return self._wait_for_manual_mobile_more_capture(
      page, profile, next_start, probe_best, before_state
    )

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
    if not normalized or not target_norm:
      return False
    return normalized == target_norm or normalized.endswith(f".{target_norm}")

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
              '검색결과더보기', '검색결과 더보기', '더보기', '다음',
              'next', 'more search results', 'more results',
            ];
            const blocked = [
              '비지니스', 'business', '지도', 'maps', '리뷰', 'review', '장소', 'place',
              '전화', '영업', 'store', 'local', '이전', 'previous',
            ];
            const normalize = (value) => (value || '').replace(/\\s+/g, '').trim().toLowerCase()
              .replace(/…/g, '').replace(/\\.\\.\\./g, '').trim();
            const isFooterMore = (text) => {
              const t = normalize(text);
              if (!t || blocked.some((token) => t.includes(token))) return false;
              return nextLabels.some((label) => t === normalize(label));
            };
            const vh = window.innerHeight || 800;
            const minTop = vh * 0.52;
            const clickables = document.querySelectorAll(
              'a, button, span, div[role="button"], span[role="link"]'
            );
            for (const el of clickables) {
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
      cap = min(
        configured_max,
        max(served_page_num, current_cap or configured_max),
      )
      cap = self._apply_serp_cap_floor(cap, history_page, configured_max)
      if current_cap != cap:
        self.logger(f"[Search] Mobile SERP depth: page {served_page_num}, cap {cap}")
      return cap

    self._scroll_to_serp_pagination(page, mobile=mobile, fast=True)
    detected = self._detect_serp_last_page(page, served_page_num, log=log)
    has_next = self._has_next_serp_page(page, profile, log=log)

    if detected and detected > 0:
      cap = min(detected, configured_max)
    elif has_next:
      cap = configured_max
    else:
      floor = 1
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
      self.logger(f"[Search] SERP last page estimate updated: {cap}")
    return cap

  def _reset_mobile_serp_page(self) -> None:
    self._mobile_serp_page = 1

  def _sync_mobile_serp_page(self, page: Page, profile: ProfileSpec) -> None:
    url_page = self._current_search_results_page_num(page)
    if url_page > 1:
      self._mobile_serp_page = url_page
      return
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
  def _build_search_order(
    max_pages: int,
    history_page: Optional[int],
    serp_cap: Optional[int] = None,
  ) -> list[int]:
    configured_cap = max(1, int(max_pages))
    effective_cap = configured_cap
    if serp_cap and serp_cap > 0:
      effective_cap = min(configured_cap, int(serp_cap))

    if effective_cap <= 1:
      return [1]

    # Always 1→N: reliable desktop pagination, no backward jumps.
    return list(range(1, effective_cap + 1))

  def _dwell_on_site(
    self,
    page: Page,
    total_seconds: float,
    stopped: Callable[[], bool],
    profile: ProfileSpec,
  ) -> None:
    mobile = self._is_mobile_profile(profile)
    elapsed = 0.0
    target_internal_clicks = random.randint(1, 2)
    internal_clicks = 0
    next_internal_at = random.uniform(
      total_seconds * 0.2,
      max(total_seconds * 0.45, 8.0),
    )
    hard_deadline = time.time() + max(5.0, float(total_seconds) + 25.0)
    self.logger(
      f"[Target] Dwell plan: scroll-heavy"
      f"{', mobile touch scroll' if mobile else ''}"
      f", internal links {target_internal_clicks}x"
    )
    for _ in range(2 if mobile else 1):
      self._human_scroll_cycle(page, stopped, mobile=mobile, intensity="high")
      if stopped():
        return

    while elapsed < total_seconds and not stopped():
      if time.time() >= hard_deadline:
        self.logger(
          f"[Target] Dwell hard-stop reached ({int(total_seconds)}s budget). "
          "Proceeding to close profile flow."
        )
        break
      remaining = total_seconds - elapsed
      cycle_seconds = min(
        remaining,
        random.uniform(2.0, 5.0) if mobile else random.uniform(5.0, 10.0),
      )
      self.logger(f"[Target] Dwell progress {elapsed:.0f}/{total_seconds:.0f}s")
      scroll_bursts = random.randint(2, 4) if mobile else random.randint(1, 2)
      for _ in range(scroll_bursts):
        self._human_scroll_cycle(
          page, stopped, mobile=mobile, intensity="high" if mobile else "normal",
        )
        if stopped():
          return

      if not mobile and random.random() < 0.38:
        self._select_some_text(page)

      if (
        internal_clicks < target_internal_clicks
        and remaining > 12
        and elapsed >= next_internal_at
        and not stopped()
      ):
        if self._click_internal_link(
          page, stopped, post_click_read=True, profile=profile,
        ):
          internal_clicks += 1
          self.logger(
            f"[Target] Internal link click {internal_clicks}/{target_internal_clicks}"
          )
          next_internal_at = elapsed + random.uniform(
            max(8.0, total_seconds * 0.15),
            max(18.0, total_seconds * 0.35),
          )
        else:
          next_internal_at = elapsed + random.uniform(6.0, 12.0)

      waited_ms = int(cycle_seconds * 1000)
      self._interruptible_wait(page, waited_ms, stopped, scroll_mobile=mobile)
      elapsed += cycle_seconds

    while internal_clicks < target_internal_clicks and not stopped():
      if self._click_internal_link(
        page, stopped, post_click_read=False, profile=profile,
      ):
        internal_clicks += 1
        self.logger(
          f"[Target] Internal link click {internal_clicks}/{target_internal_clicks} (final pass)"
        )
      else:
        break

  def _human_scroll_cycle(
    self,
    page: Page,
    stopped: Callable[[], bool],
    *,
    mobile: bool = False,
    intensity: str = "normal",
  ) -> None:
    if intensity == "high":
      scroll_count = random.randint(5, 9) if mobile else random.randint(3, 6)
      delay_range = (120, 450) if mobile else (250, 800)
      delta_range_down = (220, 620) if mobile else (180, 720)
      delta_range_up = (100, 280) if mobile else (120, 360)
    else:
      scroll_count = random.randint(2, 5)
      delay_range = (350, 1400)
      delta_range_down = (180, 720)
      delta_range_up = (120, 360)

    for _ in range(scroll_count):
      if stopped():
        return
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
  ) -> bool:
    if stopped():
      return False
    mobile = self._is_mobile_profile(profile) if profile else False
    domain = self._normalize_domain(self.config.target_domain)
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
      href = link.get_attribute("href") or ""
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
      if mobile and target_url:
        try:
          page = self._safe_goto(
            page,
            target_url,
            profile,
            self.logger,
            wait_until="domcontentloaded",
            timeout=10000,
          )
          if post_click_read:
            self._human_scroll_cycle(page, stopped, mobile=mobile)
            wait_ms = random.randint(4_000, 9_000)
            self._interruptible_wait(page, wait_ms, stopped)
          return True
        except Exception:
          pass
      try:
        human_click(
          link,
          self.config.action_delay_min,
          self.config.action_delay_max,
          page=page,
          mobile=mobile,
        )
        try:
          page.wait_for_load_state("domcontentloaded", timeout=8000 if mobile else 10000)
        except Exception:
          pass
        if post_click_read:
          self._human_scroll_cycle(page, stopped, mobile=mobile)
          wait_ms = random.randint(4_000, 9_000) if mobile else random.randint(8_000, 18_000)
          self._interruptible_wait(page, wait_ms, stopped)
        return True
      except Exception:
        if not target_url:
          continue
        try:
          page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
          if post_click_read:
            self._human_scroll_cycle(page, stopped, mobile=mobile)
            wait_ms = random.randint(4_000, 9_000) if mobile else random.randint(8_000, 18_000)
            self._interruptible_wait(page, wait_ms, stopped)
          return True
        except Exception:
          continue
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
      scroll_page(
        page,
        random.choice((-1, 1)) * random.randint(120, 420),
        mobile=mobile,
      )
      random_delay(self.config.action_delay_min, self.config.action_delay_max)
      elapsed += sleep_for

  @staticmethod
  def _normalize_domain(value: str) -> str:
    if "://" in value:
      host = urlparse(value).netloc
    else:
      host = value.split("/")[0]
    return host.lower().removeprefix("www.")
