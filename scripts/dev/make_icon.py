"""Create the small C-mark icon used by the Qt window, tray and EXE."""
import math
import struct
import zlib
from pathlib import Path


def png(size):
    pixels = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            scale = size / 256
            px, py = x / scale, y / scale
            # Deep navy field.
            r, g, b, a = 23, 34, 53, 255
            dx, dy = abs(px - 128), abs(py - 128)
            if dx <= 112 and dy <= 112 and (dx - 112) ** 2 + (dy - 112) ** 2 <= 0:
                r, g, b = 45, 108, 223
            # Rounded-square approximation for the blue mark.
            qx, qy = max(abs(px - 128) - 100, 0), max(abs(py - 128) - 100, 0)
            if qx * qx + qy * qy <= 22 * 22:
                r, g, b = 45, 108, 223
            radius = math.hypot(px - 128, py - 128)
            angle = math.degrees(math.atan2(py - 128, px - 128)) % 360
            # White C, leaving a clean opening on the right.
            if 54 <= radius <= 78 and 48 <= angle <= 312:
                r, g, b = 247, 249, 252
            i = (y * size + x) * 4
            pixels[i:i + 4] = bytes((r, g, b, a))
    raw = b"".join(b"\x00" + bytes(pixels[y * size * 4:(y + 1) * size * 4]) for y in range(size))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main():
    sizes = (16, 32, 48, 64, 128, 256)
    blobs = [png(size) for size in sizes]
    offset = 6 + 16 * len(blobs)
    directory = struct.pack("<HHH", 0, 1, len(blobs))
    entries = []
    for size, data in zip(sizes, blobs):
        entries.append(struct.pack("<BBBBHHII", 0 if size == 256 else size, 0 if size == 256 else size, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    out = Path(__file__).resolve().parents[2] / "app.ico"
    out.write_bytes(directory + b"".join(entries) + b"".join(blobs))
    print(out)


if __name__ == "__main__":
    main()
