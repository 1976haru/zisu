"""Print a deterministic SHA-256 for all frozen product Python sources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def fingerprint() -> dict:
    paths = [ROOT / "main.py", *sorted((ROOT / "mcrs").rglob("*.py"))]
    records = [
        {"path": path.relative_to(ROOT).as_posix(),
         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in paths
    ]
    canonical = json.dumps(records, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return {
        "algorithm": "zisu-runtime-python-v1",
        "file_count": len(records),
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "files": records,
    }


if __name__ == "__main__":
    print(json.dumps(fingerprint(), ensure_ascii=False, indent=2))
