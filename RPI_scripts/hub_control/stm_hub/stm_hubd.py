#!/usr/bin/env python3
"""
stm_hubd.py  (Raspberry Pi)

TCP hub control daemon for faster hub operations than calling hub_cli.py over SSH.

Why
---
Calling "python3 hub_cli.py ..." via SSH for every on/off can be slow due to:
- SSH process startup
- Python interpreter startup
- module import time

This daemon keeps the hub modules loaded and serves requests quickly.

Security model
--------------
Bind to 127.0.0.1 only. Access from the host PC is done through an SSH tunnel
(Paramiko direct-tcpip). Do NOT bind to 0.0.0.0 on an untrusted network.

Protocol
--------
Line-based:
  request: "<command> <arg0> <arg1> ...\\n"
  response: "OK <text>\\n" or "ERR <text>\\n"

Commands (must match Hub capabilities)
-------------------------------------
power_on <hex_mask>
power_off <hex_mask>
set_power_state <hex_mask>
get_power_state

boot_on <hex_mask>
boot_off <hex_mask>
set_boot_state <hex_mask>
get_boot_state

nrst_on <hex_mask>
nrst_off <hex_mask>
set_nrst_state <hex_mask>
get_nrst_state

stlink_mux <value>          (implementation-specific; forwarded to Hub.stlink_mux)
get_stlink_mux

It reuses your existing stm_hub files in the same directory:
  - hub.py
  - hub_lock.py
  - config.json
"""

from __future__ import annotations

import argparse
import socket
import threading
from typing import Callable, Any

from hub import Hub
from hub_lock import HubLock


def _parse_mask(s: str) -> int:
    """
    Parse hex mask like '0x0004' or '0004' (hex).
    Keeps compatibility with hub_cli.py which uses int(arg, 16).
    """
    s = s.strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    return int(s, 16)


def _parse_int_auto(s: str) -> int:
    """Parse integer in decimal or 0x.. hex."""
    return int(s.strip(), 0)


def _ok(conn: socket.socket, payload: Any) -> None:
    conn.sendall(("OK " + str(payload) + "\n").encode("utf-8"))


def _err(conn: socket.socket, msg: str) -> None:
    conn.sendall(("ERR " + msg + "\n").encode("utf-8"))


def handle_client(conn: socket.socket, hub: Hub) -> None:
    try:
        # Read one line
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk

        line = data.decode("utf-8", errors="replace").strip()
        if not line:
            _err(conn, "empty")
            return

        parts = line.split()
        cmd = parts[0]
        args = parts[1:]

        # Dispatch table: cmd -> (callable, arg_parser or None)
        # We keep parsing rules compatible with your existing hub_cli.py.
        def call_noargs(fn: Callable[[], Any]) -> Any:
            return fn()

        def call_mask(fn: Callable[[int], Any]) -> Any:
            if len(args) != 1:
                raise ValueError("expected_1_arg")
            return fn(_parse_mask(args[0]))

        def call_int(fn: Callable[[int], Any]) -> Any:
            if len(args) != 1:
                raise ValueError("expected_1_arg")
            return fn(_parse_int_auto(args[0]))

        with HubLock():
            # Power
            if cmd == "power_on":
                _ok(conn, call_mask(hub.power_on))
            elif cmd == "power_off":
                _ok(conn, call_mask(hub.power_off))
            elif cmd == "set_power_state":
                _ok(conn, call_mask(hub.set_power_state))
            elif cmd == "get_power_state":
                _ok(conn, call_noargs(hub.get_power_state))

            # BOOT
            elif cmd == "boot_on":
                _ok(conn, call_mask(hub.boot_on))
            elif cmd == "boot_off":
                _ok(conn, call_mask(hub.boot_off))
            elif cmd == "set_boot_state":
                _ok(conn, call_mask(hub.set_boot_state))
            elif cmd == "get_boot_state":
                _ok(conn, call_noargs(hub.get_boot_state))

            # NRST
            elif cmd == "nrst_on":
                _ok(conn, call_mask(hub.nrst_on))
            elif cmd == "nrst_off":
                _ok(conn, call_mask(hub.nrst_off))
            elif cmd == "set_nrst_state":
                _ok(conn, call_mask(hub.set_nrst_state))
            elif cmd == "get_nrst_state":
                _ok(conn, call_noargs(hub.get_nrst_state))

            # ST-LINK mux (implementation-specific)
            elif cmd == "stlink_mux":
                # Some hubs treat this as selecting one target (int).
                # If your Hub expects a different type, adjust here.
                _ok(conn, call_int(hub.stlink_mux))
            elif cmd == "get_stlink_mux":
                _ok(conn, call_noargs(hub.get_stlink_mux))

            else:
                _err(conn, "unknown_command")

    except Exception as e:
        try:
            _err(conn, str(e))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="127.0.0.1", help="Bind address (default: localhost only)")
    ap.add_argument("--port", type=int, default=9999, help="TCP port (default: 9999)")
    args = ap.parse_args()

    hub = Hub()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(50)
    print(f"[hubd] listening on {args.bind}:{args.port}")

    while True:
        conn, _addr = srv.accept()
        t = threading.Thread(target=handle_client, args=(conn, hub), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
