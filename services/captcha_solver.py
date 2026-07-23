import json
import random
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from core.profile_status import UiStatusKey, ui_label
from config.settings_store import settings_path
from utils.app_paths import data_dir

StatusNotify = Callable[[str, str], None]
CaptchaStatCallback = Callable[[str], None]
ClearedNotify = Callable[[], None]


class CaptchaSolver:
  CREATE_URL = "https://api.capsolver.com/createTask"
  RESULT_URL = "https://api.capsolver.com/getTaskResult"
  MANUAL_CAPTCHA_TIMEOUT_SEC = 60.0
  MAX_AUTOMATED_SOLVE_ATTEMPTS = 3

  def __init__(self, api_key: str, logger: Callable[[str], None]):
    self.api_key = self._normalize_api_key(api_key)
    self.logger = logger
    self._http = httpx.Client(
      trust_env=False,
      timeout=httpx.Timeout(60.0, connect=25.0),
      headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    self.automated_mode = bool(self.api_key)
    self._awaiting_clear = False
    self._session_logger: Optional[Callable[[str], None]] = None
    self._session_context: dict[str, str] = {}
    self._proxy_capsolver: str = ""
    self._proxy_capsolver_unusable: bool = False
    self._last_api_error: str = ""
    self._stats_callback: Optional[CaptchaStatCallback] = None
    self._automated_attempts: int = 0

  def set_stats_callback(self, callback: Optional[CaptchaStatCallback]) -> None:
    self._stats_callback = callback

  def _notify_stats(self, event: str) -> None:
    if event not in ("detected", "auto_solved") or not self._stats_callback:
      return
    try:
      self._stats_callback(event)
    except Exception:
      pass

  def set_session_context(
    self,
    *,
    profile_id: str = "",
    profile_name: str = "",
    proxy: str = "",
    keyword: str = "",
    proxy_host: str = "",
    proxy_port: int = 0,
    proxy_user: str = "",
    proxy_pass: str = "",
  ) -> None:
    self._session_context = {
      "profile_id": profile_id,
      "profile_name": profile_name,
      "proxy": proxy,
      "keyword": keyword,
    }
    self._proxy_capsolver = self._format_capsolver_proxy(
      proxy_host, proxy_port, proxy_user, proxy_pass,
    )

  @staticmethod
  def _format_capsolver_proxy(
    host: str,
    port: int,
    user: str,
    password: str,
  ) -> str:
    host = (host or "").strip()
    if not host or host in ("—", "-") or int(port or 0) <= 0:
      return ""
    user = (user or "").strip()
    password = (password or "").strip()
    if user and password:
      return f"http:{host}:{int(port)}:{user}:{password}"
    return f"http:{host}:{int(port)}"

  def update_keyword_context(self, keyword: str) -> None:
    self._session_context["keyword"] = keyword or ""

  def _log_captcha_event(self, event: str, captcha_type: str = "") -> None:
    payload = {
      "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
      "event": event,
      "profile_id": self._session_context.get("profile_id", ""),
      "profile_name": self._session_context.get("profile_name", ""),
      "proxy": self._session_context.get("proxy", ""),
      "keyword": self._session_context.get("keyword", ""),
      "captcha_type": captcha_type,
    }
    try:
      events_path = data_dir() / "captcha_events.jsonl"
      events_path.parent.mkdir(parents=True, exist_ok=True)
      with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
      pass
    self._notify_stats(event)
    self._log(
      f"[Captcha] Event={event} profile={payload['profile_id']} "
      f"proxy={payload['proxy']} keyword={payload['keyword']} type={captcha_type or 'unknown'}"
    )

  def set_session_logger(self, logger: Optional[Callable[[str], None]]) -> None:
    self._session_logger = logger

  def _log(self, message: str) -> None:
    if self._session_logger:
      self._session_logger(message)
    else:
      self.logger(message)

  def reset_session_state(self) -> None:
    self._awaiting_clear = False
    self._last_api_error = ""
    self._proxy_capsolver_unusable = False
    self._automated_attempts = 0

  def is_awaiting_clear(self) -> bool:
    return self._awaiting_clear

  @staticmethod
  def _url_indicates_captcha(url: str) -> bool:
    lowered = (url or "").lower()
    return any(
      token in lowered
      for token in (
        "/sorry",
        "sorry/index",
        "google.com/sorry",
        "ipv4.google.com/sorry",
        "recaptcha",
      )
    )

  def requires_captcha_clear(self, page: Page) -> bool:
    if self._awaiting_clear:
      if page.is_closed():
        return True
      return self._page_needs_captcha_clear(page)
    if page.is_closed():
      return False
    return self._page_needs_captcha_clear(page)

  def _page_needs_captcha_clear(self, page: Page) -> bool:
    try:
      if self._url_indicates_captcha(page.url or ""):
        return True
    except Exception:
      pass
    return self.is_captcha_present(page)

  @staticmethod
  def _normalize_api_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if key:
      return key
    return CaptchaSolver._load_key_from_settings()

  @staticmethod
  def _load_key_from_settings() -> str:
    try:
      path = settings_path()
      if not path.exists():
        return ""
      data = json.loads(path.read_text(encoding="utf-8"))
      return (data.get("capsolver_api_key") or "").strip()
    except Exception:
      return ""

  def update_api_key(self, api_key: str) -> None:
    key = self._normalize_api_key(api_key)
    if key == self.api_key:
      return
    self.api_key = key
    self.automated_mode = bool(self.api_key)

  def _mask_api_key(self) -> str:
    if len(self.api_key) <= 12:
      return "(empty)" if not self.api_key else "***"
    return f"{self.api_key[:8]}...{self.api_key[-4:]}"

  @staticmethod
  def _is_page_closed_error(exc: BaseException) -> bool:
    text = str(exc or "").upper()
    return (
      "TARGET PAGE, CONTEXT OR BROWSER HAS BEEN CLOSED" in text
      or "TARGETCLOSEDERROR" in text
      or "EXECUTION CONTEXT WAS DESTROYED" in text
      or "FRAME WAS DETACHED" in text
    )

  def _announce_captcha_detected(
    self,
    page: Page,
    on_status: Optional[StatusNotify],
    *,
    captcha_type: str = "",
  ) -> str:
    self._awaiting_clear = True
    self._log("[Captcha] 1. CAPTCHA DETECTED — workflow paused until solved (WAIT_CAPTCHA)")
    if on_status:
      try:
        on_status(UiStatusKey.CAPTCHA.value, ui_label(UiStatusKey.CAPTCHA))
      except Exception as status_exc:
        self._log(f"[Captcha] UI status update warning (continuing solve): {status_exc}")
    detected_type = captcha_type or self._detect_captcha_type(page)
    self._log_captcha_event("detected", detected_type)
    return detected_type

  def _captcha_still_blocking(self, page: Page) -> bool:
    if page.is_closed():
      return self._awaiting_clear
    return self._page_needs_captcha_clear(page)

  def _handle_no_api_key_captcha(
    self,
    page: Page,
    on_status: Optional[StatusNotify],
    *,
    captcha_type: str = "",
  ) -> str:
    if not self._awaiting_clear:
      self._announce_captcha_detected(page, on_status, captcha_type=captcha_type)
    self._log("[Captcha] No CapSolver API key configured — removing profile")
    return "blocked"

  def handle_before_action(
    self,
    page: Page,
    stop_event: Optional[threading.Event] = None,
    on_status: Optional[StatusNotify] = None,
    on_cleared: Optional[ClearedNotify] = None,
    *,
    captcha_type: str = "",
  ) -> str:
    """
    Returns: 'ok' | 'stopped' | 'error' | 'blocked'
    """
    if stop_event and stop_event.is_set():
      return "stopped"

    if page.is_closed():
      if self._awaiting_clear:
        self._log("[Captcha] Work tab closed while captcha pending — removing profile")
        return "blocked"
      return "ok"
    try:
      captcha_present = self._is_captcha_present(page)
    except Exception as exc:
      if self._is_page_closed_error(exc):
        if self._awaiting_clear:
          self._log("[Captcha] Work tab closed during captcha check — removing profile")
          return "blocked"
        return "ok"
      raise
    if not captcha_present and not self._awaiting_clear:
      return "ok"

    if not self.automated_mode:
      return self._handle_no_api_key_captcha(
        page, on_status, captcha_type=captcha_type,
      )

    self.logger(f"[CapSolver] Using API key {self._mask_api_key()}")
    detected_type = captcha_type

    while self._captcha_still_blocking(page):
      if stop_event and stop_event.is_set():
        return "stopped"
      if page.is_closed():
        self._log("[Captcha] Work tab closed during CapSolver flow — removing profile")
        return "blocked"
      if self._automated_attempts >= self.MAX_AUTOMATED_SOLVE_ATTEMPTS:
        self._log(
          f"[Captcha] CapSolver attempt limit reached "
          f"({self.MAX_AUTOMATED_SOLVE_ATTEMPTS}/{self.MAX_AUTOMATED_SOLVE_ATTEMPTS}) "
          "— removing profile"
        )
        return "blocked"

      if not self._awaiting_clear:
        detected_type = self._announce_captcha_detected(
          page, on_status, captcha_type=captcha_type,
        )
      else:
        self._log("[Captcha] Captcha still present — retrying CapSolver")

      self._automated_attempts += 1
      self._log(
        f"[Captcha] CapSolver attempt "
        f"{self._automated_attempts}/{self.MAX_AUTOMATED_SOLVE_ATTEMPTS}"
      )
      result = self._solve_automated(page, on_status, detected_type)
      if result == "ok":
        self._notify_captcha_cleared(on_cleared)
        self._awaiting_clear = False
        page = self._refresh_page_after_captcha(page)
        if not self._captcha_still_blocking(page):
          return "ok"
        self._log("[Captcha] Captcha reappeared after solve — retrying CapSolver")
        captcha_type = ""
        continue

      if result == "stopped":
        return "stopped"

      self._log(
        f"[Captcha] CapSolver attempt {self._automated_attempts} failed "
        f"({self._last_api_error or 'captcha not cleared'})"
      )
      if self._automated_attempts >= self.MAX_AUTOMATED_SOLVE_ATTEMPTS:
        self._log("[Captcha] CapSolver attempts exhausted — removing profile")
        return "blocked"
      time.sleep(random.uniform(1.0, 2.0))

    self._awaiting_clear = False
    return "ok"

  @staticmethod
  def _notify_captcha_cleared(on_cleared: Optional[ClearedNotify]) -> None:
    if not on_cleared:
      return
    try:
      on_cleared()
    except Exception:
      pass

  def is_captcha_present(self, page: Page) -> bool:
    if page.is_closed():
      return False
    try:
      return self._is_captcha_present(page)
    except Exception as exc:
      if self._is_page_closed_error(exc):
        return False
      raise

  def _post_json(self, url: str, payload: dict) -> dict:
    endpoint = url.rsplit("/", 1)[-1]
    last_error = "unknown error"
    for attempt in range(1, 4):
      try:
        response = self._http.post(url, json=payload)
        text = (response.text or "").strip()
        self.logger(
          f"[CapSolver] {endpoint} HTTP {response.status_code} (attempt {attempt}/3, "
          f"bytes={len(text)})"
        )
        if not text:
          last_error = f"empty body (HTTP {response.status_code})"
          time.sleep(min(attempt * 2, 5))
          continue

        try:
          data = response.json()
        except ValueError:
          self.logger(f"[CapSolver] Non-JSON body: {text[:240]}")
          return {"errorId": -1, "errorDescription": "non-json response"}

        if response.status_code >= 400 and self._capsolver_error(data):
          self.logger(
            f"[CapSolver] {endpoint} rejected: errorId={data.get('errorId')} "
            f"code={data.get('errorCode')} desc={data.get('errorDescription')}"
          )
        return data
      except httpx.RequestError as exc:
        last_error = str(exc)
        self.logger(f"[CapSolver] Network error on {endpoint} (attempt {attempt}/3): {exc}")
        time.sleep(min(attempt * 2, 5))

    self.logger(f"[CapSolver] All attempts failed for {endpoint}: {last_error}")
    return {"errorId": -1, "errorDescription": last_error}

  @staticmethod
  def _capsolver_error(result: dict) -> bool:
    error_id = result.get("errorId")
    if error_id in (None, 0, "0"):
      return False
    return True

  def _is_captcha_present(self, page: Page) -> bool:
    return page.evaluate(
      """() => {
        const isVisible = (el) => {
          if (!el) return false;
          const style = window.getComputedStyle(el);
          if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
            return false;
          }
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        };

        const url = (location.href || '').toLowerCase();
        if (
          url.includes('/sorry/') || url.includes('sorry/index') ||
          url.includes('ipv4.google.com/sorry') || url.includes('google.com/sorry')
        ) {
          return true;
        }

        const onNormalSerp = url.includes('/search') && !url.includes('/sorry');
        if (onNormalSerp) {
          const hasSearchResults = !!(
            document.querySelector('#rso a h3, div#search a h3, #rso div[data-hveid], div#center_col div[data-hveid]')
          );
          if (hasSearchResults) {
            const challenge = document.querySelector('iframe[src*="bframe"], iframe[title*="challenge" i]');
            if (isVisible(challenge)) return true;
            const captchaForm = document.querySelector('#captcha-form, form#captcha-form');
            if (isVisible(captchaForm)) return true;
            const text = (document.body?.innerText || '').toLowerCase();
            const blockingPhrases = [
              'i am not a robot',
              'unusual traffic',
              'verify you are human',
              'our systems have detected',
              '자동화된 트래픽',
              '비정상적인 트래픽',
              '비정상 트래픽',
              '로봇이 아닙니다',
            ];
            if (blockingPhrases.some((phrase) => text.includes(phrase))) return true;
            return false;
          }
        }

        const title = (document.title || '').toLowerCase();
        if (
          title.includes('unusual traffic') || title.includes('automated queries') ||
          title.includes('비정상') || title.includes('robot')
        ) {
          return true;
        }

        if (document.querySelector('#captcha, #captchaimg, input[name="captcha"], #captcha-form img')) {
          return true;
        }

        const captchaForm = document.querySelector('#captcha-form, form#captcha-form');
        const widget = document.querySelector('.g-recaptcha, #recaptcha.g-recaptcha, #recaptcha[data-sitekey]');
        if (captchaForm || (widget && widget.getAttribute('data-sitekey'))) {
          return true;
        }

        const response = document.querySelector('#g-recaptcha-response')
          || document.querySelector('textarea[name="g-recaptcha-response"]');
        const iframe = document.querySelector('iframe[src*="recaptcha"]');
        const challenge = document.querySelector('iframe[src*="bframe"], iframe[title*="challenge" i]');
        const hcaptchaFrame = document.querySelector('iframe[src*="hcaptcha"]');
        const turnstileFrame = document.querySelector('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]');
        const hcaptchaWidget = document.querySelector('[data-sitekey][data-captcha], .h-captcha');
        const turnstileWidget = document.querySelector('.cf-turnstile');
        const enterpriseScript = document.querySelector('script[src*="recaptcha/enterprise"]');

        if (isVisible(challenge)) return true;
        if (isVisible(widget)) return true;
        if (isVisible(captchaForm) && (widget || enterpriseScript)) return true;
        if (isVisible(response)) return true;
        if (isVisible(hcaptchaFrame) || isVisible(turnstileFrame)) return true;
        if (isVisible(hcaptchaWidget) || isVisible(turnstileWidget)) return true;
        if (enterpriseScript && (widget || captchaForm)) return true;

        if (iframe && isVisible(iframe)) {
          const rect = iframe.getBoundingClientRect();
          if (rect.width >= 40 && rect.height >= 40) return true;
        }

        const text = (document.body?.innerText || '').toLowerCase();
        const captchaPhrases = [
          'i am not a robot',
          'unusual traffic',
          'verify you are human',
          'our systems have detected',
          '자동화된 트래픽',
          '비정상적인 트래픽',
          '비정상 트래픽',
          '로봇이 아닙니다',
        ];
        if (captchaPhrases.some((phrase) => text.includes(phrase))) return true;
        return false;
      }"""
    )

  def _is_captcha_cleared(self, page: Page) -> bool:
    return page.evaluate(
      """() => {
        const isVisible = (el) => {
          if (!el) return false;
          const style = window.getComputedStyle(el);
          if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
            return false;
          }
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        };

        const url = (location.href || '').toLowerCase();
        if (url.includes('/sorry/') || url.includes('ipv4.google.com/sorry')) {
          return false;
        }

        const text = (document.body?.innerText || '').toLowerCase();
        const captchaPhrases = [
          'i am not a robot',
          'unusual traffic',
          'verify you are human',
          'our systems have detected',
          '자동화된 트래픽',
          '비정상적인 트래픽',
          '비정상 트래픽',
          '로봇이 아닙니다',
        ];
        if (captchaPhrases.some((phrase) => text.includes(phrase))) {
          return false;
        }

        const captchaForm = document.querySelector('#captcha-form, form#captcha-form');
        const widget = document.querySelector('.g-recaptcha, #recaptcha.g-recaptcha, #recaptcha[data-sitekey]');
        const challenge = document.querySelector('iframe[src*="bframe"], iframe[title*="challenge" i]');

        if (isVisible(challenge)) return false;
        if (isVisible(widget)) return false;
        if (isVisible(captchaForm)) return false;

        if (url.includes('/search') && !url.includes('/sorry')) return true;

        const organic = document.querySelector('#rso a h3, div#search a h3, a:has(h3)');
        if (organic) return true;

        const hasSearchResults = !!(
          document.querySelector('#rso div[data-hveid], div#center_col div[data-hveid]')
        );
        if (hasSearchResults) return true;

        if (url.includes('google.') && !url.includes('/sorry')) {
          return true;
        }

        const response = document.querySelector('#g-recaptcha-response')
          || document.querySelector('textarea[name="g-recaptcha-response"]');
        return !isVisible(response);
      }"""
    )

  def _detect_captcha_type(self, page: Page) -> str:
    try:
      meta = page.evaluate(
        """() => {
          if (document.querySelector('iframe[src*="hcaptcha"]')) return 'hcaptcha';
          if (document.querySelector('.cf-turnstile, iframe[src*="turnstile"]')) return 'turnstile';
          if (document.querySelector('#captcha, #captchaimg, input[name="captcha"]')) return 'image';
          if (document.querySelector('script[src*="recaptcha/enterprise"]')) return 'recaptcha_enterprise';
          if (document.querySelector('.g-recaptcha, iframe[src*="recaptcha"]')) return 'recaptcha_v2';
          const url = (location.href || '').toLowerCase();
          if (url.includes('/sorry/')) return 'google_sorry';
          return 'unknown';
        }"""
      )
      return str(meta or "unknown")
    except Exception:
      return "unknown"

  def _refresh_page_after_captcha(self, page: Page) -> Page:
    """Reload page after captcha solve so DOM/locators are fresh."""
    if page.is_closed():
      return page
    try:
      if self._is_captcha_cleared(page):
        self._log("[Captcha] Page already clear after solve — skipping reload")
        self._log_captcha_event("solved", self._detect_captcha_type(page))
        return page
    except Exception:
      pass
    self._log("[Captcha] Reloading page after solve (fresh DOM, no stale locators)")
    try:
      page.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
      self._log(f"[Captcha] Reload warning: {exc}")
      return page
    try:
      page.wait_for_timeout(400)
    except Exception as exc:
      self._log(f"[Captcha] Load-state wait warning: {exc}")
    self._log_captcha_event("solved", self._detect_captcha_type(page))
    return page

  def _solve_automated(self, page: Page, on_status: Optional[StatusNotify], captcha_type: str = "") -> str:
    if on_status:
      on_status(UiStatusKey.CAPTCHA.value, ui_label(UiStatusKey.CAPTCHA))

    sitekey, task_type, enterprise_s, callback_name, is_google = self._wait_for_captcha_meta(
      page, timeout_sec=6.0,
    )
    if not sitekey:
      self.logger("[CapSolver] Captcha detected but sitekey not found after waiting")
      return "error"

    resolved_type = captcha_type or task_type

    raw_url = (page.url or "").strip().split("#", 1)[0]
    page_url = self._normalize_website_url(raw_url, is_google=is_google)
    self.logger(
      f"[CapSolver] Automated solve ({task_type}, sitekey={sitekey}, url={page_url}"
      + (", data-s present)" if enterprise_s else ")")
    )
    if is_google and not enterprise_s:
      self.logger("[CapSolver] Warning: Google enterprise captcha without data-s — solve may fail")

    self._log("[Captcha] 2. CAPTCHA SOLVE REQUEST: API")
    deadline = time.time() + 120.0
    remaining = max(1, int(deadline - time.time()))
    token = self._fetch_token(
      page_url,
      sitekey,
      task_type,
      enterprise_s=enterprise_s,
      is_google=is_google,
      timeout=remaining,
      raw_page_url=raw_url,
    )
    if not token:
      detail = self._last_api_error or "no token from CapSolver"
      self.logger(f"[CapSolver] Failed to obtain token within solve window ({detail})")
      self._log(f"[Captcha] 3. CAPTCHA SOLVE FAILED ({detail})")
      return "blocked"

    self.logger("[CapSolver] Applying token to browser page")
    if not self._apply_recaptcha_token(page, token, callback_name):
      self.logger("[CapSolver] Token injection/submit did not clear captcha page")

    while time.time() < deadline:
      if self._is_captcha_cleared(page):
        self.logger("[CapSolver] Captcha solved, resuming workflow")
        self._log("[Captcha] 3. CAPTCHA AUTO SOLVED")
        self._log_captcha_event("auto_solved", resolved_type)
        return "ok"
      page.wait_for_timeout(1000)

    self.logger("[CapSolver] Captcha not cleared within solve window")
    self._log("[Captcha] 3. CAPTCHA SOLVE FAILED")
    return "blocked"

  def _apply_recaptcha_token(self, page: Page, token: str, callback_name: Optional[str]) -> bool:
    inject_result = page.evaluate(
      """({ token, callbackName }) => {
        const form = document.querySelector('#captcha-form, form#captcha-form, form');
        let textarea = document.querySelector('#g-recaptcha-response')
          || document.querySelector('textarea[name="g-recaptcha-response"]');
        if (!textarea && form) {
          textarea = document.createElement('textarea');
          textarea.id = 'g-recaptcha-response';
          textarea.name = 'g-recaptcha-response';
          textarea.style.display = 'none';
          form.appendChild(textarea);
        }
        if (textarea) {
          textarea.innerHTML = token;
          textarea.value = token;
          textarea.dispatchEvent(new Event('input', { bubbles: true }));
          textarea.dispatchEvent(new Event('change', { bubbles: true }));
        }

        let method = 'none';
        if (callbackName && typeof window[callbackName] === 'function') {
          window[callbackName](token);
          method = 'callback:' + callbackName;
        } else if (typeof submitCallback === 'function') {
          submitCallback(token);
          method = 'submitCallback';
        } else if (typeof window.___grecaptcha_cfg !== 'undefined') {
          const clients = window.___grecaptcha_cfg.clients || {};
          for (const id of Object.keys(clients)) {
            const client = clients[id];
            const callback = client?.callback || client?.W?.callback || client?.V?.callback;
            if (typeof callback === 'function') {
              callback(token);
              method = 'grecaptcha_cfg';
              break;
            }
          }
        } else if (form) {
          form.submit();
          method = 'form.submit';
        }

        const postUrl = form
          ? new URL(form.getAttribute('action') || 'index', location.href).toString()
          : '';
        return {
          method,
          postUrl,
          q: form?.querySelector('[name="q"]')?.value || '',
          continueUrl: form?.querySelector('[name="continue"]')?.value || '',
          onSorryPage: (location.href || '').toLowerCase().includes('/sorry/'),
        };
      }""",
      {"token": token, "callbackName": callback_name},
    )
    self.logger(f"[CapSolver] Token inject method: {inject_result.get('method', 'none')}")

    try:
      page.wait_for_function(
        """() => {
          const url = (location.href || '').toLowerCase();
          if (!url.includes('/sorry/')) return true;
          const text = (document.body?.innerText || '').toLowerCase();
          const blocked = ['비정상', 'unusual traffic', '로봇이 아닙니다', 'i am not a robot'];
          return !blocked.some((phrase) => text.includes(phrase));
        }""",
        timeout=15000,
      )
      self.logger("[CapSolver] Page left captcha/sorry state after token inject")
      return True
    except PlaywrightTimeoutError:
      pass

    if inject_result.get("postUrl") and (inject_result.get("onSorryPage") or inject_result.get("q")):
      return self._post_google_sorry_form(page, token, inject_result)

    try:
      with page.expect_navigation(timeout=20000, wait_until="domcontentloaded"):
        page.evaluate(
          """({ token, callbackName }) => {
            const form = document.querySelector('#captcha-form, form#captcha-form, form');
            if (form) form.submit();
          }""",
          {"token": token, "callbackName": callback_name},
        )
    except PlaywrightTimeoutError:
      pass

    return self._is_captcha_cleared(page)

  def _post_google_sorry_form(self, page: Page, token: str, form_info: dict) -> bool:
    post_url = (form_info.get("postUrl") or "").strip()
    continue_url = (form_info.get("continueUrl") or "").strip()
    q_value = form_info.get("q") or ""
    if not post_url:
      self.logger("[CapSolver] Sorry-page POST fallback skipped: no post URL")
      return False

    self.logger(f"[CapSolver] POST fallback to {post_url}")
    try:
      response = page.context.request.post(
        post_url,
        form={
          "q": q_value,
          "continue": continue_url,
          "g-recaptcha-response": token,
        },
        headers={"Referer": page.url},
        timeout=60000,
      )
      self.logger(f"[CapSolver] Sorry form POST HTTP {response.status}")
    except Exception as exc:
      self.logger(f"[CapSolver] Sorry form POST failed: {exc}")
      return False

    if continue_url:
      try:
        page.goto(continue_url, wait_until="domcontentloaded", timeout=60000)
        self.logger(f"[CapSolver] Navigated to continue URL after sorry POST")
      except Exception as exc:
        self.logger(f"[CapSolver] Continue navigation warning: {exc}")

    cleared = self._is_captcha_cleared(page)
    if cleared:
      self.logger("[CapSolver] Captcha cleared after sorry POST fallback")
    return cleared

  def _wait_manual_resolution(
    self,
    page: Page,
    stop_event: Optional[threading.Event],
    on_status: Optional[StatusNotify],
    captcha_type: str = "",
  ) -> str:
    if on_status:
      on_status(UiStatusKey.CAPTCHA_MANUAL.value, ui_label(UiStatusKey.CAPTCHA_MANUAL))
    self._log("[Captcha] 2. CAPTCHA SOLVE REQUEST: MANUAL")

    self._log(
      f"[Captcha] Manual mode — waiting up to {self.MANUAL_CAPTCHA_TIMEOUT_SEC:.0f}s "
      "for captcha solve (do not close the tab)"
    )

    started_at = time.time()
    last_heartbeat = time.time()
    last_closed_notice = 0.0
    while True:
      if stop_event and stop_event.is_set():
        self._log("[Captcha] Manual wait cancelled by stop signal")
        return "stopped"

      if time.time() - started_at >= self.MANUAL_CAPTCHA_TIMEOUT_SEC:
        self._log(
          f"[Captcha] Manual wait timed out after {self.MANUAL_CAPTCHA_TIMEOUT_SEC:.0f}s "
          "— profile will be removed"
        )
        self._log_captcha_event(
          "manual_timeout",
          captcha_type or "unknown",
        )
        self._awaiting_clear = False
        return "blocked"

      if page.is_closed():
        if time.time() - last_closed_notice >= 15.0:
          self._log(
            "[Captcha] Work tab is closed — please solve captcha on the browser. "
            "Bot will keep waiting until solved or Stop is pressed."
          )
          last_closed_notice = time.time()
        time.sleep(random.uniform(0.8, 1.3))
        continue

      try:
        cleared = self._is_captcha_cleared(page)
      except Exception as exc:
        if self._is_page_closed_error(exc):
          if time.time() - last_closed_notice >= 15.0:
            self._log(
              "[Captcha] Work tab closed while checking captcha — still waiting for manual solve"
            )
            last_closed_notice = time.time()
          time.sleep(random.uniform(0.8, 1.3))
          continue
        raise

      if cleared:
        self._awaiting_clear = False
        self._log("[Captcha] Manual captcha cleared — resuming workflow")
        self._log("[Captcha] 3. CAPTCHA MANUAL SOLVED")
        self._log_captcha_event("manual_solved", captcha_type or self._detect_captcha_type(page))
        page.wait_for_timeout(500)
        return "ok"

      if time.time() - last_heartbeat >= 15.0:
        self._log("[Captcha] Manual wait in progress — captcha still visible in browser")
        last_heartbeat = time.time()

      time.sleep(random.uniform(0.8, 1.3))

  def _wait_for_captcha_meta(
    self,
    page: Page,
    timeout_sec: float = 6.0,
  ) -> tuple[Optional[str], str, Optional[str], Optional[str], bool]:
    deadline = time.time() + timeout_sec
    attempt = 0
    while time.time() < deadline:
      attempt += 1
      meta = self._extract_captcha_meta(page)
      if meta[0]:
        if attempt > 1:
          self.logger(f"[CapSolver] Sitekey found after {attempt} attempt(s)")
        return meta
      try:
        page.wait_for_selector(
          '#recaptcha, .g-recaptcha, [data-sitekey], iframe[src*="recaptcha"], #captcha-form',
          timeout=1000,
          state="attached",
        )
      except Exception:
        pass
      page.wait_for_timeout(300)
    return self._extract_captcha_meta(page)

  def _extract_captcha_meta(
    self,
    page: Page,
  ) -> tuple[Optional[str], str, Optional[str], Optional[str], bool]:
    meta = page.evaluate(
      """() => {
        const el = document.querySelector('[data-sitekey]')
          || document.querySelector('#recaptcha.g-recaptcha')
          || document.querySelector('#recaptcha[data-sitekey]');
        const sitekey = el ? el.getAttribute('data-sitekey') : null;
        const dataS = el ? el.getAttribute('data-s') : null;
        const callbackName = el ? el.getAttribute('data-callback') : null;
        const hasEnterprise = !!document.querySelector('script[src*="recaptcha/enterprise"]');
        const hasV3Badge = !!document.querySelector('.grecaptcha-badge');
        const isGoogle = (location.hostname || '').includes('google.');
        if (!sitekey) {
          const iframe = document.querySelector('iframe[src*="recaptcha"]');
          if (iframe && iframe.src) {
            const match = iframe.src.match(/[?&]k=([^&]+)/);
            if (match) {
              return {
                sitekey: decodeURIComponent(match[1]),
                dataS,
                callbackName,
                hasEnterprise,
                hasV3Badge,
                isGoogle,
              };
            }
          }
        }
        return { sitekey, dataS, callbackName, hasEnterprise, hasV3Badge, isGoogle };
      }"""
    )
    sitekey = meta.get("sitekey")
    if not sitekey:
      return None, "", None, None, bool(meta.get("isGoogle"))

    is_google = bool(meta.get("isGoogle"))
    if meta.get("hasV3Badge"):
      return sitekey, "ReCaptchaV3TaskProxyLess", None, meta.get("callbackName"), is_google
    if meta.get("hasEnterprise") or is_google:
      return (
        sitekey,
        "ReCaptchaV2EnterpriseTaskProxyLess",
        meta.get("dataS"),
        meta.get("callbackName"),
        is_google,
      )
    return sitekey, "ReCaptchaV2TaskProxyLess", None, meta.get("callbackName"), is_google

  @staticmethod
  def _google_sorry_base_url() -> str:
    return "https://www.google.com/sorry/index"

  @staticmethod
  def _normalize_website_url(page_url: str, *, is_google: bool) -> str:
    raw = (page_url or "").strip()
    if not raw:
      return CaptchaSolver._google_sorry_base_url()
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if is_google or "google." in host:
      if "/sorry/" in path:
        return CaptchaSolver._google_sorry_base_url()
      return CaptchaSolver._google_sorry_base_url()
    return raw.split("#", 1)[0]

  def _build_task_variants(
    self,
    page_url: str,
    sitekey: str,
    captcha_type: str,
    *,
    enterprise_s: Optional[str],
    is_google: bool,
    proxy: Optional[str] = None,
    raw_page_url: str = "",
  ) -> list[dict]:
    if captcha_type == "ReCaptchaV3TaskProxyLess":
      return [{
        "type": "ReCaptchaV3TaskProxyLess",
        "websiteURL": page_url or self._google_sorry_base_url(),
        "websiteKey": sitekey,
        "pageAction": "verify",
      }]

    website_urls: list[str] = []
    base_url = self._google_sorry_base_url()
    full = (raw_page_url or page_url or "").strip().split("#", 1)[0]
    if is_google:
      website_urls.append(base_url)
      if full and full != base_url and "/sorry/" in full.lower():
        website_urls.append(full)
    else:
      website_urls.append(page_url or base_url)

    is_enterprise = bool(
      is_google
      or enterprise_s
      or "Enterprise" in captcha_type
      or captcha_type == "recaptcha_enterprise"
    )
    proxy_value = (proxy or self._proxy_capsolver or "").strip()
    use_proxy = bool(proxy_value) and not self._proxy_capsolver_unusable

    variants: list[dict] = []
    seen: set[str] = set()

    def add_variant(
      task_type: str,
      url: str,
      *,
      enterprise: bool = False,
      invisible: Optional[bool] = None,
      with_proxy: bool = False,
    ) -> None:
      task: dict = {
        "type": task_type,
        "websiteURL": url,
        "websiteKey": sitekey,
      }
      if is_google:
        task["apiDomain"] = "www.google.com"
      if invisible is True:
        task["isInvisible"] = True
      if enterprise_s and (enterprise or "Enterprise" in task_type):
        task["enterprisePayload"] = {"s": enterprise_s}
      elif enterprise_s and not enterprise and "Enterprise" not in task_type:
        task["recaptchaDataSValue"] = enterprise_s
      if with_proxy and proxy_value:
        task["proxy"] = proxy_value
      signature = json.dumps(task, sort_keys=True)
      if signature in seen:
        return
      seen.add(signature)
      variants.append(task)

    if is_google and is_enterprise:
      # Proxyless Google enterprise is rejected by CapSolver; proxy+visible works.
      if use_proxy:
        add_variant(
          "ReCaptchaV2EnterpriseTask",
          website_urls[0],
          enterprise=True,
          invisible=False,
          with_proxy=True,
        )
        if len(website_urls) > 1:
          add_variant(
            "ReCaptchaV2EnterpriseTask",
            website_urls[1],
            enterprise=True,
            invisible=False,
            with_proxy=True,
          )
        add_variant(
          "ReCaptchaV2EnterpriseTask",
          website_urls[0],
          enterprise=True,
          invisible=True,
          with_proxy=True,
        )
      return variants

    for url in website_urls:
      add_variant(
        "ReCaptchaV2EnterpriseTaskProxyLess",
        url,
        enterprise=True,
        invisible=False,
      )
      if use_proxy:
        add_variant(
          "ReCaptchaV2EnterpriseTask",
          url,
          enterprise=True,
          invisible=False,
          with_proxy=True,
        )

    if not is_enterprise:
      add_variant(
        "ReCaptchaV2TaskProxyLess",
        website_urls[0],
        invisible=False,
      )

    return variants

  @staticmethod
  def _is_proxy_connect_error(code: str, desc: str) -> bool:
    text = f"{code} {desc}".upper()
    return "PROXY_CONNECT" in text or "PROXY" in text and "CONNECT" in text

  def _fetch_token(
    self,
    page_url: str,
    sitekey: str,
    captcha_type: str,
    *,
    enterprise_s: Optional[str] = None,
    is_google: bool = False,
    timeout: int = 120,
    raw_page_url: str = "",
  ) -> Optional[str]:
    self._last_api_error = ""
    deadline = time.time() + max(30, int(timeout))
    variants = self._build_task_variants(
      page_url,
      sitekey,
      captcha_type,
      enterprise_s=enterprise_s,
      is_google=is_google,
      proxy=self._proxy_capsolver or None,
      raw_page_url=raw_page_url,
    )
    if not variants:
      self.logger("[CapSolver] No task variants available for this captcha")
      return None

    for index, task in enumerate(variants, start=1):
      remaining = max(25, int(deadline - time.time()))
      if remaining <= 8:
        self.logger("[CapSolver] Solve window exhausted before trying more variants")
        break
      if task.get("proxy") and self._proxy_capsolver_unusable:
        continue
      task_type = task.get("type", "")
      task_url = task.get("websiteURL", page_url)
      flags = ["visible" if not task.get("isInvisible") else "invisible"]
      if task.get("enterprisePayload"):
        flags.append("data-s")
      if task.get("proxy"):
        flags.append("proxy")
      flag_text = ", ".join(flags)
      self.logger(
        "[CapSolver] Sending createTask to api.capsolver.com "
        f"(variant {index}/{len(variants)}, type={task_type}, websiteURL={task_url}, "
        f"websiteKey={sitekey}, {flag_text}, budget={remaining}s)"
      )
      token, create_error = self._create_and_poll_task(task, timeout=remaining)
      if token:
        return token
      if create_error and self._is_proxy_connect_error(
        create_error.get("code", ""),
        create_error.get("desc", ""),
      ):
        self._proxy_capsolver_unusable = True
        self.logger("[CapSolver] Proxy rejected by CapSolver — skipping further proxy variants")
      self.logger(f"[CapSolver] Variant {index} ({task_type}) did not return a token")

    return None

  def _create_and_poll_task(self, task: dict, *, timeout: int) -> tuple[Optional[str], Optional[dict]]:
    create_response = self._post_json(
      self.CREATE_URL,
      {"clientKey": self.api_key, "task": task},
    )
    if self._capsolver_error(create_response):
      code = create_response.get("errorCode") or ""
      desc = create_response.get("errorDescription") or ""
      self._last_api_error = f"{code}: {desc}".strip(": ").strip() or "createTask rejected"
      self.logger(
        f"[CapSolver] createTask error: errorId={create_response.get('errorId')} "
        f"code={code} desc={desc}"
      )
      return None, {"code": code, "desc": desc, "stage": "createTask"}

    task_id = create_response.get("taskId")
    if not task_id:
      self.logger(f"[CapSolver] createTask missing taskId: {create_response}")
      return None, {"code": "", "desc": "missing taskId", "stage": "createTask"}

    self.logger(f"[CapSolver] createTask accepted by CapSolver: taskId={task_id}")
    poll_deadline = time.time() + timeout
    poll_count = 0
    while time.time() < poll_deadline:
      poll_count += 1
      result = self._post_json(
        self.RESULT_URL,
        {"clientKey": self.api_key, "taskId": task_id},
      )
      if self._capsolver_error(result):
        code = result.get("errorCode") or ""
        desc = result.get("errorDescription") or ""
        self._last_api_error = f"{code}: {desc}".strip(": ").strip() or "getTaskResult failed"
        self.logger(
          f"[CapSolver] getTaskResult error: errorId={result.get('errorId')} "
          f"code={code} desc={desc}"
        )
        return None, {"code": code, "desc": desc, "stage": "getTaskResult"}

      status = result.get("status")
      if poll_count == 1 or poll_count % 5 == 0:
        self.logger(f"[CapSolver] getTaskResult status: {status}")
      if status == "ready":
        solution = result.get("solution") or {}
        token = solution.get("gRecaptchaResponse") or solution.get("token")
        if token:
          self.logger("[CapSolver] Token received from CapSolver")
          return token, None
        self.logger(f"[CapSolver] ready response missing token: {result}")
        return None, {"code": "", "desc": "missing token", "stage": "getTaskResult"}
      if status == "failed":
        self.logger(f"[CapSolver] solve failed: {result}")
        return None, {"code": "", "desc": "solve failed", "stage": "getTaskResult"}
      time.sleep(1)

    self.logger("[CapSolver] solve timed out while polling getTaskResult")
    return None, None
