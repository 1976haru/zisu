"""Privacy-safe offline diagnostics for support and packaged builds."""
from __future__ import annotations

import importlib.util
import os
import platform
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .store import db_path


def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, AttributeError):
        return False


def collect(database=None):
    target = Path(database or db_path())
    report = {
        "app_version": "4.5.4",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "frozen_executable": bool(getattr(sys, "frozen", False)),
        "oauth_module": _module_available("google_auth_oauthlib"),
        "google_credentials_module": _module_available("google.oauth2.credentials"),
        "data_directory_exists": target.parent.exists(),
        "data_directory_writable": os.access(target.parent, os.W_OK) if target.parent.exists() else False,
        "database_exists": target.exists(),
        "database_integrity": "not_checked",
    }
    if target.exists():
        try:
            uri = target.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
                report["database_integrity"] = connection.execute("PRAGMA quick_check").fetchone()[0]
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                for table in ("profiles", "candidates", "metrics", "genre_audit"):
                    if table in tables:
                        report[f"{table}_rows"] = connection.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
        except (OSError, sqlite3.Error) as exc:
            report["database_integrity"] = "error"
            report["database_error_type"] = type(exc).__name__
    return report


def format_report(report):
    """No paths, account identifiers, token values, or exception text are emitted."""
    return "\n".join(f"{key}: {value}" for key, value in sorted(report.items()))


def main():
    print("ZISU Research Studio 안전 진단 (비밀정보 제외)")
    print(format_report(collect()))


if __name__ == "__main__":
    main()
