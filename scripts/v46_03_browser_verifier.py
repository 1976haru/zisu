"""Windowed-executable entry point for the user-run browser verification GUI."""
from pathlib import Path
import sys

# PyInstaller executes this entry point from a temporary extraction directory.
# Bundle the project package directly instead of relying on the checkout path.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcrs.browser_verifier import main


if __name__ == "__main__":
    main()
