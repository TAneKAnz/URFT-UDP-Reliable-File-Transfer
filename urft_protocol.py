from __future__ import annotations

import struct
from collections import namedtuple

# packet types
TYPE_DATA    = 0x00
TYPE_ACK     = 0x01
TYPE_META    = 0x02
TYPE_FIN     = 0x03
TYPE_FIN_ACK = 0x04

# protocol params
CHUNK_SIZE   = 1400
WINDOW_SIZE  = 128
INIT_TIMEOUT = 1.0
MAX_RETRIES  = 20

# header: type(1B) + seq(4B) + len(2B) = 7 bytes
HEADER_FMT  = "!BIH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

Packet = namedtuple("Packet", ["ptype", "seq", "data"])


def pack_data(seq: int, payload: bytes) -> bytes:
    hdr = struct.pack(HEADER_FMT, TYPE_DATA, seq, len(payload))
    return hdr + payload


def pack_ack(seq: int) -> bytes:
    return struct.pack(HEADER_FMT, TYPE_ACK, seq, 0)


def pack_meta(filename: str, filesize: int) -> bytes:
    name_bytes = filename.encode()
    hdr = struct.pack(HEADER_FMT, TYPE_META, 0, len(name_bytes))
    size_field = struct.pack("!Q", filesize)
    return hdr + name_bytes + size_field


def pack_fin(seq: int) -> bytes:
    return struct.pack(HEADER_FMT, TYPE_FIN, seq, 0)


def pack_fin_ack(seq: int) -> bytes:
    return struct.pack(HEADER_FMT, TYPE_FIN_ACK, seq, 0)


def unpack(raw: bytes) -> Packet:
    if len(raw) < HEADER_SIZE:
        raise ValueError(f"Packet too short: {len(raw)} bytes")
    ptype, seq, length = struct.unpack_from(HEADER_FMT, raw)
    payload = raw[HEADER_SIZE: HEADER_SIZE + length]
    return Packet(ptype=ptype, seq=seq, data=payload)


def unpack_meta_raw(raw: bytes) -> tuple[str, int]:
    if len(raw) < HEADER_SIZE:
        raise ValueError("META packet too short")
    ptype, seq, name_len = struct.unpack_from(HEADER_FMT, raw)
    if ptype != TYPE_META:
        raise ValueError(f"Expected META(0x02), got 0x{ptype:02x}")
    name_start = HEADER_SIZE
    name_end   = name_start + name_len
    size_end   = name_end + 8
    if len(raw) < size_end:
        raise ValueError("META packet truncated")
    filename  = raw[name_start:name_end].decode()
    filesize, = struct.unpack_from("!Q", raw, name_end)
    return filename, filesize
