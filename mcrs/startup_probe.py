"""Small, identity-free stage reporter for offline frozen-app smoke tests."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

_STARTED = time.perf_counter()
_EVENTS = []
_REPORT_TARGET = None


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
    _EVENTS.append(event)
    payload = {
        "schema": 2,
        "stage": stage,
        "elapsed_ms": event["elapsed_ms"],
        "events": list(_EVENTS),
        **details,
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(target)
