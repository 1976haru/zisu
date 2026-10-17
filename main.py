import os
import sys
import json
from pathlib import Path


def _apply_offline_smoke_arguments(arguments):
    """Translate explicit CI smoke arguments before any application imports."""
    mapping = {
        "--offline-smoke-report": "ZISU_EXE_SMOKE_REPORT",
        "--data-dir": "ZISU_DATA_DIR",
    }
    for flag, environment_name in mapping.items():
        if flag not in arguments:
            continue
        position = arguments.index(flag)
        if position + 1 >= len(arguments) or arguments[position + 1].startswith("--"):
            raise SystemExit(f"Missing value for {flag}")
        os.environ[environment_name] = arguments[position + 1]


_apply_offline_smoke_arguments(sys.argv[1:])


def _write_smoke_report(stage, error_type=None):
    report = os.environ.get("ZISU_EXE_SMOKE_REPORT", "").strip()
    if not report:
        return
    target = Path(report)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "stage": stage}
    if error_type:
        payload["error_type"] = error_type
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


if __name__ == '__main__':
    smoke = bool(os.environ.get("ZISU_EXE_SMOKE_REPORT", "").strip())
    if smoke:
        _write_smoke_report("bootstrap_started")
    try:
        from mcrs.gui import Studio

        app = Studio()
        if smoke:
            _write_smoke_report("gui_initialized")
        app.mainloop()
    except Exception as exc:
        if smoke:
            _write_smoke_report("startup_failed", type(exc).__name__)
        raise
