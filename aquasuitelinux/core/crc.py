"""CRC-16/USB, the checksum at the end of Aquacomputer control and software-sensor reports.

Polynomial 0x8005 (reflected 0xA001), init 0xFFFF, final XOR 0xFFFF.
"""

from __future__ import annotations


def _table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_TABLE = _table()


def crc16_usb(data: bytes | bytearray | memoryview) -> int:
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFF


def seal(buf: bytearray, start: int = 1) -> None:
    """Write the checksum of ``buf[start:-2]`` big-endian into the last two bytes."""
    crc = crc16_usb(memoryview(buf)[start:len(buf) - 2])
    buf[-2] = crc >> 8
    buf[-1] = crc & 0xFF


def is_sealed(buf: bytes | bytearray, start: int = 1) -> bool:
    if len(buf) < start + 3:
        return False
    return crc16_usb(memoryview(buf)[start:len(buf) - 2]) == (buf[-2] << 8 | buf[-1])
