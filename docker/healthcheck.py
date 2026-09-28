#!/usr/bin/env python3
"""Docker HEALTHCHECK for the VoiceRT engine bridge.

A bare TCP connect proves only that something accepted a socket. This
speaks the real protocol instead: it sends a HELLO frame and requires a
READY frame back, which means the accept loop ran, ``Hello.parse``
succeeded, ``runtime.start()`` brought a pipeline up, and the writer
pump drained a frame to the wire. That is the whole handshake a Unity
NPC performs on connect.

Exit 0 = healthy, exit 1 = unhealthy. Nothing else is installed: this
uses stdlib only, same as the bridge core.

A "server full" ERROR also counts as healthy. Every NPC slot being busy
is a capacity fact, not a liveness fault -- the server still parsed our
frame and answered, which is exactly what the check is asking.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys

HEADER = struct.Struct("!BI")  # type (1B) + length (4B big-endian)
T_HELLO = 0x01
T_READY = 0x81
T_ERROR = 0x8F

PROBE_ID = "docker-healthcheck"


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"peer closed after {len(buf)}/{n} bytes")
        buf += chunk
    return bytes(buf)


def probe(host: str, port: int, timeout: float) -> str:
    payload = json.dumps(
        {"proto": 1, "npc_id": PROBE_ID, "character": "", "lore_scope": "", "voice": "default"},
        ensure_ascii=False,
    ).encode("utf-8")

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(HEADER.pack(T_HELLO, len(payload)) + payload)
        ftype, length = HEADER.unpack(_recv_exact(sock, HEADER.size))
        body = _recv_exact(sock, length) if length else b""

    if ftype == T_READY:
        obj = json.loads(body.decode("utf-8") or "{}")
        if obj.get("npc_id") != PROBE_ID:
            raise ValueError(f"READY for the wrong npc_id: {obj.get('npc_id')!r}")
        return (
            f"READY npc_id={obj.get('npc_id')} "
            f"sample_rate={obj.get('sample_rate')} "
            f"channels={obj.get('channels')} format={obj.get('format')}"
        )

    if ftype == T_ERROR:
        message = json.loads(body.decode("utf-8") or "{}").get("message", "")
        if "full" in message.lower():
            return f"ERROR but alive: {message}"
        raise ValueError(f"server ERROR: {message}")

    raise ValueError(f"unexpected frame type {ftype:#04x} ({length} bytes)")


def main() -> int:
    host = os.environ.get("VOICERT_HEALTH_HOST", "127.0.0.1")
    port = int(os.environ.get("VOICERT_PORT", "8765"))
    timeout = float(os.environ.get("VOICERT_HEALTH_TIMEOUT", "4"))
    try:
        print(f"healthy: {probe(host, port, timeout)}")
        return 0
    except Exception as exc:  # noqa: BLE001 - any failure is an unhealthy container
        print(f"unhealthy: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
