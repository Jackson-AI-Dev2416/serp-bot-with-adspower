import random
import time
from typing import Callable, Optional

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

ACCEPT_TEXTS = (
  "모두 수락",
  "Accept all",
  "Alle akzeptieren",
  "Tout accepter",
  "Aceptar todo",
  "Accetta tutto",
  "동의함",
  "I agree",
)
EXPAND_TEXTS = (
  "더보기",
  "Read more",
  "Show more",
  "Más información",
  "Mehr anzeigen",
)
CONSENT_DOMAINS = (".google.com", ".google.co.kr")


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
          'before you continue',
          'we use cookies and data to',
          '쿠키 및 데이터를 사용하는 방식',
        ];
        if (phrases.some((phrase) => text.includes(phrase))) return true;

        const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
        const labels = buttons.map((btn) => (btn.innerText || btn.textContent || '').trim().toLowerCase());
        const acceptHints = ['모두 수락', 'accept all', 'alle akzeptieren', 'tout accepter'];
        const expandHints = ['더보기', 'read more', 'show more'];
        if (labels.some((label) => acceptHints.some((hint) => label.includes(hint)))) return true;
        if (labels.some((label) => expandHints.some((hint) => label.includes(hint)))
            && text.includes('google')) {
          return true;
        }
        return false;
      }"""
    )
  except Exception:
    return False


def dismiss_google_consent(page: Page, logger: Callable[[str], None]) -> bool:
  if not is_google_consent_present(page):
    return False

  logger("[Consent] Google cookie/consent dialog detected — accepting")

  for label in EXPAND_TEXTS:
    locator = page.get_by_role("button", name=label, exact=False)
    try:
      if locator.count() > 0 and locator.first.is_visible(timeout=800):
        locator.first.click(timeout=3000)
        page.wait_for_timeout(600)
        break
    except Exception:
      pass

  for label in ACCEPT_TEXTS:
    locator = page.get_by_role("button", name=label, exact=False)
    try:
      if locator.count() > 0 and locator.first.is_visible(timeout=1200):
        try:
          with page.expect_navigation(timeout=20000, wait_until="domcontentloaded"):
            locator.first.click(timeout=5000)
        except PlaywrightTimeoutError:
          locator.first.click(timeout=5000)
          page.wait_for_timeout(1200)
        if not is_google_consent_present(page):
          logger("[Consent] Google consent accepted")
          return True
    except Exception:
      continue

  css_selectors = (
    "#L2AGLb",
    'button[aria-label*="Accept"]',
    'button[aria-label*="accept"]',
    'button[aria-label*="수락"]',
    'form[action*="consent"] button',
    'button[jsname="b3VHJd"]',
    'button.VfPpkd-LgbsSe',
    'div[role="dialog"] button',
    'c-wiz button',
  )
  for selector in css_selectors:
    locator = page.locator(selector).first
    try:
      if locator.count() <= 0 or not locator.is_visible(timeout=800):
        continue
      try:
        with page.expect_navigation(timeout=20000, wait_until="domcontentloaded"):
          locator.click(timeout=5000)
      except PlaywrightTimeoutError:
        locator.click(timeout=5000)
        page.wait_for_timeout(1200)
      if not is_google_consent_present(page):
        logger(f"[Consent] Google consent accepted via {selector}")
        return True
    except Exception:
      continue

  clicked = page.evaluate(
    """() => {
      const acceptHints = ['모두 수락', 'accept all', 'alle akzeptieren', 'tout accepter'];
      const expandHints = ['더보기', 'read more', 'show more'];
      const candidates = Array.from(document.querySelectorAll('button, [role="button"], div[role="button"]'));
      for (const hint of expandHints) {
        const expand = candidates.find((el) => (el.innerText || '').toLowerCase().includes(hint));
        if (expand) {
          expand.click();
          break;
        }
      }
      const refreshed = Array.from(document.querySelectorAll('button, [role="button"], div[role="button"]'));
      for (const hint of acceptHints) {
        const accept = refreshed.find((el) => (el.innerText || '').toLowerCase().includes(hint));
        if (accept) {
          accept.click();
          return true;
        }
      }
      return false;
    }"""
  )
  if clicked:
    page.wait_for_timeout(1500)
    if not is_google_consent_present(page):
      logger("[Consent] Google consent accepted via JS fallback")
      return True

  deadline = time.time() + 3.0
  while time.time() < deadline:
    if not is_google_consent_present(page):
      logger("[Consent] Google consent cleared")
      return True
    page.wait_for_timeout(500)

  logger("[Consent] Could not auto-accept Google consent dialog")
  return False
