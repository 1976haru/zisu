import os
import sys


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

from mcrs.gui import main  # noqa: E402


if __name__ == '__main__':
    main()
