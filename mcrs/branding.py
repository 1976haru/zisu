"""Small, dependency-free ZISU SIGNAL LAB brand assets."""
from __future__ import annotations

import base64
import math
import struct

from .version import DISPLAY_VERSION

DISPLAY_NAME = "ZISU SIGNAL LAB"
KOREAN_NAME = "지수 시그널 랩"
SLOGAN = "채널 성장의 신호를 발견하다."

_BACKGROUND = (18, 43, 53)
_SIGNAL = (113, 205, 174)
_WAVE = (242, 248, 244)
_ACCENT = (242, 180, 83)


def render_rgb(size: int) -> bytes:
    """Render a crisp square mark using only the Python standard library."""
    if isinstance(size, bool) or not isinstance(size, int) or not 16 <= size <= 512:
        raise ValueError("Brand icon size must be between 16 and 512 pixels.")
    scale = 3
    high = size * scale
    pixels = bytearray(_BACKGROUND * (high * high))
    center = (high - 1) / 2
    unit = size / 48 * scale

    def paint(x: int, y: int, color: tuple[int, int, int]):
        offset = (y * high + x) * 3
        pixels[offset:offset + 3] = bytes(color)

    outer = 17.5 * unit
    inner = 15.0 * unit
    for y in range(high):
        for x in range(high):
            distance = math.hypot(x - center, y - center)
            if inner <= distance <= outer:
                paint(x, y, _SIGNAL)

    points = [(9, 25), (17, 25), (21, 17), (26, 31), (31, 21), (39, 21)]
    thickness = 1.55 * unit
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        ax, ay, bx, by = (x1 * unit, y1 * unit, x2 * unit, y2 * unit)
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        left = max(0, int(min(ax, bx) - thickness))
        right = min(high, int(max(ax, bx) + thickness + 1))
        top = max(0, int(min(ay, by) - thickness))
        bottom = min(high, int(max(ay, by) + thickness + 1))
        for y in range(top, bottom):
            for x in range(left, right):
                ratio = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
                if math.hypot(x - (ax + ratio * dx), y - (ay + ratio * dy)) <= thickness:
                    paint(x, y, _WAVE)

    dot_x, dot_y, dot_radius = 34 * unit, 13 * unit, 2.3 * unit
    for y in range(max(0, int(dot_y - dot_radius)), min(high, int(dot_y + dot_radius + 1))):
        for x in range(max(0, int(dot_x - dot_radius)), min(high, int(dot_x + dot_radius + 1))):
            if math.hypot(x - dot_x, y - dot_y) <= dot_radius:
                paint(x, y, _ACCENT)

    # Box-filter the supersampled raster for smoother Tk and Windows icons.
    output = bytearray(size * size * 3)
    samples = scale * scale
    for y in range(size):
        for x in range(size):
            sums = [0, 0, 0]
            for sy in range(scale):
                start = (((y * scale + sy) * high) + x * scale) * 3
                for sx in range(scale):
                    offset = start + sx * 3
                    for channel in range(3):
                        sums[channel] += pixels[offset + channel]
            target = (y * size + x) * 3
            output[target:target + 3] = bytes(value // samples for value in sums)
    return bytes(output)


def ppm_data(size: int = 48) -> str:
    header = f"P6\n{size} {size}\n255\n".encode("ascii")
    return base64.b64encode(header + render_rgb(size)).decode("ascii")


def ico_bytes(sizes=(16, 32, 48, 256)) -> bytes:
    """Build a multi-resolution ICO (32-bit DIB) without image dependencies."""
    sizes = tuple(sizes)
    if not sizes or any(isinstance(size, bool) or not isinstance(size, int) or not 16 <= size <= 256 for size in sizes):
        raise ValueError("ICO sizes must be between 16 and 256 pixels.")
    entries = []
    images = []
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        rgb = render_rgb(size)
        xor_rows = bytearray()
        for y in range(size - 1, -1, -1):
            for x in range(size):
                pos = (y * size + x) * 3
                r, g, b = rgb[pos:pos + 3]
                xor_rows.extend((b, g, r, 255))
        mask_stride = ((size + 31) // 32) * 4
        mask = bytes(mask_stride * size)
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                             len(xor_rows) + len(mask), 0, 0, 0, 0)
        image = header + xor_rows + mask
        images.append(image)
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                                   1, 32, len(image), offset))
        offset += len(image)
    return struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries) + b"".join(images)


def apply_window_branding(root) -> None:
    """Set the user-facing window title and a compact native Tk icon."""
    root.title(f"{DISPLAY_NAME} · {KOREAN_NAME} · {DISPLAY_VERSION}")
    try:
        import tkinter as tk
        image = tk.PhotoImage(master=root, data=ppm_data(), format="PPM")
        root.iconphoto(True, image)
        root._zisu_signal_lab_icon = image
    except Exception:
        # Branding must never prevent startup on a Tk build without PPM support.
        pass
