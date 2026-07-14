from dataclasses import dataclass, field
from typing import List, Tuple
from urllib.parse import urlparse


@dataclass
class BotConfig:
  capsolver_api_key: str
  adspower_api_url: str = "http://local.adspower.com:50325"
  adspower_api_key: str = ""
  adspower_group_id: str = "0"
  target_domain: str = ""
  proxies: List[Tuple[str, int, str, str]] = field(default_factory=list)
  keywords: List[str] = field(default_factory=list)
  warmup_queries: List[str] = field(default_factory=list)
  launch_interval_min: int = 1
  launch_interval_max: int = 4
  dwell_min: int = 60
  dwell_max: int = 120
  warmup_dwell_min: int = 8
  warmup_dwell_max: int = 16
  warmup_count_min: int = 1
  warmup_count_max: int = 2
  action_delay_min: float = 0.1
  action_delay_max: float = 0.3
  max_search_pages: int = 8
  max_keywords_per_profile: int = 3
  automation_threads: int = 1
  automation_cycles: int = 1
  auto_create_profiles: bool = False
  proxy_cooldown_seconds: int = 1800
  ip_check_session_start: bool = False
  ip_check_enabled: bool = False
  profile_count: int = 20
  session_click_log_path: str = ""
  cursor_api_key: str = ""
  cursor_model: str = "composer-2.5"
  llm_api_key: str = ""
  llm_base_url: str = "https://api.openai.com/v1"
  llm_model: str = "gpt-4o-mini"

  @property
  def adspower_url(self) -> str:
    raw = (self.adspower_api_url or "http://127.0.0.1:50325").strip().rstrip("/")
    if "://" not in raw:
      raw = f"http://{raw}"
    parsed = urlparse(raw)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 50325
    return f"{scheme}://{host}:{port}"
