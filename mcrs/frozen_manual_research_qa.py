"""Fixture-only callback E2E for the frozen Windows manual research UI.

This opt-in gate uses real Studio/ProfileDialog/ManualResearchWindow widgets and
their production callbacks. It is not an OS mouse/keyboard test. Both launches
are limited to a newly created QA root and have sockets blocked.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

from .startup_probe import write_stage as _write_startup_stage


def _stage(name: str, **details) -> None:
    _write_startup_stage(name, **details)


def _last_probe_stage(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload.get("stage", "")) or None
    except (OSError, ValueError, TypeError):
        return None


def _inside(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    return left == right or left in right.parents or right in left.parents


def _validate_root(root: Path, *, fresh: bool) -> tuple[Path, Path, Path]:
    root = root.expanduser().resolve()
    if "qa" not in root.name.casefold() or "manual" not in root.name.casefold():
        raise ValueError("The isolated run folder name must include QA and manual.")
    data, reports = root / "data", root / "reports"
    if fresh and (root.exists() or data.exists() or reports.exists()):
        raise ValueError("Refusing to reuse an existing manual E2E QA folder.")
    if not fresh and (not root.is_dir() or not data.is_dir() or not reports.is_dir()):
        raise ValueError("The manual E2E resume folder is incomplete.")
    protected = [Path(r"D:\03 zisu\ZISU_APP")]
    for variable in ("LOCALAPPDATA", "APPDATA"):
        value = os.environ.get(variable)
        if value:
            redirected_base = Path(value).resolve()
            fixture_base = (root / ("localappdata" if variable == "LOCALAPPDATA" else "appdata")).resolve()
            if redirected_base != fixture_base:
                protected.append(redirected_base / "ZisuResearchStudio")
    value = os.environ.get("ZISU_DATA_DIR")
    if value:
        existing_data = Path(value).resolve()
        if existing_data != data.resolve():
            protected.append(existing_data)
    for path in protected:
        if _inside(root, path):
            raise ValueError("The isolated E2E root overlaps a protected app or data location.")
    return root, data, reports


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def _block_network():
    attempts = {"count": 0}
    original = []

    def blocked(*_args, **_kwargs):
        attempts["count"] += 1
        raise OSError("Network disabled in frozen manual E2E fixture mode.")

    for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                        (socket, "create_connection"), (socket, "getaddrinfo"),
                        (urllib.request, "urlopen")):
        original.append((owner, name, getattr(owner, name)))
        setattr(owner, name, blocked)
    try:
        yield attempts
    finally:
        for owner, name, function in reversed(original):
            setattr(owner, name, function)


def _prepare_environment(root: Path, data: Path, reports: Path) -> dict[str, str]:
    root.mkdir(parents=False, exist_ok=False)
    data.mkdir()
    reports.mkdir()
    local, roaming, temp = root / "localappdata", root / "appdata", root / "temp"
    for directory in (local, roaming, temp):
        directory.mkdir()
    env = os.environ.copy()
    env.update({
        "ZISU_DATA_DIR": str(data),
        "LOCALAPPDATA": str(local),
        "APPDATA": str(roaming),
        "TEMP": str(temp),
        "TMP": str(temp),
        "ZISU_FROZEN_MANUAL_E2E": "1",
    })
    for key in ("YOUTUBE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                "ZISU_AUTO_RESEARCH_REQUEST_ID", "ZISU_EXE_SMOKE_REPORT",
                "ZISU_EXE_SMOKE_PROFILE_TEST"):
        env.pop(key, None)
    return env


def _find_child(app, kind):
    return next((child for child in app.winfo_children() if isinstance(child, kind)), None)


def _create_profile_from_dialog(app, ProfileDialog, *, name: str, market: str,
                                related: tuple[str, ...]):
    fill_state = {"error": None}

    def fill_dialog(attempt=0):
        dialog = _find_child(app, ProfileDialog)
        if dialog is None:
            if attempt < 250 and app.winfo_exists():
                app.after(20, lambda: fill_dialog(attempt + 1))
            elif attempt >= 250:
                fill_state["error"] = "Timed out waiting for the product profile dialog."
            return
        try:
            dialog.name.set(name)
            dialog.market.set(market)
            dialog.genre.set("MORNING_CHILL_RAP")
            index = dialog.genre_keys.index("MORNING_CHILL_RAP")
            dialog.genre_display.set(dialog.genre_names[index])
            for key, variable in dialog.related_vars.items():
                variable.set(key in related)
            dialog.subgenre_var.set(f"fixture-{market.lower()}-subgenre")
            dialog.facet_vars["languages"].set("Japanese" if market == "JP" else "Korean")
            dialog.search_languages_var.set("JA, EN" if market == "JP" else "KO, EN")
            dialog._update_related_summary()
            dialog._save()
        except Exception as exc:
            fill_state["error"] = f"Profile dialog callback failed: {type(exc).__name__}"
            if dialog.winfo_exists():
                dialog.destroy()

    app.after(80, fill_dialog)
    app.after(6000, lambda: app.destroy() if _find_child(app, ProfileDialog) else None)
    profile_id = app.add_profile()
    if fill_state["error"]:
        raise RuntimeError(fill_state["error"])
    if not profile_id:
        raise RuntimeError(f"The {market} profile dialog did not save through Studio.add_profile().")
    return profile_id


def _manual_window(app, ManualResearchWindow):
    window = _find_child(app, ManualResearchWindow)
    if window is None:
        raise RuntimeError("Studio did not open the manual research window.")
    return window


def _exercise_first_launch() -> dict:
    from . import manual_research, store
    from .gui import ProfileDialog, Studio
    from .manual_research_ui import ManualResearchWindow

    app = Studio()
    result: dict = {"status": "FAIL", "ui_input_method": "widget_values_and_production_callbacks"}
    completed = {"done": False}

    def drive():
        nonlocal result
        try:
            jp = _create_profile_from_dialog(
                app, ProfileDialog, name="Fixture 日本語 JP", market="JP",
                related=("CHILL_RAP", "RNB_HIPHOP"),
            )
            jp_window = _manual_window(app, ManualResearchWindow)
            jp_profile = store.get_profile(jp)
            if Path(store.db_path()).resolve() != Path(os.environ["ZISU_DATA_DIR"], "zisu_v2.db").resolve():
                raise AssertionError("Frozen QA opened a database outside its isolated data root.")
            jp_related = json.loads(jp_profile["related_genres_json"])
            if jp_profile["market"] != "JP" or jp_profile["genre"] != "MORNING_CHILL_RAP":
                raise AssertionError("JP core market/genre did not persist.")
            if set(jp_related) != {"CHILL_RAP", "RNB_HIPHOP"}:
                raise AssertionError("JP related genres were not stored separately.")
            if "Japanese" not in json.loads(jp_profile["genre_config_json"])["facets"]["languages"]:
                raise AssertionError("Content language facet did not persist.")

            urls = [f"https://www.youtube.com/watch?v={index:011d}" for index in range(1, 51)]
            jp_window.links.delete("1.0", "end")
            jp_window.links.insert("1.0", "\n".join(urls))
            preview50 = jp_window._preview()
            if not preview50.can_save or len(preview50.valid) != 50:
                raise AssertionError("50-link preview did not accept exactly 50 unique fixtures.")

            jp_window.links.insert("end", "\nhttps://www.youtube.com/watch?v=00000000051")
            preview51 = jp_window._preview()
            if preview51.can_save or not any(line == 0 for line, _ in preview51.errors):
                raise AssertionError("51-link preview was not rejected as a whole.")
            if manual_research.list_batches(jp):
                raise AssertionError("Invalid preview partially wrote a batch.")

            jp_window.links.delete("1.0", "end")
            jp_window.links.insert("1.0", "\n".join((urls[0], urls[0], "https://youtube.com.evil.example/watch?v=00000000052")))
            invalid = jp_window._preview()
            if len(invalid.valid) != 1 or len(invalid.duplicates) != 1 or len(invalid.errors) != 1 or invalid.can_save:
                raise AssertionError("Duplicate/fake-domain preview did not report each condition.")
            if manual_research.list_batches(jp):
                raise AssertionError("Invalid and duplicate links wrote a batch.")

            jp_window.links.delete("1.0", "end")
            jp_window.links.insert("1.0", "\n".join(urls))
            jp_window.hours.set("1"); jp_window.analysis.set("10"); jp_window.breaks.set("5")
            jp_window._preview(); jp_window._create()
            batch_id = jp_window.batch_id
            rows = manual_research.list_items(batch_id, jp)
            if len(rows) != 50 or [row["position"] for row in rows] != list(range(1, 51)):
                raise AssertionError("50-link batch did not persist in input order.")
            if any(row["auto_genre_decision"] != manual_research.REVIEW for row in rows):
                raise AssertionError("Missing metadata did not remain REVIEW.")
            if not jp_window.open_button.instate(["disabled"]):
                raise AssertionError("Video-open action was enabled before a user selection.")

            video_id = rows[0]["video_id"]
            jp_window.tree.selection_set(video_id)
            jp_window._select()
            if jp_window.open_button.instate(["disabled"]):
                raise AssertionError("Selecting a fixture video did not enable its one-video open action.")
            jp_window._resume()
            app.update()
            time.sleep(1.1)
            app.update()
            jp_window._pause()
            first_pause = manual_research.get_batch(batch_id, jp)["active_seconds"]
            if manual_research.get_batch(batch_id, jp)["state"] != "paused":
                raise AssertionError("Timer pause callback did not persist paused state.")
            jp_window._resume()
            time.sleep(0.1)
            jp_window._break()
            time.sleep(1.1)
            jp_window._resume()
            jp_window._pause()
            paused = manual_research.get_batch(batch_id, jp)
            if paused["active_seconds"] < first_pause or paused["break_seconds"] <= 0:
                raise AssertionError("Resume/break time was not kept in separate persisted counters.")

            jp_window.human_fit.set("제외")
            jp_window.fields["notes"].delete("1.0", "end")
            jp_window.fields["notes"].insert("1.0", "fixture human exclusion note")
            jp_window.fields["listened_segments"].delete("1.0", "end")
            jp_window.fields["listened_segments"].insert("1.0", "00:12-00:48 fixture segment")
            jp_window._save()
            saved = manual_research.get_item(batch_id, jp, video_id)
            if saved["human_fit"] != "제외" or saved["notes"] != "fixture human exclusion note":
                raise AssertionError("Human EXCLUDE or note did not persist through the UI callback.")
            jp_window._close()

            kr = _create_profile_from_dialog(
                app, ProfileDialog, name="Fixture 한국어 KR", market="KR",
                related=("CHILL_RAP",),
            )
            kr_profile = store.get_profile(kr)
            kr_window = _manual_window(app, ManualResearchWindow)
            if kr_profile["market"] != "KR" or kr_profile["genre"] != "MORNING_CHILL_RAP":
                raise AssertionError("KR profile did not keep its independent market/core genre.")
            if json.loads(kr_profile["related_genres_json"]) != ["CHILL_RAP"]:
                raise AssertionError("KR related genre leaked from JP profile.")
            kr_urls = [f"https://youtu.be/{index:011d}" for index in (90001, 90002)]
            kr_window.links.delete("1.0", "end"); kr_window.links.insert("1.0", "\n".join(kr_urls))
            kr_window._preview(); kr_window._create()
            kr_items = manual_research.list_items(kr_window.batch_id, kr)
            if len(kr_items) != 2 or {item["video_id"] for item in kr_items} & {row["video_id"] for row in rows}:
                raise AssertionError("KR manual list is not profile-isolated from JP.")
            kr_window._close()

            result = {
                "status": "PASS",
                "ui_input_method": "widget_values_and_production_callbacks",
                "os_mouse_keyboard_input": "PENDING_UNAVAILABLE_DESKTOP",
            "post_restart_gui_render": "NOT_EXERCISED_HEADLESS_FROZEN_RESUME",
                "profile_count": len(store.list_profiles()),
                "jp_profile_id": jp,
                "kr_profile_id": kr,
                "jp_batch_id": batch_id,
                "jp_link_count": len(rows),
                "invalid_51_rejected_without_write": True,
                "duplicate_and_fake_host_rejected": True,
                "missing_metadata_review_count": sum(
                    row["auto_genre_decision"] == manual_research.REVIEW for row in rows),
                "active_seconds": paused["active_seconds"],
                "break_seconds": paused["break_seconds"],
                "video_active_seconds": saved["active_seconds"],
                "human_exclude_and_note_saved": True,
                "expected_human_fit": saved["human_fit"],
                "video_opened": False,
                "oauth_connected": False,
            }
        except Exception as exc:
            result = {"status": "FAIL", "error_type": type(exc).__name__,
                      "error": str(exc)[:400],
                      "ui_input_method": "widget_values_and_production_callbacks"}
        finally:
            completed["done"] = True
            if app.winfo_exists():
                app.destroy()

    app.after(100, drive)
    app.after(30000, lambda: app.destroy() if app.winfo_exists() else None)
    app.mainloop()
    if not completed["done"]:
        return {"status": "FAIL", "error_type": "GuiTimeout", "error": "Fixture callback workflow timed out."}
    return result


def _resume_after_process_restart(root: Path, reports: Path) -> dict:
    """Verify persisted product data from a separate frozen-process launch.

    This process is deliberately headless: environments without an input
    desktop can still verify the reopened SQLite state without pretending to
    have tested OS keyboard/mouse input or a rendered post-restart window.
    """
    _stage("resume_start", pid=os.getpid())
    from . import manual_research, store
    _stage("resume_product_modules_imported")

    state_path = reports / "e2e-state.json"
    if not state_path.is_file():
        raise ValueError("Parent-process E2E state marker is missing.")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema") != 1 or state.get("qa_root") != str(root):
        raise ValueError("E2E resume marker does not match this QA root.")
    _stage("resume_state_marker_validated")
    expected_db = root / "data" / "zisu_v2.db"
    if Path(store.db_path()).resolve() != expected_db.resolve():
        raise ValueError("Frozen resume process opened a database outside the fixture root.")

    _stage("resume_before_profile_read")
    profiles = store.list_profiles()
    _stage("resume_profiles_read", profile_count=len(profiles))
    jp_id, kr_id = state["jp_profile_id"], state["kr_profile_id"]
    if len(profiles) != 2 or {p["id"] for p in profiles} != {jp_id, kr_id}:
        raise AssertionError("Profiles did not survive the frozen process restart.")
    batch_id = state["jp_batch_id"]
    rows = manual_research.list_items(batch_id, jp_id)
    _stage("resume_items_read", item_count=len(rows))
    if len(rows) != 50:
        raise AssertionError("JP 50-link queue did not restore after restart.")
    video_id = rows[0]["video_id"]
    restored = manual_research.get_item(batch_id, jp_id, video_id)
    batch = manual_research.get_batch(batch_id, jp_id)
    _stage("resume_evaluation_read", paused=batch["state"] == "paused")
    if restored["human_fit"] != state.get("expected_human_fit") or restored["notes"] != "fixture human exclusion note":
        raise AssertionError("Human label or notes did not survive process restart.")
    if batch["state"] != "paused" or batch["running_since_utc"] or batch["break_started_utc"]:
        raise AssertionError("Paused timer state was not restored cleanly after restart.")
    if batch["active_seconds"] != state["expected_active_seconds"]:
        raise AssertionError("Restart downtime changed the persisted active-session total.")
    if batch["break_seconds"] != state["expected_break_seconds"]:
        raise AssertionError("Restart downtime changed the persisted break total.")
    if restored["active_seconds"] != state["expected_video_active_seconds"] or restored["timer_started_utc"]:
        raise AssertionError("Restart downtime changed the persisted active-video total.")

    with store.connect() as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = db.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_key_errors:
        raise AssertionError("Fixture database integrity or foreign-key check failed after restart.")

    # Exercise the real resume/pause persistence API in this new frozen process.
    _stage("resume_before_timer_api")
    manual_research.start_or_resume(batch_id, jp_id, video_id)
    manual_research.pause(batch_id, jp_id)
    batch = manual_research.get_batch(batch_id, jp_id)
    if batch["state"] != "paused":
        raise AssertionError("Resumed timer did not return to paused state.")

    kr_batches = manual_research.list_batches(kr_id)
    kr_items = manual_research.list_items(kr_batches[0]["id"], kr_id) if kr_batches else []
    jp_ids = {row["video_id"] for row in rows}
    if len(kr_items) != 2 or jp_ids & {row["video_id"] for row in kr_items}:
        raise AssertionError("JP/KR queues crossed profile scope after restart.")
    if store.get_profile(jp_id)["connected_email"] or store.get_profile(kr_id)["connected_email"]:
        raise AssertionError("Fixture profile unexpectedly has an account connection.")
    _stage("resume_verification_complete", profile_count=len(profiles), jp_items=len(rows), kr_items=len(kr_items),
           database_integrity=integrity, foreign_key_errors=len(foreign_key_errors))
    return {
        "status": "PASS", "process_restart_verified": True,
        "restart_verification": "fresh_frozen_process_database_and_resume_api",
        "profile_count": len(profiles), "jp_link_count": len(rows),
        "kr_link_count": len(kr_items), "jp_human_exclude_restored": True,
        "jp_note_restored": True, "active_time_before_resume": state["expected_active_seconds"],
        "break_time_before_resume": state["expected_break_seconds"],
        "video_time_before_resume": state["expected_video_active_seconds"],
        "restart_gap_added_seconds": 0, "database_integrity": integrity,
        "foreign_key_errors": len(foreign_key_errors), "jp_kr_isolation": True,
        "active_time_after_resume_pause": batch["active_seconds"],
        "break_time_after_resume_pause": batch["break_seconds"],
        "oauth_connected": False,
    }

def _run_parent(root: Path, data: Path, reports: Path) -> int:
    env = _prepare_environment(root, data, reports)
    for key in ("YOUTUBE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                "ZISU_AUTO_RESEARCH_REQUEST_ID", "ZISU_EXE_SMOKE_REPORT",
                "ZISU_EXE_SMOKE_PROFILE_TEST"):
        os.environ.pop(key, None)
    os.environ.update(env)
    state_path = reports / "e2e-state.json"
    final_path = reports / "manual-research-e2e.json"
    try:
        with _block_network() as network:
            first = _exercise_first_launch()
        if first.get("status") != "PASS":
            final = {"result": "FAIL", "first_process": first,
                     "network_attempts": network["count"], "os_input": "PENDING_UNAVAILABLE_DESKTOP"}
            _write_json(final_path, final)
            return 1
        state = {"schema": 1, "qa_root": str(root),
                 "jp_profile_id": first["jp_profile_id"], "kr_profile_id": first["kr_profile_id"],
                 "jp_batch_id": first["jp_batch_id"], "expected_active_seconds": first["active_seconds"],
                 "expected_break_seconds": first.get("break_seconds", 0),
                 "expected_video_active_seconds": first["video_active_seconds"],
                 "expected_human_fit": first["expected_human_fit"]}
        _write_json(state_path, state)
        command = [sys.executable, "--qa-manual-research-e2e-resume", "--qa-root", str(root)]
        executable = Path(sys.executable).resolve()
        resume_probe_path = reports / "resume-startup.json"
        env["ZISU_EXE_SMOKE_REPORT"] = str(resume_probe_path)
        env.pop("ZISU_EXE_SMOKE_START_COUNTER", None)
        env.pop("ZISU_EXE_SMOKE_COUNTER_FREQUENCY", None)
        child_started_utc = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat()
        child_start_monotonic = time.monotonic()
        child = subprocess.Popen(
            command, cwd=os.getcwd(), env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        timed_out = False
        alive_at_timeout = False
        killed_after_timeout = False
        try:
            child.wait(timeout=45)
        except subprocess.TimeoutExpired:
            timed_out = True
            alive_at_timeout = child.poll() is None
            if alive_at_timeout:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                killed_after_timeout = True
        child_exit_code = child.returncode
        child_process = {
            "pid": child.pid,
            "executable_path": str(executable),
            "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "command_flags": ["--qa-manual-research-e2e-resume", "--qa-root", str(root)],
            "working_directory": os.getcwd(),
            "started_at_utc": child_started_utc,
            "elapsed_seconds": round(time.monotonic() - child_start_monotonic, 3),
            "timed_out": timed_out,
            "alive_at_timeout": alive_at_timeout,
            "killed_after_timeout": killed_after_timeout,
            "exit_code": child_exit_code,
            "completion_report_exists": (reports / "resume-e2e.json").is_file(),
            "startup_probe_exists": resume_probe_path.is_file(),
            "fixture_environment_checks": {
                "data_dir_matches_fixture": env.get("ZISU_DATA_DIR") == str(data),
                "localappdata_is_isolated": env.get("LOCALAPPDATA") == str(root / "localappdata"),
                "appdata_is_isolated": env.get("APPDATA") == str(root / "appdata"),
                "temp_and_tmp_match_isolated_dir": env.get("TEMP") == env.get("TMP") == str(root / "temp"),
                "fixture_mode_enabled": env.get("ZISU_FROZEN_MANUAL_E2E") == "1",
                "api_credentials_present": any(k in env for k in ("YOUTUBE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")),
            },
        }
        _write_json(reports / "resume-process-diagnostic.json", child_process)
        resume_path = reports / "resume-e2e.json"
        if not resume_path.is_file():
            restart = {"status": "FAIL",
                       "error_type": "ChildTimeout" if timed_out else "MissingResumeReport",
                       "child_exit_code": child_exit_code,
                       "startup_last_stage": _last_probe_stage(resume_probe_path)}
        else:
            restart = json.loads(resume_path.read_text(encoding="utf-8"))
        final = {
            "schema": 1, "result": "PASS" if restart.get("status") == "PASS" and network["count"] == 0 else "FAIL",
            "source_head": "uncommitted working tree based on cb528bb7d051b83be6ff3193422ae04c5420feb9",
            "frozen_executable": True, "fixture_only": True,
            "first_process": first, "restart_process": restart,
            "restart_child_exit_code": child_exit_code,
            "restart_child": child_process,
            "parent_network_attempts": network["count"],
            "child_network_attempts": restart.get("network_attempts", "guarded_zero_if_pass"),
            "oauth_api": "not_called", "youtube_open_playback": "not_called",
            "os_mouse_keyboard_input": "PENDING_UNAVAILABLE_DESKTOP",
            "post_restart_gui_render": "NOT_EXERCISED_HEADLESS_FROZEN_RESUME",
            "created_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        }
        _write_json(final_path, final)
        return 0 if final["result"] == "PASS" else 1
    except Exception as exc:
        _write_json(final_path, {"schema": 1, "result": "FAIL", "error_type": type(exc).__name__,
                                 "error": str(exc)[:400], "fixture_only": True})
        return 1


def run_cli(arguments: list[str]) -> int:
    _stage("manual_e2e_run_cli_entered", resume="--qa-manual-research-e2e-resume" in arguments)
    if "--qa-manual-research-e2e-resume" in arguments:
        parser = argparse.ArgumentParser(prog="ZisuResearchStudio manual E2E resume")
        parser.add_argument("--qa-manual-research-e2e-resume", action="store_true")
        parser.add_argument("--qa-root", required=True)
        args = parser.parse_args(arguments)
        _stage("manual_e2e_resume_arguments_parsed")
        try:
            root, data, reports = _validate_root(Path(args.qa_root), fresh=False)
        except Exception as exc:
            _stage("manual_e2e_resume_root_validation_failed", error_type=type(exc).__name__)
            return 2
        _stage("manual_e2e_resume_root_validated")
        expected_environment = {
            "ZISU_DATA_DIR": data,
            "LOCALAPPDATA": root / "localappdata",
            "APPDATA": root / "appdata",
            "TEMP": root / "temp",
            "TMP": root / "temp",
        }
        failed_environment_checks = []
        for variable, expected in expected_environment.items():
            actual = os.environ.get(variable)
            if not actual or Path(actual).resolve() != expected.resolve():
                failed_environment_checks.append(variable)
        if os.environ.get("ZISU_FROZEN_MANUAL_E2E") != "1":
            failed_environment_checks.append("ZISU_FROZEN_MANUAL_E2E")
        if failed_environment_checks:
            _stage("manual_e2e_resume_environment_rejected", failed_checks=failed_environment_checks)
            _write_json(reports / "resume-e2e.json", {
                "status": "FAIL", "error_type": "FixtureEnvironmentMismatch",
                "failed_checks": failed_environment_checks,
            })
            return 2
        _stage("manual_e2e_resume_environment_validated")
        try:
            with _block_network() as network:
                _stage("manual_e2e_resume_before_database_verification")
                result = _resume_after_process_restart(root, reports)
                _stage("manual_e2e_resume_database_verification_complete")
            result["network_attempts"] = network["count"]
            _write_json(reports / "resume-e2e.json", result)
            return 0 if result.get("status") == "PASS" and network["count"] == 0 else 1
        except Exception as exc:
            _write_json(reports / "resume-e2e.json", {"status": "FAIL", "error_type": type(exc).__name__,
                                                        "error": str(exc)[:400]})
            return 1

    parser = argparse.ArgumentParser(prog="ZisuResearchStudio manual E2E")
    parser.add_argument("--qa-manual-research-e2e", action="store_true")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args(arguments)
    root, data, reports = _validate_root(Path(args.data_dir).resolve().parent, fresh=True)
    if data.resolve() != Path(args.data_dir).resolve() or reports.resolve() != Path(args.report_dir).resolve():
        raise SystemExit("Use --data-dir and --report-dir as siblings named data and reports under the new QA root.")
    return _run_parent(root, data, reports)
