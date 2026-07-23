"""Desktop SERP page visit order based on historical rank position."""

from typing import Sequence


def pending_desktop_scan_pages(
  search_order: Sequence[int],
  visited: set[int],
  effective_cap: int,
) -> list[int]:
  """Pages still to scan in search_order within Google's visible cap."""
  cap = max(1, int(effective_cap))
  return [p for p in search_order if p not in visited and p <= cap]


def build_desktop_page_order(
  y: int | None,
  z: int,
  x: int,
) -> list[int]:
  """
  Pages to visit after page 1 (which is always scanned on initial search).

  z: current Google SERP last page visible
  y: historical page from result.csv (None if unknown)
  x: settings max_search_pages

  z > y: [y, y-1, y+1, y-2, y-3, ..., 2, y+2, ...] capped at min(x, z), all >= 2
  z < y: [z, z-1, z-2, ..., 2]
  z == y: same as z > y
  """
  cap = min(max(1, int(x)), max(1, int(z)))
  if cap <= 1:
    return []

  if y is None or y < 2:
    return list(range(2, cap + 1))

  y = int(y)
  z = max(1, int(z))
  result: list[int] = []
  seen: set[int] = set()

  def add(page_num: int) -> None:
    if 2 <= page_num <= cap and page_num not in seen:
      seen.add(page_num)
      result.append(page_num)

  if z < y:
    for page_num in range(cap, 1, -1):
      add(page_num)
    return result

  add(y)
  offset = 1
  while True:
    progressed = False
    below = y - offset
    if below >= 2:
      add(below)
      progressed = True
    above = y + offset
    if above <= cap:
      add(above)
      progressed = True
    if not progressed:
      break
    offset += 1

  return result
