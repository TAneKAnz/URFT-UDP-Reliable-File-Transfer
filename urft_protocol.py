"""
urft_protocol.py — Shared constants and packet encode/decode for URFT.

Packet format (big-endian):
  Byte 0      : type (uint8)
  Bytes 1-4   : seq  (uint32)
  Bytes 5-6   : len  (uint16)  — payload length (0 for ACK/FIN/FIN_ACK)
  Bytes 7+    : payload

Packet types:
  DATA    0x00  — data chunk
  ACK     0x01  — acknowledgement
  META    0x02  — filename + filesize header
  FIN     0x03  — end-of-transfer signal
  FIN_ACK 0x04  — end-of-transfer acknowledgement
"""
from __future__ import annotations

import struct
from collections import namedtuple

# ---------------------------------------------------------------------------
# Packet type constants
# ---------------------------------------------------------------------------
TYPE_DATA    = 0x00
TYPE_ACK     = 0x01
TYPE_META    = 0x02
TYPE_FIN     = 0x03
TYPE_FIN_ACK = 0x04

# ---------------------------------------------------------------------------
# Protocol parameters
# ---------------------------------------------------------------------------
CHUNK_SIZE   = 1400   # bytes per DATA packet payload (stays below 1500 MTU)
WINDOW_SIZE  = 128    # Selective Repeat window size
INIT_TIMEOUT = 1.0    # initial RTO in seconds
MAX_RETRIES  = 20     # max retransmit attempts before aborting

# ---------------------------------------------------------------------------
# Header layout
# ---------------------------------------------------------------------------
# type(1B) + seq(4B) + len(2B) = 7 bytes
HEADER_FMT  = "!BIH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)   # 7

Packet = namedtuple("Packet", ["ptype", "seq", "data"])


# ---------------------------------------------------------------------------
# Encode helpers
# ---------------------------------------------------------------------------

def pack_data(seq: int, payload: bytes) -> bytes:
    """Encode a DATA packet."""
    hdr = struct.pack(HEADER_FMT, TYPE_DATA, seq, len(payload))
    return hdr + payload


def pack_ack(seq: int) -> bytes:
    """Encode an ACK packet."""
    return struct.pack(HEADER_FMT, TYPE_ACK, seq, 0)


def pack_meta(filename: str, filesize: int) -> bytes:
    """
    Encode a META packet.
    Payload = filename bytes + filesize (8-byte big-endian uint64).
    The seq field is always 0; len field = len(filename_bytes).
    """
    name_bytes = filename.encode()
    hdr = struct.pack(HEADER_FMT, TYPE_META, 0, len(name_bytes))
    size_field = struct.pack("!Q", filesize)   # uint64, 8 bytes
    return hdr + name_bytes + size_field


def pack_fin(seq: int) -> bytes:
    """Encode a FIN packet."""
    return struct.pack(HEADER_FMT, TYPE_FIN, seq, 0)


def pack_fin_ack(seq: int) -> bytes:
    """Encode a FIN_ACK packet."""
    return struct.pack(HEADER_FMT, TYPE_FIN_ACK, seq, 0)


# ---------------------------------------------------------------------------
# Decode helper
# ---------------------------------------------------------------------------

def unpack(raw: bytes) -> Packet:
    """
    Decode a raw datagram into a Packet namedtuple.
    Returns Packet(ptype, seq, data) where data is bytes (may be empty).
    Raises ValueError on malformed input.
    """
    if len(raw) < HEADER_SIZE:
        raise ValueError(f"Packet too short: {len(raw)} bytes")
    ptype, seq, length = struct.unpack_from(HEADER_FMT, raw)
    payload = raw[HEADER_SIZE: HEADER_SIZE + length]
    return Packet(ptype=ptype, seq=seq, data=payload)


def unpack_meta_payload(pkt: Packet) -> tuple[str, int]:
    """
    Extract (filename, filesize) from a META packet's combined payload.
    The raw bytes after the header are: name_bytes + 8-byte filesize.
    pkt.data holds only the name portion (length = pkt header's len field).
    The filesize uint64 immediately follows in the original datagram, so
    we need the full datagram — but we reconstruct it from pkt fields.

    NOTE: callers should pass the *raw datagram* bytes; use unpack_meta_raw().
    """
    raise NotImplementedError("Use unpack_meta_raw(raw) instead.")


def unpack_meta_raw(raw: bytes) -> tuple[str, int]:
    """
    Extract (filename, filesize) directly from a raw META datagram.
    Layout: HEADER(7) | filename(name_len bytes) | filesize(8 bytes)
    """
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
