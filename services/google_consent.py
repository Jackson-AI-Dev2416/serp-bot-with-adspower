import random
import time
from typing import Callable, Optional

from playwright.sync_api import BrowserContext, Page

ACCEPT_TEXTS = (
  "모두 수락",
  "모두 동의",
  "전체 동의",
  "Accept all",
  "Alle akzeptieren",
  "Tout accepter",
  "Aceptar todo",
  "Accetta tutto",
  "동의함",
  "I agree",
  "Agree",
)
CONSENT_DOMAINS = (".google.com", ".google.co.kr")

LOCATION_PROMPT_DISMISS_TEXTS = (
  "나중에",
  "Later",
  "Not now",
  "No thanks",
  "Skip",
  "취소",
)
LOCATION_PROMPT_PHRASES = (
  "더 가까운 위치의 검색 결과",
  "closer to your location",
  "closer search results",
  "use your device's precise location",
  "기기의 정확한 위치",
  "정확한 위치를 사용",
  "정확한 위치 사용",
)


def seed_google_consent_cookies(
  context: BrowserContext,
  logger: Optional[Callable[[str], None]] = None,
) -> None:
  """Pre-set consent cookies so the Google dialog is less likely to appear."""
  suffix = random.randint(100, 999)
  cookies = []
  for domain in CONSENT_DOMAINS:
    cookies.extend(
      [
        {
          "name": "CONSENT",
          "value": f"YES+cb.20210328-17-p0.en+FX+{suffix}",
          "domain": domain,
          "path": "/",
        },
        {
          "name": "SOCS",
          "value": "CAISEwgDEgk0NjI3MDkwNjIaAmtvIAEaBgiA_LyiBg",
          "domain": domain,
          "path": "/",
        },
      ]
    )
  try:
    context.add_cookies(cookies)
    if logger:
      logger("[Consent] Seeded Google consent cookies on browser context")
  except Exception as exc:
    if logger:
      logger(f"[Consent] Could not seed cookies (continuing): {exc}")


def is_google_consent_present(page: Page) -> bool:
  try:
    return page.evaluate(
      """() => {
        const url = (location.href || '').toLowerCase();
        if (url.includes('consent.google.')) return true;

        const text = (document.body?.innerText || '').toLowerCase();
        const phrases = [
          'google 서비스를 계속 이용하기 전에',
          'before you continue to google',
          'we use cookies and data to',
          '쿠키 및 데이터를 사용하는 방식',
        ];
        if (phrases.some((phrase) => text.includes(phrase))) return true;

        const acceptHints = ['모두 수락', 'accept all', 'alle akzeptieren', 'tout accepter'];
        const consentRoots = Array.from(document.querySelectorAll(
          'form[action*="consent"], [role="dialog"], [aria-modal="true"], #L2AGLb'
        ));
        for (const root of consentRoots) {
          const rootText = (root.innerText || root.textContent || '').trim().toLowerCase();
          if (acceptHints.some((hint) => rootText.includes(hint))) return true;
        }
        return false;
      }"""
    )
  except Exception:
    return False


def _consent_click_targets(page: Page) -> list:
  targets = [page]
  try:
    for frame in page.frames:
      frame_url = (frame.url or "").lower()
      if "consent.google." in frame_url or "consent" in frame_url:
        targets.append(frame)
  except Exception:
    pass
  return targets


def _click_consent_locator(scope, locator, page: Page) -> bool:
  try:
    if locator.count() <= 0:
      return False
    target = locator.first
    try:
      target.scroll_into_view_if_needed(timeout=1500)
    except Exception:
      pass
    target.click(timeout=5000, force=True)
    page.wait_for_timeout(1200)
    return True
  except Exception:
    return False


def _try_accept_in_scope(scope, page: Page) -> bool:
  for label in ACCEPT_TEXTS:
    locator = scope.get_by_role("button", name=label, exact=False)
    if _click_consent_locator(scope, locator, page):
      return True
    locator = scope.get_by_text(label, exact=False)
    if _click_consent_locator(scope, locator, page):
      return True

  css_selectors = (
    "#L2AGLb",
    'button[jsname="b3VHJd"]',
    'button[aria-label*="Accept"]',
    'button[aria-label*="accept"]',
    'button[aria-label*="수락"]',
    'button[aria-label*="동의"]',
    'form[action*="consent"] button',
  )
  for selector in css_selectors:
    locator = scope.locator(selector)
    if _click_consent_locator(scope, locator, page):
      return True
  return False


def _click_consent_via_js(page: Page) -> bool:
  return bool(
    page.evaluate(
      """() => {
        const acceptHints = [
          '모두 수락', '모두 동의', '전체 동의', 'accept all',
          'alle akzeptieren', 'tout accepter', '동의함',
        ];

        function isVisible(el) {
          if (!el) return false;
          const style = window.getComputedStyle(el);
          if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
            return false;
          }
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        }

        function walk(root) {
          const nodes = root.querySelectorAll('button, [role="button"], div[role="button"]');
          for (const el of nodes) {
            const text = (el.innerText || el.textContent || '').trim().toLowerCase();
            if (!text) continue;
            if (acceptHints.some((hint) => text.includes(hint)) && isVisible(el)) {
              el.click();
              return true;
            }
          }
          const all = root.querySelectorAll('*');
          for (const el of all) {
            if (el.shadowRoot && walk(el.shadowRoot)) return true;
          }
          return false;
        }

        return walk(document);
      }"""
    )
  )


def dismiss_google_consent(page: Page, logger: Callable[[str], None]) -> bool:
  if not is_google_consent_present(page):
    return False

  logger("[Consent] Google cookie/consent dialog detected — accepting")

  for attempt in range(2):
    for scope in _consent_click_targets(page):
      if _try_accept_in_scope(scope, page):
        page.wait_for_timeout(1200)
        if not is_google_consent_present(page):
          logger("[Consent] Google consent accepted")
          return True

    if _click_consent_via_js(page):
      page.wait_for_timeout(1500)
      if not is_google_consent_present(page):
        logger("[Consent] Google consent accepted via JS fallback")
        return True

    if attempt == 0:
      page.wait_for_timeout(800)

  deadline = time.time() + 3.0
  while time.time() < deadline:
    if not is_google_consent_present(page):
      logger("[Consent] Google consent cleared")
      return True
    page.wait_for_timeout(500)

  logger("[Consent] Could not auto-accept Google consent dialog")
  return False


def is_google_location_prompt_present(page: Page) -> bool:
  try:
    return page.evaluate(
      """(phrases) => {
        const text = (document.body?.innerText || '').toLowerCase();
        if (!text) return false;
        if (phrases.some((p) => text.includes(String(p).toLowerCase()))) return true;
        const dialogs = Array.from(document.querySelectorAll('[role="dialog"], div[aria-modal="true"]'));
        for (const dlg of dialogs) {
          const dlgText = (dlg.innerText || dlg.textContent || '').toLowerCase();
          if (phrases.some((p) => dlgText.includes(String(p).toLowerCase()))) return true;
        }
        return false;
      }""",
      list(LOCATION_PROMPT_PHRASES),
    )
  except Exception:
    return False


def dismiss_google_location_prompt(page: Page, logger: Callable[[str], None]) -> bool:
  """Dismiss Google SERP location nudge — click Later, never grant precise location."""
  if not is_google_location_prompt_present(page):
    return False

  logger("[Consent] Google location prompt detected — clicking Later")

  for label in LOCATION_PROMPT_DISMISS_TEXTS:
    locator = page.get_by_role("button", name=label, exact=False)
    try:
      if locator.count() > 0 and locator.first.is_visible(timeout=1000):
        locator.first.click(timeout=4000)
        page.wait_for_timeout(600)
        if not is_google_location_prompt_present(page):
          logger(f"[Consent] Location prompt dismissed via '{label}'")
          return True
    except Exception:
      continue

  clicked = page.evaluate(
    """() => {
      const dismissHints = ['나중에', 'later', 'not now', 'no thanks', 'skip'];
      const denyHints = ['정확한 위치', 'precise location', 'use location'];
      const nodes = Array.from(document.querySelectorAll('button, [role="button"], div[role="button"]'));
      for (const el of nodes) {
        const label = (el.innerText || el.textContent || '').trim().toLowerCase();
        if (!label) continue;
        if (denyHints.some((h) => label.includes(h))) continue;
        if (dismissHints.some((h) => label === h || label.includes(h))) {
          el.click();
          return label.slice(0, 40);
        }
      }
      return '';
    }"""
  )
  if clicked:
    page.wait_for_timeout(800)
    if not is_google_location_prompt_present(page):
      logger(f"[Consent] Location prompt dismissed via JS ({clicked})")
      return True

  logger("[Consent] Could not dismiss Google location prompt")
  return False


def deny_google_geolocation(page: Page, logger: Optional[Callable[[str], None]] = None) -> None:
  """Tell Chrome to deny geolocation for Google origins (reduces location modals)."""
  origins = ("https://www.google.com", "https://www.google.co.kr")
  try:
    browser = page.context.browser
    if browser is None:
      return
    cdp = browser.new_browser_cdp_session()
    for origin in origins:
      try:
        cdp.send(
          "Browser.setPermission",
          {
            "origin": origin,
            "permission": {"name": "geolocation"},
            "setting": "denied",
          },
        )
      except Exception:
        pass
    if logger:
      logger("[Consent] Geolocation denied for Google origins")
  except Exception as exc:
    if logger:
      logger(f"[Consent] Geolocation deny skipped: {exc}")
