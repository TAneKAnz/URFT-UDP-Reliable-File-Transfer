# URFT — UDP Reliable File Transfer

A reliable file transfer application built entirely on raw UDP sockets in Python. Implements **Selective Repeat ARQ** with **adaptive RTO (RFC 6298)** to guarantee correct, complete, and ordered delivery over unreliable networks.

Built for course **01076117 Computer Networks in Practice**, Computer Engineering, KMITL.

---

## Features

- **Reliable delivery** over UDP — handles packet loss, duplication, reordering, and corruption
- **Selective Repeat** sliding window — only retransmits lost packets, not the entire window
- **Adaptive timeout** — RFC 6298 RTT estimation with Karn's algorithm (no ambiguous samples)
- **Binary-safe** — transfers any file type: images, executables, archives, PDFs
- **Lightweight protocol** — 7-byte header, 5 packet types, zero external dependencies
- **Python 3.8+ compatible** — standard library only, no `pip install` required

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  urft_protocol.py                   │
│                   (134 lines)                       │
│    Constants · Packet types · Encode/Decode         │
└─────────────┬───────────────────────┬───────────────┘
              │                       │
      imports │                       │ imports
              ▼                       ▼
┌─────────────────────┐   ┌───────────────────────────┐
│   urft_client.py    │   │     urft_server.py        │
│    (232 lines)      │   │      (227 lines)          │
│                     │   │                           │
│  RTOEstimator       │   │  wait_for_meta()          │
│  send_meta()        │   │  sr_receive()             │
│  sr_send()          │   │  send_fin_ack()           │
│  send_fin()         │   │  main()                   │
└─────────────────────┘   └───────────────────────────┘
    SR Sender +                SR Receiver +
    Adaptive RTO               File Writer
```

**Total: 3 files, 593 lines**

---

## Quick Start

### 1. Start the server

```bash
python urft_server.py <server_ip> <server_port>
```

The server binds a UDP socket and waits for a client to connect. It saves the received file in the current working directory.

**Example:**
```bash
mkdir -p /tmp/received
cd /tmp/received
python urft_server.py 127.0.0.1 9000
```

```
[URFT] Server listening on 127.0.0.1:9000
[URFT] Waiting for META...
```

### 2. Send a file from the client

```bash
python urft_client.py <file_path> <server_ip> <server_port>
```

The client reads the file, splits it into 1400-byte chunks, and transfers it using Selective Repeat.

**Example:**
```bash
python urft_client.py photo.jpg 127.0.0.1 9000
```

```
[URFT] Sending 'photo.jpg' (1048576 bytes, 749 chunks) → 127.0.0.1:9000
[META] ACK received — starting data transfer.
[SR] all 749 chunks ACKed.
[URFT] Transfer done in 0.03s (40558.6 KB/s)
[URFT] FIN_ACK received — transfer complete.
```

### 3. Verify integrity

```bash
# macOS
md5 original.bin /tmp/received/original.bin

# Linux
md5sum original.bin /tmp/received/original.bin
```

Both hashes must be identical.

---

## Protocol Overview

### Packet Header (7 bytes, big-endian)

```
+----------+----------------------------+--------------------+
|  Type    |     Seq (uint32)           |   Len (uint16)     |
| (1 byte) |     (4 bytes)              |   (2 bytes)        |
+----------+----------------------------+--------------------+
```

`struct` format: `"!BIH"`

### Packet Types

| Type | Value | Description |
|------|-------|-------------|
| DATA | `0x00` | File data chunk (up to 1400 bytes payload) |
| ACK | `0x01` | Acknowledges a specific DATA packet |
| META | `0x02` | Initiates transfer (sends filename + file size) |
| FIN | `0x03` | Signals end of data transfer |
| FIN_ACK | `0x04` | Confirms FIN received |

### Transfer Flow

```
Client                                Server
  │                                     │
  │──── META (filename, size) ─────────>│   Phase 1: Handshake
  │<──── ACK(0) ───────────────────────│
  │                                     │
  │──── DATA(0) ───────────────────────>│   Phase 2: Selective Repeat
  │──── DATA(1) ───────────────────────>│
  │──── DATA(2) ─────── X              │   (lost)
  │──── DATA(3) ───────────────────────>│
  │<──── ACK(0), ACK(1), ACK(3) ──────│
  │                                     │
  │  ⏰ timeout for seq=2              │
  │──── DATA(2) ───────────────────────>│   (retransmit)
  │<──── ACK(2) ───────────────────────│
  │                                     │
  │──── FIN ───────────────────────────>│   Phase 3: Termination
  │<──── FIN_ACK ──────────────────────│
  │                                     │
  ✓ Done                                ✓ File saved
```

---

## Protocol Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `CHUNK_SIZE` | 1400 bytes | Fits within Ethernet MTU (1500) after IP/UDP/URFT headers (35 bytes) with 65-byte safety margin |
| `WINDOW_SIZE` | 128 | At 250ms RTT: 128 × 1400 / 0.25 = 700 KB/s — sufficient for all test cases |
| `INIT_TIMEOUT` | 1.0 s | RFC 6298 recommended initial RTO |
| `MAX_RETRIES` | 20 | ~160s total retry time with exponential back-off (capped at 16s) |

---

## Adaptive RTO (RFC 6298)

The timeout adapts to actual network conditions using exponential weighted moving averages:

```
SRTT   = (1 - α) × SRTT   + α × R        (α = 1/8)
RTTVAR = (1 - β) × RTTVAR + β × |SRTT-R|  (β = 1/4)
RTO    = max(0.2s, SRTT + 4 × RTTVAR)
```

**Karn's Rule:** RTT samples are only collected from non-retransmitted packets to avoid ambiguous measurements.

**Exponential back-off:** On timeout, RTO doubles (capped at 16s) to prevent retransmission storms.

---

## Edge Cases Handled

| Scenario | Solution |
|----------|----------|
| DATA packet lost | Per-packet timeout → retransmit only that chunk |
| ACK lost | Sender timeout → retransmit DATA → server re-ACKs |
| Duplicate DATA | Server re-ACKs, discards duplicate data |
| Duplicate ACK | Client ignores (already in `acked` set) |
| Out-of-order DATA | Server buffers in `recv_buffer`, delivers when gap fills |
| FIN lost | Client retransmits FIN (up to 20 times) |
| FIN_ACK lost | Server re-sends FIN_ACK during 3s grace period |
| META ACK lost | Server re-ACKs META even during data transfer phase |
| Zero-byte file | META + FIN only, no DATA phase |
| FIN before all DATA | Server ignores early FIN; client retransmits later |

---

## Test Cases

The grading system runs client and server on separate hosts connected through a router simulating network conditions. Tests are **sequential** — must pass all lower tests first.

| Test | File | RTT | C→S Condition | S→C Condition | Limit | Points |
|------|------|-----|---------------|---------------|-------|--------|
| 1 | 1 MiB | 10 ms | — | — | 30s | 3 |
| 2 | 1 MiB | 10 ms | Dup 2% | — | 30s | 3 |
| 3 | 1 MiB | 10 ms | Loss 2% | — | 30s | 3 |
| 4 | 1 MiB | 10 ms | — | Dup 5% | 30s | 3 |
| 5 | 1 MiB | 10 ms | — | Loss 5% | 30s | 2 |
| 6 | 1 MiB | 250 ms | — | — | 60s | 2 |
| 7 | 1 MiB | 250 ms | Reorder 2% | — | 90s | 2 |
| 8 | 5 MiB | 100 ms | Loss 5% | Loss 2% | 30s | 2 |

**Total: 20 points**

---

## Requirements

- Python 3.8+
- Standard library only: `os`, `sys`, `time`, `socket`, `math`, `struct`, `collections`
- Linux for grading (works on macOS/Windows for local testing)

---

## Documentation

| Document | Description |
|----------|-------------|
| [URFT_Design.md](URFT_Design.md) | Full design documentation (English) — protocol spec, algorithm details, compliance checklist, usage guide |
| [URFT_Design_TH.md](URFT_Design_TH.md) | Full design documentation (Thai) — same structure as English version |

---

## Project Constraints

| Constraint | Limit | Actual |
|------------|-------|--------|
| Source files | ≤ 5 | 3 |
| Total lines | ≤ 2000 | 593 |
| Transport | UDP only | `SOCK_DGRAM` |
| Server sockets | 1 per transfer | 1 |
| Dependencies | stdlib only | No pip packages |
| Python version | 3.8+ | `from __future__ import annotations` |

---

## License

This project is an academic assignment for course 01076117 at KMITL.
