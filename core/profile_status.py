from enum import Enum


class UiStatusKey(str, Enum):
  NORMAL = "Active"
  CAPTCHA = "Captcha Solving"
  CAPTCHA_MANUAL = "CAPTCHA (Manual)"
  ERROR = "Error"
  SELF_HEALING = "Self-Healing"
  CLOSED = "Offline"


class ProfileStatus(str, Enum):
  IDLE = "idle"
  RUNNING = "running"
  WARMING_UP = "warming_up"
  SEARCHING = "searching"
  SUCCESS = "success"
  CAPTCHA_WAIT = "captcha_wait"
  CAPTCHA_MANUAL = "captcha_manual"
  ERROR = "error"
  BLOCKED = "blocked"
  COOLDOWN = "cooldown"
  STOPPED = "stopped"
  SELF_HEALING = "self_healing"

  def to_ui(self, cooldown_seconds: int = 0) -> tuple[str, str]:
    if self in (ProfileStatus.RUNNING, ProfileStatus.WARMING_UP, ProfileStatus.SEARCHING, ProfileStatus.SUCCESS):
      return UiStatusKey.NORMAL.value, UiStatusKey.NORMAL.value
    if self == ProfileStatus.CAPTCHA_WAIT:
      return UiStatusKey.CAPTCHA.value, UiStatusKey.CAPTCHA.value
    if self == ProfileStatus.CAPTCHA_MANUAL:
      return UiStatusKey.CAPTCHA_MANUAL.value, UiStatusKey.CAPTCHA_MANUAL.value
    if self == ProfileStatus.SELF_HEALING:
      return UiStatusKey.SELF_HEALING.value, UiStatusKey.SELF_HEALING.value
    if self in (ProfileStatus.ERROR, ProfileStatus.BLOCKED):
      return UiStatusKey.ERROR.value, UiStatusKey.ERROR.value
    if self == ProfileStatus.COOLDOWN and cooldown_seconds > 0:
      minutes, seconds = divmod(cooldown_seconds, 60)
      text = f"{UiStatusKey.CLOSED.value} [{minutes:02d}:{seconds:02d}]"
      return UiStatusKey.CLOSED.value, text
    return UiStatusKey.CLOSED.value, UiStatusKey.CLOSED.value
