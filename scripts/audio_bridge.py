#!/usr/bin/env python3
"""Playback fallback. Runs as the PipeWire session user.

The soundcore worker connects to 127.0.0.1:8743 and sends one header line:

    RATE 16000 SINK bluez_output.AA.1

then raw s16le mono PCM until it closes the socket. Each connection is one
paplay process, so PipeWire mixes concurrent routes.
"""

from __future__ import annotations

import socket
import subprocess

HOST = "127.0.0.1"
PORT = 8743


def _header(line: str) -> tuple[int, str | None]:
    rate = 16000
    sink = None
    pieces = line.strip().split()
    if "RATE" in pieces:
        rate = int(pieces[pieces.index("RATE") + 1])
    if "SINK" in pieces:
        sink = pieces[pieces.index("SINK") + 1]
        if sink == "default":
            sink = None
    return rate, sink


def _serve(conn: socket.socket) -> None:
    buffered = b""
    while b"\n" not in buffered:
        chunk = conn.recv(256)
        if not chunk:
            return
        buffered += chunk
    line, rest = buffered.split(b"\n", 1)
    rate, sink = _header(line.decode("utf-8", errors="replace"))
    command = [
        "paplay",
        "--raw",
        "--format=s16le",
        f"--rate={rate}",
        "--channels=1",
        "--latency-msec=200",
    ]
    if sink:
        command.append(f"--device={sink}")
    proc = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        if rest and proc.stdin:
            proc.stdin.write(rest)
        while True:
            chunk = conn.recv(4096)
            if not chunk or proc.stdin is None:
                break
            proc.stdin.write(chunk)
            proc.stdin.flush()
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=2)


def main() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(8)
    while True:
        conn, _addr = server.accept()
        try:
            _serve(conn)
        except Exception:
            pass
        finally:
            conn.close()


if __name__ == "__main__":
    main()
