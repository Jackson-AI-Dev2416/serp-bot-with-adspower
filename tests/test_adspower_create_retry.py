from core.worker import BotWorkerThread


def test_profile_create_retry_delay_connection_refused():
  delay = BotWorkerThread._profile_create_retry_delay(
    1,
    "Cannot connect to AdsPower Local API (WinError 10061)",
    10.0,
  )
  assert delay == 5.0
  delay2 = BotWorkerThread._profile_create_retry_delay(
    3,
    "connection refused",
    10.0,
  )
  assert delay2 == 20.0


def test_profile_create_retry_delay_timeout():
  delay = BotWorkerThread._profile_create_retry_delay(
    1,
    "AdsPower API timed out after 25s",
    10.0,
  )
  assert delay == 15.0
  delay2 = BotWorkerThread._profile_create_retry_delay(
    4,
    "timeout",
    10.0,
  )
  assert delay2 == 30.0


def test_profile_create_retry_delay_generic_uses_launch_interval():
  delay = BotWorkerThread._profile_create_retry_delay(1, "duplicate profile name", 12.0)
  assert delay == 12.0
  delay2 = BotWorkerThread._profile_create_retry_delay(3, "api error", 12.0)
  assert delay2 == 16.0
