"""Minimal sd_notify support (no external dependency)."""

from __future__ import annotations

import os
import socket


def notify(state: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode("utf-8"))
        return True
    except OSError:
        return False


def watchdog_interval() -> float | None:
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec or not usec.isdigit():
        return None
    return int(usec) / 1_000_000.0 / 3.0
