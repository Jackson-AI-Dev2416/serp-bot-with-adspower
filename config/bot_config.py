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
  target_domains: List[str] = field(default_factory=list)
  proxies: List[Tuple[str, int, str, str]] = field(default_factory=list)
  keywords: List[str] = field(default_factory=list)
  warmup_queries: List[str] = field(default_factory=list)
  launch_interval_min: int = 1
  launch_interval_max: int = 4
  session_start_delay_min: int = 10
  session_start_delay_max: int = 30
  dwell_min: int = 60
  dwell_max: int = 120
  internal_link_min: int = 1
  internal_link_max: int = 1
  warmup_dwell_min: int = 8
  warmup_dwell_max: int = 16
  warmup_count_min: int = 1
  warmup_count_max: int = 2
  action_delay_min: float = 0.1
  action_delay_max: float = 0.3
  max_search_pages: int = 8
  max_keywords_per_profile: int = 3  # max retry attempts per profile (site-first, then keyword)
  automation_threads: int = 1
  automation_cycles: int = 1
  auto_create_profiles: bool = False
  proxy_cooldown_seconds: int = 1800
  ip_check_session_start: bool = False
  ip_check_enabled: bool = False
  skip_exhausted_pairs_in_session: bool = False
  resource_blocking_enabled: bool = True
  profile_count: int = 20
  profile_os_mode: str = "mixed"  # mixed: mobile p1-2 or windows p1 w/o mobile → android
  session_click_log_path: str = ""
  failure_rate_auto_stop_percent: int = 20  # 0 = disabled
  failure_rate_auto_stop_min_attempts: int = 20

  def get_target_domains(self) -> List[str]:
    raw = [d.strip() for d in (self.target_domains or []) if d and d.strip()]
    if not raw and (self.target_domain or "").strip():
      raw = [self.target_domain.strip()]
    seen: set[str] = set()
    ordered: List[str] = []
    for domain in raw:
      key = domain.lower().removeprefix("www.")
      if key in seen:
        continue
      seen.add(key)
      ordered.append(domain)
      if len(ordered) >= 5:
        break
    return ordered

  @property
  def primary_target_domain(self) -> str:
    domains = self.get_target_domains()
    return domains[0] if domains else (self.target_domain or "").strip()

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
