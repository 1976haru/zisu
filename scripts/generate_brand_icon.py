"""Generate the checked-in Windows icon from the vector-like brand renderer."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcrs.branding import ico_bytes


def main() -> int:
    target = Path(__file__).resolve().parents[1] / "assets" / "zisu_signal_lab.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ico_bytes())
    print(f"Generated {target.name} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
