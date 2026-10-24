import os
import sys
from mcrs.startup_probe import write_stage as _write_smoke_report
from mcrs.startup_probe import safe_exception_details as _safe_exception_details


def _apply_offline_smoke_arguments(arguments):
    """Translate explicit CI smoke arguments before any application imports."""
    # The QA entrypoint validates its fresh data folder before redirecting any
    # process-wide product paths. Do not pre-seed ZISU_DATA_DIR from its CLI.
    if "--qa-offline-soak" in arguments or "--qa-short" in arguments:
        return
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


def _is_offline_qa_requested(arguments):
    return "--qa-offline-soak" in arguments or "--qa-short" in arguments


def _run_offline_qa(arguments):
    """Run the explicitly isolated frozen-EXE QA entrypoint, never the GUI."""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="ZisuResearchStudio offline QA")
    parser.add_argument("--qa-offline-soak", action="store_true")
    parser.add_argument("--qa-short", action="store_true",
                        help="CI-only accelerated scheduler regression; not a six-hour result")
    parser.add_argument("--short-duration-seconds", type=int, default=45)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--duration-hours", type=int, default=6)
    args = parser.parse_args(arguments)
    if not args.qa_offline_soak:
        parser.error("--qa-short can only be used with --qa-offline-soak")
    if not getattr(sys, "frozen", False):
        parser.error("Offline soak mode must run inside the packaged Windows executable")
    if args.duration_hours != 6:
        parser.error("The full offline soak duration is fixed at six hours")
    if args.qa_short and not 30 <= args.short_duration_seconds <= 90:
        parser.error("Short CI QA duration must be between 30 and 90 seconds")

    data_dir = Path(args.data_dir).resolve()
    report_dir = Path(args.report_dir).resolve()
    if data_dir.name.casefold() != "data" or report_dir.name.casefold() != "reports":
        parser.error("Use separate data and reports folders under a new QA run folder")
    if data_dir.parent != report_dir.parent:
        parser.error("QA data and reports must be siblings under one isolated run folder")
    qa_root = data_dir.parent
    if "qa" not in qa_root.name.casefold() and "soak" not in qa_root.name.casefold():
        parser.error("The isolated run folder name must clearly contain QA or soak")
    if qa_root.exists() or data_dir.exists() or report_dir.exists():
        parser.error("Refusing to reuse an existing QA run folder")

    def overlaps(left, right):
        left = left.resolve()
        right = right.resolve()
        return left == right or left in right.parents or right in left.parents

    protected = [Path(r"D:\03 zisu\ZISU_APP")]
    for variable in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(variable)
        if base:
            protected.append(Path(base) / "ZisuResearchStudio")
    existing_data = os.environ.get("ZISU_DATA_DIR")
    if existing_data:
        protected.append(Path(existing_data))
    else:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            protected.append(Path(local) / "ZisuResearchStudio")
    if any(overlaps(qa_root, path) for path in protected):
        parser.error("The QA folder overlaps a protected installation or user-data location")

    # Redirect all product stores before importing any feature modules. This
    # intentionally does not load or copy the real DPAPI/OAuth credentials.
    qa_root.mkdir(parents=False, exist_ok=False)
    data_dir.mkdir()
    report_dir.mkdir()
    isolated_local = qa_root / "localappdata"
    isolated_roaming = qa_root / "appdata"
    isolated_temp = qa_root / "temp"
    for directory in (isolated_local, isolated_roaming, isolated_temp):
        directory.mkdir()
    os.environ["ZISU_DATA_DIR"] = str(data_dir)
    os.environ["LOCALAPPDATA"] = str(isolated_local)
    os.environ["APPDATA"] = str(isolated_roaming)
    os.environ["TEMP"] = str(isolated_temp)
    os.environ["TMP"] = str(isolated_temp)
    os.environ.pop("YOUTUBE_API_KEY", None)

    from mcrs.offline_soak import run_offline_soak
    mode = "short" if args.qa_short else "wallclock"
    return run_offline_soak(
        data_dir=data_dir,
        report_dir=report_dir,
        duration_hours=args.duration_hours,
        mode=mode,
        short_duration_seconds=args.short_duration_seconds,
    )


if __name__ == '__main__':
    if _is_offline_qa_requested(sys.argv[1:]):
        raise SystemExit(_run_offline_qa(sys.argv[1:]))
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
            _write_smoke_report("startup_failed", **_safe_exception_details(exc))
        raise
