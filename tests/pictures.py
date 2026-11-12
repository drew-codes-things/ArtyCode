import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EIGHT_BIT_COLOUR = (8, 2, 0, 0, 0)


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png(width: int, height: int, colour=(200, 30, 30)) -> bytes:
    row = b"\x00" + bytes(colour) * width
    header = struct.pack(">II", width, height) + bytes(EIGHT_BIT_COLOUR)
    return PNG_SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"")


def gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    start_of_frame = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + start_of_frame + b"\xff\xd9"


def webp_lossy(width: int, height: int) -> bytes:
    frame = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(frame)) + b"WEBP" + b"VP8 " + struct.pack("<I", len(frame)) + frame


def webp_lossless(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    data = b"\x2f" + bits.to_bytes(4, "little")
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(data)) + b"WEBP" + b"VP8L" + struct.pack("<I", len(data)) + data


def webp_extended(width: int, height: int) -> bytes:
    data = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(data)) + b"WEBP" + b"VP8X" + struct.pack("<I", len(data)) + data
