from __future__ import annotations

import os
import sys
import time
import socket
import math

from urft_protocol import (
    CHUNK_SIZE, WINDOW_SIZE, INIT_TIMEOUT, MAX_RETRIES,
    TYPE_ACK, TYPE_FIN_ACK,
    pack_meta, pack_data, pack_ack, pack_fin,
    unpack,
)


class RTOEstimator:
    def __init__(self, init_rto: float = INIT_TIMEOUT):
        self.srtt   = None
        self.rttvar = None
        self.rto    = init_rto

    def update(self, sample: float) -> None:
        if self.srtt is None:
            self.srtt   = sample
            self.rttvar = sample / 2.0
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - sample)
            self.srtt   = 0.875 * self.srtt  + 0.125 * sample
        self.rto = max(0.2, self.srtt + 4.0 * self.rttvar)

    def backoff(self) -> None:
        self.rto = min(self.rto * 2.0, 16.0)


def send_meta(sock: socket.socket, addr: tuple, filename: str, filesize: int,
              rto_est: RTOEstimator) -> None:
    meta_pkt = pack_meta(os.path.basename(filename), filesize)
    for attempt in range(MAX_RETRIES):
        sock.sendto(meta_pkt, addr)
        sock.settimeout(rto_est.rto)
        try:
            raw, _ = sock.recvfrom(65535)
            pkt = unpack(raw)
            if pkt.ptype == TYPE_ACK and pkt.seq == 0:
                return
        except socket.timeout:
            rto_est.backoff()
            print(f"[META] timeout (attempt {attempt+1}), RTO={rto_est.rto:.3f}s", flush=True)
    raise RuntimeError("META handshake failed after max retries")


def sr_send(sock: socket.socket, addr: tuple, chunks: list[bytes],
            rto_est: RTOEstimator) -> None:
    total       = len(chunks)
    base        = 0
    next_seq    = 0
    acked       = set()
    send_time   = {}
    retries     = {}
    is_retransmit = set()

    def send_chunk(seq: int, is_retry: bool = False) -> None:
        sock.sendto(pack_data(seq, chunks[seq]), addr)
        send_time[seq] = time.monotonic()
        if is_retry:
            is_retransmit.add(seq)
        else:
            is_retransmit.discard(seq)

    while base < total:
        # fill window
        while next_seq < total and next_seq < base + WINDOW_SIZE:
            send_chunk(next_seq)
            retries[next_seq] = 0
            next_seq += 1

        # find tightest deadline
        now     = time.monotonic()
        timeout = rto_est.rto
        for seq in range(base, min(next_seq, base + WINDOW_SIZE)):
            if seq not in acked:
                remaining = rto_est.rto - (now - send_time.get(seq, now))
                timeout = max(0.001, min(timeout, remaining))

        sock.settimeout(timeout)
        try:
            raw, _ = sock.recvfrom(65535)
            pkt = unpack(raw)
            if pkt.ptype != TYPE_ACK:
                continue

            seq = pkt.seq
            if seq < base or seq >= base + WINDOW_SIZE + total:
                continue

            if seq not in acked:
                acked.add(seq)
                # update RTT only for non-retransmitted packets
                if seq not in is_retransmit and seq in send_time:
                    rtt = time.monotonic() - send_time[seq]
                    rto_est.update(rtt)

            # advance base
            while base < total and base in acked:
                base += 1

        except socket.timeout:
            now = time.monotonic()
            pre_backoff_rto = rto_est.rto
            rto_est.backoff()
            for seq in range(base, min(next_seq, base + WINDOW_SIZE)):
                if seq not in acked:
                    elapsed = now - send_time.get(seq, 0)
                    if elapsed >= pre_backoff_rto * 0.8:
                        retries[seq] = retries.get(seq, 0) + 1
                        if retries[seq] > MAX_RETRIES:
                            raise RuntimeError(
                                f"Packet {seq} exceeded MAX_RETRIES={MAX_RETRIES}"
                            )
                        send_chunk(seq, is_retry=True)

    print(f"[SR] all {total} chunks ACKed.", flush=True)


def send_fin(sock: socket.socket, addr: tuple, total_chunks: int,
             rto_est: RTOEstimator) -> None:
    fin_pkt = pack_fin(total_chunks)
    for attempt in range(MAX_RETRIES):
        sock.sendto(fin_pkt, addr)
        sock.settimeout(rto_est.rto)
        try:
            raw, _ = sock.recvfrom(65535)
            pkt = unpack(raw)
            if pkt.ptype == TYPE_FIN_ACK:
                return
        except socket.timeout:
            rto_est.backoff()
            print(f"[FIN] timeout (attempt {attempt+1}), RTO={rto_est.rto:.3f}s",
                  flush=True)
    raise RuntimeError("FIN handshake failed after max retries")


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python urft_client.py <file_path> <server_ip> <server_port>")
        sys.exit(1)

    file_path   = sys.argv[1]
    server_ip   = sys.argv[2]
    server_port = int(sys.argv[3])
    addr        = (server_ip, server_port)

    if not os.path.isfile(file_path):
        print(f"Error: file not found: {file_path}")
        sys.exit(1)

    filesize = os.path.getsize(file_path)
    with open(file_path, "rb") as f:
        raw_data = f.read()

    total_chunks = math.ceil(filesize / CHUNK_SIZE) if filesize > 0 else 0
    chunks = [
        raw_data[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
        for i in range(total_chunks)
    ]

    print(f"[URFT] Sending '{file_path}' ({filesize} bytes, "
          f"{total_chunks} chunks) → {server_ip}:{server_port}", flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        rto_est = RTOEstimator()

        send_meta(sock, addr, file_path, filesize, rto_est)
        print("[META] ACK received — starting data transfer.", flush=True)

        t0 = time.monotonic()
        if total_chunks > 0:
            sr_send(sock, addr, chunks, rto_est)
        elapsed = time.monotonic() - t0
        if elapsed > 0:
            throughput = filesize / elapsed / 1024
            print(f"[URFT] Transfer done in {elapsed:.2f}s "
                  f"({throughput:.1f} KB/s)", flush=True)

        send_fin(sock, addr, total_chunks, rto_est)
        print("[URFT] FIN_ACK received — transfer complete.", flush=True)

    finally:
        sock.close()


if __name__ == "__main__":
    main()
