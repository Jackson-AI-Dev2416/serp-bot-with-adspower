import random
import time
from typing import Callable, Optional

from playwright.sync_api import Locator, Page


def random_delay(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _neighbor_typo(ch: str) -> str:
    keyboard_neighbors = {
        "a": "sqwz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr",
        "f": "drtgvc", "g": "ftyhbv", "h": "gyujnb", "i": "ujko", "j": "huikmn",
        "k": "jiolm", "l": "kop", "m": "njk", "n": "bhjm", "o": "iklp",
        "p": "ol", "q": "wa", "r": "edft", "s": "awedxz", "t": "rfgy",
        "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc", "y": "tghu",
        "z": "asx",
    }
    base = ch.lower()
    pool = keyboard_neighbors.get(base)
    if not pool:
        return ch
    typo = random.choice(pool)
    return typo.upper() if ch.isupper() else typo


_touch_sessions: set[int] = set()


def _apply_mobile_touch_cdp(page: Page) -> None:
    client = page.context.new_cdp_session(page)
    client.send(
        "Emulation.setTouchEmulationEnabled",
        {"enabled": True, "maxTouchPoints": random.choice((5, 10))},
    )
    client.send(
        "Emulation.setEmitTouchEventsForMouse",
        {"enabled": True, "configuration": "mobile"},
    )


def enable_mobile_touch(page: Page, timeout_seconds: float = 8.0) -> bool:
    """Enable CDP touch emulation once per browser context (AdsPower CDP has no hasTouch)."""
    del timeout_seconds  # kept for call-site compatibility; CDP must run on the Playwright thread
    context_id = id(page.context)
    if context_id in _touch_sessions:
        return True
    if page.is_closed():
        return False
    try:
        _apply_mobile_touch_cdp(page)
        _touch_sessions.add(context_id)
        return True
    except Exception:
        return False


def _pick_touch_point(box: dict) -> tuple[float, float]:
    margin_x = max(4.0, box["width"] * 0.12)
    margin_y = max(4.0, box["height"] * 0.12)
    x_lo = box["x"] + margin_x
    x_hi = box["x"] + box["width"] - margin_x
    y_lo = box["y"] + margin_y
    y_hi = box["y"] + box["height"] - margin_y
    if x_hi <= x_lo:
        x_lo, x_hi = box["x"], box["x"] + box["width"]
    if y_hi <= y_lo:
        y_lo, y_hi = box["y"], box["y"] + box["height"]
    return random.uniform(x_lo, x_hi), random.uniform(y_lo, y_hi)


def get_viewport_touch_metrics(page: Page) -> dict:
    try:
        metrics = page.evaluate(
            """() => ({
              width: window.innerWidth || 0,
              height: window.innerHeight || 0,
              devicePixelRatio: window.devicePixelRatio || 1,
            })"""
        )
        if isinstance(metrics, dict):
            return metrics
    except Exception:
        pass
    return {"width": 0, "height": 0, "devicePixelRatio": 1.0}


def pick_touch_point_from_box(box: dict, strategy: str) -> tuple[float, float]:
    bx = float(box.get("x") or 0)
    by = float(box.get("y") or 0)
    bw = max(float(box.get("width") or 0), 1.0)
    bh = max(float(box.get("height") or 0), 1.0)
    if strategy == "upper_30":
        return bx + bw * 0.5, by + bh * 0.30
    if strategy == "title_bias":
        return bx + bw * 0.35, by + bh * 0.25
    return bx + bw * 0.5, by + bh * 0.5


def _log_touch_tap_attempt(
    logger: Optional[Callable[[str], None]],
    label: str,
    attempt: int,
    strategy: str,
    x: float,
    y: float,
    metrics: dict,
    *,
    coord_mode: str = "css",
) -> None:
    if not logger:
        return
    logger(
        f"[Touch] {label} attempt={attempt}/3 strategy={strategy} "
        f"coords={coord_mode} x={x:.1f} y={y:.1f} "
        f"viewport={int(metrics.get('width') or 0)}x{int(metrics.get('height') or 0)} "
        f"dpr={float(metrics.get('devicePixelRatio') or 1.0):.2f}"
    )


def _fire_touch_at(page: Page, x: float, y: float) -> None:
    client = page.context.new_cdp_session(page)
    radius = random.uniform(9.0, 16.0)
    force = random.uniform(0.35, 0.85)
    drift_x = random.uniform(-2.5, 2.5)
    drift_y = random.uniform(-2.5, 2.5)

    def touch_point(px: float, py: float) -> dict:
        return {
            "x": round(px, 1),
            "y": round(py, 1),
            "radiusX": round(radius, 1),
            "radiusY": round(radius * random.uniform(0.85, 1.15), 1),
            "force": round(force, 3),
            "id": 0,
        }

    hold_ms = random.randint(55, 140)
    client.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [touch_point(x, y)]},
    )
    page.wait_for_timeout(hold_ms // 2)
    if abs(drift_x) > 0.5 or abs(drift_y) > 0.5:
        client.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchMove",
                "touchPoints": [touch_point(x + drift_x * 0.4, y + drift_y * 0.4)],
            },
        )
        page.wait_for_timeout(hold_ms // 2)
    client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(random.randint(40, 120))


def dispatch_touch_tap(
    page: Page,
    x: float,
    y: float,
    *,
    logger: Optional[Callable[[str], None]] = None,
    label: str = "",
) -> None:
    """Fire a human-like touch tap via CDP (works on AdsPower mobile profiles)."""
    enable_mobile_touch(page)
    if logger and label:
        metrics = get_viewport_touch_metrics(page)
        logger(
            f"[Touch] {label} tap x={x:.1f} y={y:.1f} "
            f"viewport={int(metrics.get('width') or 0)}x{int(metrics.get('height') or 0)} "
            f"dpr={float(metrics.get('devicePixelRatio') or 1.0):.2f}"
        )
    _fire_touch_at(page, x, y)


def _locator_hit_at_point(locator: Locator, x: float, y: float) -> bool:
    try:
        return bool(
            locator.evaluate(
                """(el, coords) => {
                  const [px, py] = coords;
                  const hit = document.elementFromPoint(px, py);
                  if (!hit) return false;
                  const anchor = el.closest ? (el.closest('a') || el) : el;
                  return anchor === hit || anchor.contains(hit) || hit.contains(anchor);
                }""",
                [float(x), float(y)],
            )
        )
    except Exception:
        return False


def _navigation_left_serp(page: Page) -> bool:
    try:
        url = (page.url or "").lower()
        if not url or url.startswith("about:"):
            return False
        return "google." not in url
    except Exception:
        return False


def _poll_landed_after_tap(
    page: Page,
    landed_check: Optional[Callable[[], bool]],
    *,
    max_wait_ms: int = 12000,
    poll_ms: int = 250,
) -> bool:
    if not landed_check:
        return False
    deadline = time.monotonic() + max(500, max_wait_ms) / 1000.0
    while time.monotonic() < deadline:
        try:
            if landed_check():
                return True
        except Exception:
            pass
        page.wait_for_timeout(poll_ms)
    try:
        return bool(landed_check())
    except Exception:
        return False


def _resolve_touch_outcome(
    page: Page,
    landed_check: Optional[Callable[[], bool]],
    logger: Optional[Callable[[str], None]],
    label: str,
    *,
    success_note: str,
) -> bool:
    if _poll_landed_after_tap(page, landed_check):
        if logger:
            logger(f"[Touch] {label} success {success_note}")
        return True
    if _navigation_left_serp(page):
        if logger:
            logger(
                f"[Touch] {label} left Google SERP but target not confirmed "
                f"(url={(page.url or '')[:100]})"
            )
    return False


def dispatch_serp_anchor_touch_tap(
    page: Page,
    locator: Locator,
    *,
    logger: Optional[Callable[[str], None]] = None,
    label: str = "serp-target",
    landed_check: Optional[Callable[[], bool]] = None,
    delay_lo: float = 0.3,
    delay_hi: float = 0.5,
) -> bool:
    """CDP touch tap on a SERP result anchor with scroll, bbox refresh, and retries."""
    enable_mobile_touch(page, timeout_seconds=10.0)
    random_delay(delay_lo, delay_hi)
    try:
        locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(random.randint(250, 520))
    scroll_page(page, random.randint(90, 220), mobile=True)
    page.wait_for_timeout(random.randint(150, 320))
    try:
        locator.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    page.wait_for_timeout(random.randint(180, 380))

    metrics = get_viewport_touch_metrics(page)
    dpr = float(metrics.get("devicePixelRatio") or 1.0)
    strategies = ("center", "upper_30", "title_bias")
    bbox_timeout_ms = 2000

    for attempt, strategy in enumerate(strategies, start=1):
        if landed_check and landed_check():
            if logger:
                logger(f"[Touch] {label} success (already on target before tap)")
            return True
        if _navigation_left_serp(page):
            return _resolve_touch_outcome(
                page, landed_check, logger, label, success_note="(navigation already in progress)",
            )

        try:
            box = locator.bounding_box(timeout=bbox_timeout_ms)
        except Exception:
            if landed_check and landed_check():
                return _resolve_touch_outcome(
                    page, landed_check, logger, label, success_note="after bbox timeout",
                )
            if _navigation_left_serp(page):
                return _resolve_touch_outcome(
                    page, landed_check, logger, label, success_note="after navigation started",
                )
            if logger:
                logger(
                    f"[Touch] {label} attempt={attempt}/3 strategy={strategy} "
                    f"— bounding box unavailable"
                )
            continue

        if not box or box.get("width", 0) < 2 or box.get("height", 0) < 2:
            if logger:
                logger(f"[Touch] {label} attempt={attempt}/3 strategy={strategy} — no bounding box")
            continue

        css_x, css_y = pick_touch_point_from_box(box, strategy)
        coord_modes: list[tuple[str, float, float]] = [("css", css_x, css_y)]
        if dpr > 1.01:
            coord_modes.append(("dpr", css_x * dpr, css_y * dpr))

        for coord_mode, tap_x, tap_y in coord_modes:
            if landed_check and landed_check():
                return _resolve_touch_outcome(
                    page, landed_check, logger, label, success_note="before tap dispatch",
                )
            if _navigation_left_serp(page):
                return _resolve_touch_outcome(
                    page, landed_check, logger, label, success_note="before tap dispatch",
                )

            _log_touch_tap_attempt(
                logger, label, attempt, strategy, tap_x, tap_y, metrics, coord_mode=coord_mode,
            )
            if coord_mode == "css" and not _locator_hit_at_point(locator, tap_x, tap_y):
                if logger:
                    logger(
                        f"[Touch] {label} attempt={attempt}/3 strategy={strategy} "
                        f"— elementFromPoint miss at css ({tap_x:.1f},{tap_y:.1f})"
                    )
            try:
                dispatch_touch_tap(page, tap_x, tap_y)
            except Exception as exc:
                if logger:
                    logger(f"[Touch] {label} dispatch failed: {exc}")
                if landed_check and landed_check():
                    return _resolve_touch_outcome(
                        page, landed_check, logger, label, success_note="after dispatch error",
                    )
                if _navigation_left_serp(page):
                    return _resolve_touch_outcome(
                        page, landed_check, logger, label, success_note="after dispatch error",
                    )
                continue

            if _resolve_touch_outcome(
                page,
                landed_check,
                logger,
                label,
                success_note=f"via {strategy}/{coord_mode} ({tap_x:.1f},{tap_y:.1f})",
            ):
                return True

    if landed_check and landed_check():
        return _resolve_touch_outcome(
            page, landed_check, logger, label, success_note="after all tap attempts",
        )
    return False


def human_touch_click(
    page: Page,
    locator: Locator,
    delay_lo: float,
    delay_hi: float,
) -> None:
    random_delay(delay_lo, delay_hi)
    locator.scroll_into_view_if_needed(timeout=4000)
    page.wait_for_timeout(random.randint(80, 220))
    box = locator.bounding_box()
    if not box or box.get("width", 0) < 2 or box.get("height", 0) < 2:
        locator.click(timeout=5000)
        random_delay(delay_lo, delay_hi)
        return

    x, y = _pick_touch_point(box)
    try:
        dispatch_touch_tap(page, x, y)
    except Exception:
        page.mouse.click(x, y, delay=random.randint(40, 110))
    random_delay(delay_lo, delay_hi)


def _clear_serp_search_input(locator: Locator, page: Page, *, mobile: bool) -> None:
    """Clear Google SERP search box; Android needs JS/value reset (Ctrl+A does not work)."""
    page.wait_for_timeout(random.randint(120, 280))
    if mobile:
        try:
            locator.evaluate(
                """el => {
                    if (!el) return;
                    el.focus();
                    if (typeof el.select === 'function') {
                        el.select();
                    }
                    el.value = '';
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }"""
            )
            page.wait_for_timeout(random.randint(80, 180))
        except Exception:
            pass
        try:
            locator.fill("")
        except Exception:
            pass
        try:
            remaining = (locator.input_value(timeout=2000) or "").strip()
            if remaining:
                locator.evaluate(
                    """el => {
                        if (!el) return;
                        el.focus();
                        if (typeof el.select === 'function') {
                            el.select();
                        }
                        el.value = '';
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }"""
                )
                page.wait_for_timeout(random.randint(80, 160))
                try:
                    locator.fill("")
                except Exception:
                    pass
        except Exception:
            pass
        return

    try:
        locator.focus()
    except Exception:
        pass
    try:
        locator.fill("")
    except Exception:
        try:
            locator.press("Control+a")
            locator.press("Backspace")
        except Exception:
            pass


def human_type_focus_safe(
    locator: Locator,
    text: str,
    lo: float,
    hi: float,
    *,
    page: Page,
    mobile: bool = False,
    typo_chance: float = 0.03,
    min_length_for_typo: int = 9,
) -> None:
    """Type into SERP search box without requiring OS window focus (desktop: locator API)."""
    type_lo = max(0.06, float(lo))
    type_hi = max(type_lo + 0.04, float(hi))
    if mobile:
        try:
            human_touch_click(page, locator, type_lo, type_hi)
        except Exception:
            try:
                locator.focus()
            except Exception:
                pass
        _clear_serp_search_input(locator, page, mobile=True)
        human_type(
            locator,
            text,
            type_lo,
            type_hi,
            typo_chance=typo_chance,
            min_length_for_typo=min_length_for_typo,
            page=page,
            mobile=True,
            skip_clear=True,
        )
        return

    type_lo = max(0.06, float(lo))
    type_hi = max(type_lo + 0.04, float(hi))
    try:
        locator.focus()
    except Exception:
        pass
    page.wait_for_timeout(random.randint(120, 280))
    try:
        locator.fill("")
    except Exception:
        try:
            locator.press("Control+a")
            locator.press("Backspace")
        except Exception:
            pass
    made_typo = False
    for ch in text:
        can_typo = (
            not made_typo
            and len(text) >= min_length_for_typo
            and ch.isalpha()
            and random.random() < typo_chance
        )
        if can_typo:
            wrong = _neighbor_typo(ch)
            if wrong != ch:
                delay_wrong = random.uniform(type_lo, type_hi) * 1000
                locator.type(wrong, delay=delay_wrong)
                random_delay(max(0.05, type_lo * 0.6), max(0.12, type_hi * 0.8))
                locator.press("Backspace")
                random_delay(max(0.05, type_lo * 0.6), max(0.15, type_hi))
                made_typo = True
        delay_ch = random.uniform(type_lo, type_hi) * 1000
        locator.type(ch, delay=delay_ch)


def human_type(
    locator: Locator,
    text: str,
    lo: float,
    hi: float,
    typo_chance: float = 0.06,
    min_length_for_typo: int = 6,
    *,
    page: Optional[Page] = None,
    mobile: bool = False,
    skip_clear: bool = False,
) -> None:
    type_lo = max(0.06, float(lo))
    type_hi = max(type_lo + 0.04, float(hi))
    try:
        if mobile and page:
            human_touch_click(page, locator, type_lo, type_hi)
        else:
            locator.click(timeout=3500)
    except Exception:
        try:
            locator.focus()
        except Exception:
            pass
    if not skip_clear:
        if mobile and page:
            _clear_serp_search_input(locator, page, mobile=True)
        else:
            page.wait_for_timeout(random.randint(120, 280)) if page else time.sleep(random.uniform(0.12, 0.28))
            try:
                locator.fill("")
            except Exception:
                if page:
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
    made_typo = False
    keyboard = page.keyboard if page else None
    for ch in text:
        can_typo = (
            not made_typo
            and len(text) >= min_length_for_typo
            and ch.isalpha()
            and random.random() < typo_chance
        )
        if can_typo:
            wrong = _neighbor_typo(ch)
            if wrong != ch:
                delay_wrong = random.uniform(type_lo, type_hi) * 1000
                if keyboard:
                    keyboard.type(wrong, delay=delay_wrong)
                else:
                    locator.type(wrong, delay=delay_wrong)
                random_delay(max(0.05, type_lo * 0.6), max(0.12, type_hi * 0.8))
                if keyboard:
                    keyboard.press("Backspace")
                else:
                    locator.press("Backspace")
                random_delay(max(0.05, type_lo * 0.6), max(0.15, type_hi))
                made_typo = True
        delay_ch = random.uniform(type_lo, type_hi) * 1000
        if keyboard:
            keyboard.type(ch, delay=delay_ch)
        else:
            locator.type(ch, delay=delay_ch)


def human_keyboard_type(
    page: Page,
    text: str,
    lo: float,
    hi: float,
    typo_chance: float = 0.03,
    min_length_for_typo: int = 9,
) -> None:
    """Type into the focused field (e.g. browser omnibox) via page.keyboard."""
    type_lo = max(0.06, float(lo))
    type_hi = max(type_lo + 0.04, float(hi))
    keyboard = page.keyboard
    page.wait_for_timeout(random.randint(120, 280))
    keyboard.press("Control+a")
    keyboard.press("Backspace")
    made_typo = False
    for ch in text:
        can_typo = (
            not made_typo
            and len(text) >= min_length_for_typo
            and ch.isalpha()
            and random.random() < typo_chance
        )
        if can_typo:
            wrong = _neighbor_typo(ch)
            if wrong != ch:
                delay_wrong = random.uniform(type_lo, type_hi) * 1000
                keyboard.type(wrong, delay=delay_wrong)
                random_delay(max(0.05, type_lo * 0.6), max(0.12, type_hi * 0.8))
                keyboard.press("Backspace")
                random_delay(max(0.05, type_lo * 0.6), max(0.15, type_hi))
                made_typo = True
        delay_ch = random.uniform(type_lo, type_hi) * 1000
        keyboard.type(ch, delay=delay_ch)


def micro_scroll(
    page: Page,
    times: int = 1,
    delay_lo: float = 0.4,
    delay_hi: float = 1.2,
    *,
    mobile: bool = False,
) -> None:
    for _ in range(times):
        scroll_page(page, random.randint(120, 420), mobile=mobile)
        random_delay(delay_lo, delay_hi)


def scroll_page(page: Page, delta_y: int, *, mobile: bool = False) -> None:
    try:
        if page.is_closed():
            return
        if mobile:
            # Flick-style scroll: short burst with slight horizontal jitter (like a thumb).
            flick = int(delta_y)
            jitter_x = random.randint(-6, 6)
            page.evaluate(
                """([y, jx]) => {
                  window.scrollBy({ top: Number(y) || 0, left: Number(jx) || 0, behavior: 'auto' });
                }""",
                [flick, jitter_x],
            )
            page.wait_for_timeout(random.randint(70, 200))
        else:
            page.mouse.wheel(0, int(delta_y))
    except Exception:
        pass


def human_click(
    locator: Locator,
    delay_lo: float,
    delay_hi: float,
    *,
    page: Optional[Page] = None,
    mobile: bool = False,
    modifiers: Optional[list[str]] = None,
) -> None:
    if mobile and page:
        try:
            human_touch_click(page, locator, delay_lo, delay_hi)
            return
        except Exception:
            try:
                random_delay(delay_lo, delay_hi)
                locator.scroll_into_view_if_needed(timeout=3000)
                locator.click(timeout=5000, force=True)
                random_delay(delay_lo, delay_hi)
                return
            except Exception:
                pass

    random_delay(delay_lo, delay_hi)
    click_mods = list(modifiers or [])
    try:
        locator.scroll_into_view_if_needed(timeout=3000)
        locator.click(timeout=5000, modifiers=click_mods)
    except Exception:
        locator.click(timeout=2500, force=True, modifiers=click_mods)
    random_delay(delay_lo, delay_hi)
