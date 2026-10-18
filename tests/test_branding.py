from __future__ import annotations

from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from mcrs import branding


class BrandingTests(unittest.TestCase):
    def test_beginner_manual_uses_official_brand(self):
        manual = Path(__file__).resolve().parents[1] / "사용설명서_초보자.html"
        text = manual.read_text(encoding="utf-8")
        self.assertIn("ZISU SIGNAL LAB", text)
        self.assertIn("지수 시그널 랩", text)
        self.assertIn("채널 성장의 신호를 발견하다.", text)

    def test_official_brand_identity_and_slogan_are_stable(self):
        self.assertEqual("ZISU SIGNAL LAB", branding.DISPLAY_NAME)
        self.assertEqual("지수 시그널 랩", branding.KOREAN_NAME)
        self.assertEqual("채널 성장의 신호를 발견하다.", branding.SLOGAN)

    def test_generated_ico_contains_expected_windows_sizes(self):
        data = branding.ico_bytes()
        reserved, kind, count = struct.unpack_from("<HHH", data)
        self.assertEqual((0, 1, 4), (reserved, kind, count))
        entries = [struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
                   for index in range(count)]
        sizes = [entry[0] or 256 for entry in entries]
        self.assertEqual([16, 32, 48, 256], sizes)
        self.assertTrue(all(entry[7] + entry[6] <= len(data) for entry in entries))

    def test_tk_window_branding_sets_title_and_keeps_icon_alive(self):
        class Root:
            def __init__(self): self.title_text = ""; self.icon = None
            def title(self, value): self.title_text = value
            def iconphoto(self, _default, image): self.icon = image
        root = Root()
        image = object()
        with patch("tkinter.PhotoImage", return_value=image):
            branding.apply_window_branding(root)
        self.assertEqual("ZISU SIGNAL LAB · 지수 시그널 랩", root.title_text)
        self.assertIs(root.icon, image)
        self.assertIs(root._zisu_signal_lab_icon, image)
        self.assertTrue(branding.ppm_data().startswith("UDYK"))  # Base64 P6 header.

    def test_gui_start_screen_and_header_use_the_official_identity(self):
        source = (Path(__file__).resolve().parents[1] / "mcrs" / "gui.py").read_text(encoding="utf-8")
        self.assertIn("apply_window_branding(self)", source)
        self.assertIn("{DISPLAY_NAME} · {KOREAN_NAME}", source)
        self.assertIn("{SLOGAN}", source)


if __name__ == "__main__":
    unittest.main()
