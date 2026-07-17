"""
Shared Google Sheets API helpers: retry on rate-limit (429) and transient 5xx.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, TypeVar

from googleapiclient.errors import HttpError

T = TypeVar("T")


def execute_with_retry(
    request: Any,
    *,
    max_attempts: int = 6,
    label: str = "sheets",
) -> Any:
    """
    Execute a googleapiclient request with exponential backoff on 429 / 503.
    Default Sheets per-user read quota is 60/min — brief waits recover quickly.
    """
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()
        except HttpError as exc:
            last_exc = exc
            status = int(getattr(exc.resp, "status", 0) or 0)
            if status not in (429, 500, 503) or attempt >= max_attempts:
                raise
            sleep_for = delay + random.uniform(0, 0.4)
            print(
                f"  [SHEETS] {label}: HTTP {status}; "
                f"retry {attempt}/{max_attempts} in {sleep_for:.1f}s",
                flush=True,
            )
            time.sleep(sleep_for)
            delay = min(delay * 2, 30.0)
        except Exception as exc:
            last_exc = exc
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{label}: execute_with_retry failed with no exception")


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 6,
    label: str = "sheets",
) -> T:
    """Same backoff for callables that already invoke .execute() internally."""
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except HttpError as exc:
            last_exc = exc
            status = int(getattr(exc.resp, "status", 0) or 0)
            if status not in (429, 500, 503) or attempt >= max_attempts:
                raise
            sleep_for = delay + random.uniform(0, 0.4)
            print(
                f"  [SHEETS] {label}: HTTP {status}; "
                f"retry {attempt}/{max_attempts} in {sleep_for:.1f}s",
                flush=True,
            )
            time.sleep(sleep_for)
            delay = min(delay * 2, 30.0)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{label}: call_with_retry failed with no exception")
