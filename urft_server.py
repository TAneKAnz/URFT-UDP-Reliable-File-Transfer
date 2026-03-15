"""
urft_server.py — UDP Reliable File Transfer: Receiver side.

Usage:
    python urft_server.py <server_ip> <server_port>

Protocol: Selective Repeat receiver.
Writes the received file to the current working directory with its original name.
"""
from __future__ import annotations

import os
import sys
import time
import socket

from urft_protocol import (
    CHUNK_SIZE, WINDOW_SIZE, INIT_TIMEOUT, MAX_RETRIES,
    TYPE_DATA, TYPE_META, TYPE_FIN,
    pack_ack, pack_fin_ack,
    unpack, unpack_meta_raw,
)


# ---------------------------------------------------------------------------
# Handshake: wait for META, reply ACK(0)
# ---------------------------------------------------------------------------

def wait_for_meta(sock: socket.socket) -> tuple[str, int, tuple]:
    """
    Block until a META packet arrives.
    Returns (filename, filesize, client_addr).
    """
    print("[URFT] Waiting for META...", flush=True)
    while True:
        raw, addr = sock.recvfrom(65535)
        try:
            filename, filesize = unpack_meta_raw(raw)
        except (ValueError, Exception):
            continue   # ignore malformed or non-META packets
        # Send ACK(0) and remember client
        sock.sendto(pack_ack(0), addr)
        print(f"[META] filename='{filename}', filesize={filesize} bytes, "
              f"client={addr}", flush=True)
        return filename, filesize, addr


# ---------------------------------------------------------------------------
# Selective Repeat Receiver
# ---------------------------------------------------------------------------

def sr_receive(sock: socket.socket, client_addr: tuple,
               total_chunks: int) -> tuple[bytearray, int | None]:
    """
    Receive all data chunks via Selective Repeat.
    Returns assembled file data as a bytearray.
    """
    expected     = 0                    # next in-order seq
    recv_buffer  = {}                   # seq -> bytes (out-of-order buffer)
    file_data    = bytearray()
    fin_received = False
    fin_seq      = None

    sock.settimeout(30.0)   # generous overall guard

    while not fin_received:
        try:
            raw, addr = sock.recvfrom(65535)
        except socket.timeout:
            print("[SR] socket timeout waiting for data", flush=True)
            break

        if addr != client_addr:
            continue   # ignore stray packets

        try:
            pkt = unpack(raw)
        except ValueError:
            continue

        if pkt.ptype == TYPE_DATA:
            seq = pkt.seq

            # Duplicate or already-delivered packet: re-ACK to help client advance
            if seq < expected:
                sock.sendto(pack_ack(seq), client_addr)
                continue

            # In window: buffer and ACK
            if seq < expected + WINDOW_SIZE:
                if seq not in recv_buffer:
                    recv_buffer[seq] = pkt.data
                sock.sendto(pack_ack(seq), client_addr)

                # Deliver consecutive buffered data in order
                while expected in recv_buffer:
                    file_data.extend(recv_buffer.pop(expected))
                    expected += 1

            # else: out of window — discard silently

        elif pkt.ptype == TYPE_META:
            # Client retransmitted META (our ACK(0) was lost) — re-ACK
            sock.sendto(pack_ack(0), client_addr)

        elif pkt.ptype == TYPE_FIN:
            fin_seq = pkt.seq
            # Only accept FIN after all data chunks have been delivered
            if expected >= total_chunks:
                fin_received = True
            # else: ignore early FIN; client will retransmit it

    # Drain any remaining buffered data
    while expected in recv_buffer:
        file_data.extend(recv_buffer.pop(expected))
        expected += 1

    return file_data, fin_seq


# ---------------------------------------------------------------------------
# Termination: send FIN_ACK (retransmit if duplicate FIN arrives)
# ---------------------------------------------------------------------------

def send_fin_ack(sock: socket.socket, client_addr: tuple, fin_seq: int) -> None:
    """
    Send FIN_ACK and hold the socket open briefly to re-send if the
    client retransmits FIN (meaning our FIN_ACK was lost).
    """
    fin_ack_pkt = pack_fin_ack(fin_seq)
    sock.sendto(fin_ack_pkt, client_addr)

    # Listen for up to ~3 s in case client resends FIN
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        sock.settimeout(max(0.05, remaining))
        try:
            raw, addr = sock.recvfrom(65535)
        except socket.timeout:
            break
        if addr != client_addr:
            continue
        try:
            pkt = unpack(raw)
        except ValueError:
            continue
        if pkt.ptype == TYPE_FIN:
            # Client re-sent FIN → our FIN_ACK was lost, resend
            sock.sendto(fin_ack_pkt, client_addr)
            print("[FIN] re-sent FIN_ACK (duplicate FIN received)", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python urft_server.py <server_ip> <server_port>")
        sys.exit(1)

    server_ip   = sys.argv[1]
    server_port = int(sys.argv[2])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((server_ip, server_port))
    print(f"[URFT] Server listening on {server_ip}:{server_port}", flush=True)

    try:
        # 1. Wait for META handshake
        filename, filesize, client_addr = wait_for_meta(sock)

        # 2. Selective Repeat data receive
        t0           = time.monotonic()
        total_chunks = (filesize + CHUNK_SIZE - 1) // CHUNK_SIZE if filesize > 0 else 0

        if total_chunks > 0:
            file_data, fin_seq = sr_receive(sock, client_addr, total_chunks)
        else:
            # Zero-byte file: just wait for FIN
            file_data = bytearray()
            fin_seq   = 0
            sock.settimeout(10.0)
            while True:
                try:
                    raw, addr = sock.recvfrom(65535)
                except socket.timeout:
                    break
                if addr != client_addr:
                    continue
                try:
                    pkt = unpack(raw)
                except ValueError:
                    continue
                if pkt.ptype == TYPE_FIN:
                    fin_seq = pkt.seq
                    break

        elapsed = time.monotonic() - t0

        # 3. Write file
        out_path = os.path.join(os.getcwd(), filename)
        with open(out_path, "wb") as f:
            f.write(file_data)

        received = len(file_data)
        print(f"[URFT] File saved: '{out_path}' ({received} bytes) "
              f"in {elapsed:.2f}s", flush=True)

        if received != filesize:
            print(f"[WARN] Expected {filesize} bytes, got {received} bytes",
                  flush=True)

        # 4. Send FIN_ACK
        if fin_seq is not None:
            send_fin_ack(sock, client_addr, fin_seq)
            print("[URFT] FIN_ACK sent — done.", flush=True)

    finally:
        sock.close()


if __name__ == "__main__":
    main()
