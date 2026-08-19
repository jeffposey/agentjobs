"""Generate reproducible AgentJobs PNG icons from the committed SVG primitives."""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path
from typing import NamedTuple
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "app-icon.svg"
OUTPUT_DIR = ROOT / "public" / "icons"


class Shape(NamedTuple):
    kind: str
    values: tuple[float, ...]
    color: tuple[int, int, int, int]


def _color(value: str) -> tuple[int, int, int, int]:
    value = value.removeprefix("#")
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return (red, green, blue, 255)


def _shapes() -> list[Shape]:
    root = ElementTree.parse(SOURCE).getroot()
    shapes: list[Shape] = []
    for element in root:
        kind = element.tag.rsplit("}", 1)[-1]
        fill = _color(element.attrib["fill"])
        if kind == "rect":
            shapes.append(
                Shape(
                    kind,
                    tuple(
                        float(element.attrib.get(name, "0"))
                        for name in ("x", "y", "width", "height", "rx")
                    ),
                    fill,
                )
            )
        elif kind == "circle":
            shapes.append(
                Shape(
                    kind,
                    tuple(float(element.attrib[name]) for name in ("cx", "cy", "r")),
                    fill,
                )
            )
        else:
            raise ValueError(f"Unsupported SVG primitive: {kind}")
    return shapes


def _inside(shape: Shape, x: float, y: float) -> bool:
    if shape.kind == "circle":
        cx, cy, radius = shape.values
        return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2

    left, top, width, height, radius = shape.values
    right, bottom = left + width, top + height
    if not (left <= x <= right and top <= y <= bottom):
        return False
    radius = min(radius, width / 2, height / 2)
    if radius == 0 or left + radius <= x <= right - radius or top + radius <= y <= bottom - radius:
        return True
    corner_x = left + radius if x < left + radius else right - radius
    corner_y = top + radius if y < top + radius else bottom - radius
    return (x - corner_x) ** 2 + (y - corner_y) ** 2 <= radius**2


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def render(size: int) -> bytes:
    shapes = _shapes()
    rows = bytearray()
    for row in range(size):
        rows.append(0)
        y = (row + 0.5) * 512 / size
        for column in range(size):
            x = (column + 0.5) * 512 / size
            pixel = (0, 0, 0, 0)
            for shape in shapes:
                if _inside(shape, x, y):
                    pixel = shape.color
            rows.extend(pixel)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + _chunk(b"IEND", b"")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if generated icons are stale.")
    args = parser.parse_args()
    expected = {
        "icon-192.png": render(192),
        "icon-512.png": render(512),
        "icon-maskable-512.png": render(512),
    }
    stale = [
        name
        for name, data in expected.items()
        if not (OUTPUT_DIR / name).is_file() or (OUTPUT_DIR / name).read_bytes() != data
    ]
    if args.check:
        if stale:
            print(f"Stale PWA icons: {', '.join(stale)}. Run npm run generate:icons.")
            return 1
        print("Generated PWA icons match the committed SVG source.")
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in expected.items():
        (OUTPUT_DIR / name).write_bytes(data)
        print(f"Wrote public/icons/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
