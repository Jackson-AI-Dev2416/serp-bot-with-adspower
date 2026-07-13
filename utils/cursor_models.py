"""Cursor API model names (not OpenAI model IDs)."""

DEFAULT_CURSOR_MODEL = "gpt-5.3-codex"

# Legacy OpenAI / invalid names saved from older Self-Healer settings.
_LEGACY_INVALID_MODELS = frozenset(
  {
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4",
    "gpt-3.5-turbo",
    "gpt-4-turbo",
    "o1-mini",
    "o1",
  }
)

# First-party Composer models consume the Auto/Composer quota — listed last.
CURSOR_MODEL_CHOICES = (
  "gpt-5.3-codex",
  "claude-sonnet-5",
  "gpt-5.5",
  "claude-opus-4-8",
  "grok-4.5",
  "default",
  "composer-2.5",
  "composer-2",
)


def normalize_cursor_model(model: str) -> str:
  name = (model or "").strip()
  if not name or name.lower() in _LEGACY_INVALID_MODELS:
    return DEFAULT_CURSOR_MODEL
  return name
