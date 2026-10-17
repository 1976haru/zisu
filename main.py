import os
import sys
from mcrs.startup_probe import write_stage as _write_smoke_report


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


if __name__ == '__main__':
    smoke = bool(os.environ.get("ZISU_EXE_SMOKE_REPORT", "").strip())
    if smoke:
        _write_smoke_report("python_entrypoint")
    try:
        if smoke:
            _write_smoke_report("gui_module_import_started")
        from mcrs.gui import Studio

        if smoke:
            _write_smoke_report("gui_module_imported")
        app = Studio()
        if smoke:
            _write_smoke_report("gui_constructor_returned")
        app.mainloop()
    except Exception as exc:
        if smoke:
            _write_smoke_report("startup_failed", error_type=type(exc).__name__)
        raise
