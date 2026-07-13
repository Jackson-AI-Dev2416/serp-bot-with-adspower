import random
import time
from typing import Optional

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


def enable_mobile_touch(page: Page) -> None:
    """Enable CDP touch emulation once per browser context (AdsPower CDP has no hasTouch)."""
    context_id = id(page.context)
    if context_id in _touch_sessions:
        return
    try:
        client = page.context.new_cdp_session(page)
        client.send(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": True, "maxTouchPoints": random.choice((5, 10))},
        )
        client.send(
            "Emulation.setEmitTouchEventsForMouse",
            {"enabled": True, "configuration": "mobile"},
        )
        _touch_sessions.add(context_id)
    except Exception:
        pass


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


def dispatch_touch_tap(page: Page, x: float, y: float) -> None:
    """Fire a human-like touch tap via CDP (works on AdsPower mobile profiles)."""
    enable_mobile_touch(page)
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


def human_click(
    locator: Locator,
    delay_lo: float,
    delay_hi: float,
    *,
    page: Optional[Page] = None,
    mobile: bool = False,
) -> None:
    if mobile and page:
        try:
            human_touch_click(page, locator, delay_lo, delay_hi)
            return
        except Exception:
            pass

    random_delay(delay_lo, delay_hi)
    try:
        locator.scroll_into_view_if_needed(timeout=3000)
        locator.click(timeout=5000)
    except Exception:
        locator.click(timeout=2500, force=True)
    random_delay(delay_lo, delay_hi)
