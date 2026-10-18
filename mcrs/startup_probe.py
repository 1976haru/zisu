"""Small, identity-free stage reporter for offline frozen-app smoke tests."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import traceback

_STARTED = time.perf_counter()
_EVENTS = []
_REPORT_TARGET = None


def safe_exception_details(exc: BaseException) -> dict:
    """Return exception type and code location without message or full paths."""
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    details = {"error_type": type(exc).__name__[:80]}
    if frames:
        frame = frames[-1]
        details["error_location"] = {
            "module": Path(frame.filename).stem[:80],
            "function": frame.name[:80],
            "line": int(frame.lineno),
        }
    return details


def write_stage(stage: str, **details) -> None:
    """Atomically persist a smoke stage only when an explicit report is requested."""
    global _REPORT_TARGET
    report = os.environ.get("ZISU_EXE_SMOKE_REPORT", "").strip()
    if not report:
        return
    target = Path(report)
    target_key = str(target.resolve())
    if target_key != _REPORT_TARGET:
        _REPORT_TARGET = target_key
        _EVENTS.clear()
    target.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "stage": stage,
        "elapsed_ms": round((time.perf_counter() - _STARTED) * 1000, 1),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    try:
        origin = int(os.environ["ZISU_EXE_SMOKE_START_COUNTER"])
        frequency = int(os.environ["ZISU_EXE_SMOKE_COUNTER_FREQUENCY"])
        if frequency > 0:
            origin_ns = origin * 1_000_000_000 // frequency
            event["launch_elapsed_ms"] = round(
                (time.perf_counter_ns() - origin_ns) / 1_000_000, 1
            )
    except (KeyError, ValueError):
        pass
    _EVENTS.append(event)
    payload = {
        "schema": 2,
        "stage": stage,
        "elapsed_ms": event["elapsed_ms"],
        **({"launch_elapsed_ms": event["launch_elapsed_ms"]} if "launch_elapsed_ms" in event else {}),
        "events": list(_EVENTS),
        **details,
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(target)
