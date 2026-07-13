import time
from dataclasses import dataclass
from typing import Dict


@dataclass
class ProxyState:
  running: bool = False
  cooldown_until: float = 0.0


class ProxyScheduler:
  """Enforces per-IP exclusivity and post-close cooldown windows."""

  def __init__(self, cooldown_seconds: int):
    self.cooldown_seconds = cooldown_seconds
    self._states: Dict[str, ProxyState] = {}

  def _state(self, proxy_key: str) -> ProxyState:
    if proxy_key not in self._states:
      self._states[proxy_key] = ProxyState()
    return self._states[proxy_key]

  def can_run(self, proxy_key: str) -> bool:
    state = self._state(proxy_key)
    return (not state.running) and (time.time() >= state.cooldown_until)

  def mark_running(self, proxy_key: str) -> None:
    self._state(proxy_key).running = True

  def mark_finished(self, proxy_key: str) -> None:
    state = self._state(proxy_key)
    state.running = False
    state.cooldown_until = time.time() + self.cooldown_seconds

  def seconds_until_next_slot(self) -> float:
    now = time.time()
    waits = [
      max(0.0, state.cooldown_until - now)
      for state in self._states.values()
      if state.running or state.cooldown_until > now
    ]
    return min(waits) if waits else 5.0

  def seconds_until_proxy(self, proxy_key: str) -> float:
    state = self._state(proxy_key)
    now = time.time()
    if state.running:
      return max(0.0, state.cooldown_until - now) or 9999.0
    return max(0.0, state.cooldown_until - now)
