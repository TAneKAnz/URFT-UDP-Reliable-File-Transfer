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


def wait_for_meta(sock: socket.socket) -> tuple[str, int, tuple]:
    print("[URFT] Waiting for META...", flush=True)
    while True:
        raw, addr = sock.recvfrom(65535)
        try:
            filename, filesize = unpack_meta_raw(raw)
        except (ValueError, Exception):
            continue
        sock.sendto(pack_ack(0), addr)
        print(f"[META] filename='{filename}', filesize={filesize} bytes, "
              f"client={addr}", flush=True)
        return filename, filesize, addr


def sr_receive(sock: socket.socket, client_addr: tuple,
               total_chunks: int) -> tuple[bytearray, int | None]:
    expected     = 0
    recv_buffer  = {}
    file_data    = bytearray()
    fin_received = False
    fin_seq      = None

    sock.settimeout(30.0)

    while not fin_received:
        try:
            raw, addr = sock.recvfrom(65535)
        except socket.timeout:
            print("[SR] socket timeout waiting for data", flush=True)
            break

        if addr != client_addr:
            continue

        try:
            pkt = unpack(raw)
        except ValueError:
            continue

        if pkt.ptype == TYPE_DATA:
            seq = pkt.seq

            if seq < expected:
                sock.sendto(pack_ack(seq), client_addr)
                continue

            if seq < expected + WINDOW_SIZE:
                if seq not in recv_buffer:
                    recv_buffer[seq] = pkt.data
                sock.sendto(pack_ack(seq), client_addr)

                while expected in recv_buffer:
                    file_data.extend(recv_buffer.pop(expected))
                    expected += 1

        elif pkt.ptype == TYPE_META:
            # client resent META (our ACK was lost)
            sock.sendto(pack_ack(0), client_addr)

        elif pkt.ptype == TYPE_FIN:
            fin_seq = pkt.seq
            if expected >= total_chunks:
                fin_received = True

    # drain remaining buffer
    while expected in recv_buffer:
        file_data.extend(recv_buffer.pop(expected))
        expected += 1

    return file_data, fin_seq


def send_fin_ack(sock: socket.socket, client_addr: tuple, fin_seq: int) -> None:
    fin_ack_pkt = pack_fin_ack(fin_seq)
    sock.sendto(fin_ack_pkt, client_addr)

    # wait a bit in case client resends FIN (our FIN_ACK was lost)
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
            sock.sendto(fin_ack_pkt, client_addr)
            print("[FIN] re-sent FIN_ACK (duplicate FIN received)", flush=True)


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
        filename, filesize, client_addr = wait_for_meta(sock)

        t0           = time.monotonic()
        total_chunks = (filesize + CHUNK_SIZE - 1) // CHUNK_SIZE if filesize > 0 else 0

        if total_chunks > 0:
            file_data, fin_seq = sr_receive(sock, client_addr, total_chunks)
        else:
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

        out_path = os.path.join(os.getcwd(), filename)
        with open(out_path, "wb") as f:
            f.write(file_data)

        received = len(file_data)
        print(f"[URFT] File saved: '{out_path}' ({received} bytes) "
              f"in {elapsed:.2f}s", flush=True)

        if received != filesize:
            print(f"[WARN] Expected {filesize} bytes, got {received} bytes",
                  flush=True)

        if fin_seq is not None:
            send_fin_ack(sock, client_addr, fin_seq)
            print("[URFT] FIN_ACK sent — done.", flush=True)

    finally:
        sock.close()


if __name__ == "__main__":
    main()
