"""Process-wide bounded admission slots for external research work."""
from __future__ import annotations

from contextlib import contextmanager
import threading


class WorkSlotBusy(RuntimeError):
    pass


SEARCH_SLOT = threading.BoundedSemaphore(1)
AI_REQUEST_SLOT = threading.BoundedSemaphore(1)


@contextmanager
def occupy(slot: threading.BoundedSemaphore, *, timeout: float = 30.0):
    """Wait briefly off the UI thread; never spin or exceed one active request."""
    if not slot.acquire(timeout=max(0.0, float(timeout))):
        raise WorkSlotBusy("Another request is still active.")
    try:
        yield
    finally:
        slot.release()
