from enum import Enum


class UiStatusKey(str, Enum):
  CREATING_PROFILE = "creating_profile"
  LAUNCHING = "launching"
  CHECKING_IP = "checking_ip"
  WARMING_UP = "warming_up"
  SEARCHING = "searching"
  VISITING_SITE = "visiting_site"
  CAPTCHA = "captcha"
  CAPTCHA_MANUAL = "captcha_manual"
  ERROR = "error"
  SELF_HEALING = "self_healing"
  CLOSED = "closed"


UI_STATUS_LABELS: dict[UiStatusKey, str] = {
  UiStatusKey.CREATING_PROFILE: "Creating Profile",
  UiStatusKey.LAUNCHING: "Launching",
  UiStatusKey.CHECKING_IP: "Checking IP",
  UiStatusKey.WARMING_UP: "Warming Up",
  UiStatusKey.SEARCHING: "Searching Keyword",
  UiStatusKey.VISITING_SITE: "Visiting Site",
  UiStatusKey.CAPTCHA: "Solving Captcha",
  UiStatusKey.CAPTCHA_MANUAL: "Captcha (Manual)",
  UiStatusKey.ERROR: "Error",
  UiStatusKey.SELF_HEALING: "Self-Healing",
  UiStatusKey.CLOSED: "Offline",
}


SESSION_ELAPSED_KEYS = frozenset({
  UiStatusKey.CREATING_PROFILE.value,
  UiStatusKey.LAUNCHING.value,
  UiStatusKey.CHECKING_IP.value,
  UiStatusKey.WARMING_UP.value,
  UiStatusKey.SEARCHING.value,
  UiStatusKey.VISITING_SITE.value,
  UiStatusKey.SELF_HEALING.value,
})

CAPTCHA_ELAPSED_KEYS = frozenset({
  UiStatusKey.CAPTCHA.value,
  UiStatusKey.CAPTCHA_MANUAL.value,
})

ERROR_ELAPSED_KEYS = frozenset({
  UiStatusKey.ERROR.value,
})

ELAPSED_STATUS_KEYS = SESSION_ELAPSED_KEYS | CAPTCHA_ELAPSED_KEYS | ERROR_ELAPSED_KEYS


def ui_label(key: UiStatusKey) -> str:
  return UI_STATUS_LABELS[key]


def short_error_detail(
  exc: BaseException | str,
  context: str = "",
  max_len: int = 48,
) -> str:
  message = str(exc).strip() or context or "Unknown error"
  message = " ".join(message.split())
  if context and context not in message:
    message = f"{context}: {message}"
  if len(message) > max_len:
    return message[: max_len - 1] + "…"
  return message


class ProfileStatus(str, Enum):
  IDLE = "idle"
  CREATING_PROFILE = "creating_profile"
  LAUNCHING = "launching"
  CHECKING_IP = "checking_ip"
  RUNNING = "running"
  WARMING_UP = "warming_up"
  SEARCHING = "searching"
  VISITING_SITE = "visiting_site"
  SUCCESS = "success"
  CAPTCHA_WAIT = "captcha_wait"
  CAPTCHA_MANUAL = "captcha_manual"
  ERROR = "error"
  BLOCKED = "blocked"
  COOLDOWN = "cooldown"
  STOPPED = "stopped"
  SELF_HEALING = "self_healing"

  def to_ui(self, cooldown_seconds: int = 0, detail: str = "") -> tuple[str, str]:
    if self == ProfileStatus.CREATING_PROFILE:
      return self._pair(UiStatusKey.CREATING_PROFILE)
    if self in (ProfileStatus.LAUNCHING, ProfileStatus.RUNNING):
      return self._pair(UiStatusKey.LAUNCHING)
    if self == ProfileStatus.CHECKING_IP:
      return self._pair(UiStatusKey.CHECKING_IP)
    if self == ProfileStatus.WARMING_UP:
      return self._pair(UiStatusKey.WARMING_UP)
    if self == ProfileStatus.SEARCHING:
      return self._pair(UiStatusKey.SEARCHING)
    if self in (ProfileStatus.VISITING_SITE, ProfileStatus.SUCCESS):
      return self._pair(UiStatusKey.VISITING_SITE)
    if self == ProfileStatus.CAPTCHA_WAIT:
      return self._pair(UiStatusKey.CAPTCHA)
    if self == ProfileStatus.CAPTCHA_MANUAL:
      return self._pair(UiStatusKey.CAPTCHA_MANUAL)
    if self == ProfileStatus.SELF_HEALING:
      return self._pair(UiStatusKey.SELF_HEALING)
    if self in (ProfileStatus.ERROR, ProfileStatus.BLOCKED):
      label = ui_label(UiStatusKey.ERROR)
      if detail:
        label = f"{label}: {detail}"
      return UiStatusKey.ERROR.value, label
    if self == ProfileStatus.COOLDOWN and cooldown_seconds > 0:
      minutes, seconds = divmod(cooldown_seconds, 60)
      text = f"{ui_label(UiStatusKey.CLOSED)} [{minutes:02d}:{seconds:02d}]"
      return UiStatusKey.CLOSED.value, text
    return self._pair(UiStatusKey.CLOSED)

  @staticmethod
  def _pair(key: UiStatusKey) -> tuple[str, str]:
    label = ui_label(key)
    return key.value, label
