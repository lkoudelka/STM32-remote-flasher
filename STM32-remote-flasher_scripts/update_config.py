#!/usr/bin/env python3
"""
update_config.py

Safe STM32 discovery & config update (host-side, cross-platform via Paramiko).

What it does
------------
- Connects to each configured server over SSH (Paramiko).
- Runs: STM32_Programmer_CLI -l
- Parses:
  - DFU devices (USB): "Serial number"
  - ST-LINK probes (SWD): "ST-LINK SN" + "Board Name"
- Prompts to:
  - keep existing entry, or update alias/hub_port/signals
  - add new devices
- Optional cleanup:
  - if a device in config.json is not detected, offer removal (default: no)

Notes
-----
- Hub port mapping is manual (USB1..USB16).
- Forces UTF-8 locale for STM32_Programmer_CLI:
  LC_ALL=C.UTF-8

Usage
-----
  python3 update_config.py

Config shape expected
---------------------
Uses the same config.json shape as stm_remote.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

import paramiko

CONFIG_FILE = "config.json"
HUB_PORTS = {f"USB{i}" for i in range(1, 17)}


def expand_key_path(p: str) -> str:
    return os.path.abspath(os.path.expanduser(p))


def ssh_connect(host: str, user: str, key: str) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        hostname=host,
        username=user,
        key_filename=expand_key_path(key),
        allow_agent=True,
        look_for_keys=True,
        timeout=10,
    )
    return c


def ssh_exec(client: paramiko.SSHClient, cmd: str) -> str:
    # Force UTF-8 locale to silence STM32CubeProgrammer warning
    cmd = f"LC_ALL=C.UTF-8 {cmd}"
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    _ = stdout.channel.recv_exit_status()
    return out + ("\n" + err if err else "")


def parse_stm32_list(output: str) -> List[dict]:
    """
    Parse STM32_Programmer_CLI -l output.

    Returns list of:
      {"serial": "...", "interface": "USB"|"SWD", "board": Optional[str]}
    """
    devices: List[dict] = []
    seen = set()

    current_iface: Optional[str] = None
    stlink_sn: Optional[str] = None

    for raw in output.splitlines():
        line = raw.strip()

        if "=====  DFU Interface" in line:
            current_iface = "USB"
            continue
        if "===== STLink Interface" in line:
            current_iface = "SWD"
            continue

        if current_iface == "USB" and "Serial number" in line:
            serial = line.split(":")[-1].strip()
            key = (serial, "USB")
            if key not in seen:
                seen.add(key)
                devices.append({"serial": serial, "interface": "USB", "board": None})
            continue

        if current_iface == "SWD" and "ST-LINK SN" in line:
            stlink_sn = line.split(":")[-1].strip()
            continue

        if current_iface == "SWD" and "Board Name" in line and stlink_sn:
            board = line.split(":")[-1].strip()
            key = (stlink_sn, "SWD")
            if key not in seen:
                seen.add(key)
                devices.append({"serial": stlink_sn, "interface": "SWD", "board": board})
            stlink_sn = None
            continue

    return devices


def print_device_cfg(cfg: dict) -> None:
    print("Current config:")
    for k, v in cfg.items():
        print(f"  {k:<18}: {v}")
    print()


def print_detected_info(dev: dict) -> None:
    print("\n[NEW DEVICE DETECTED]")
    if dev["interface"] == "USB":
        print("  Interface : USB DFU (ROM bootloader)")
        print(f"  Serial    : {dev['serial']}")
        print("  Info      : Serial is MCU DFU USB identifier")
    else:
        print("  Interface : SWD via ST-LINK")
        print(f"  Serial    : {dev['serial']}")
        if dev.get("board"):
            print(f"  Board     : {dev['board']}")
        print("  Info      : Serial is ST-LINK probe serial number")
    print()


def ask_hub_port(current: Optional[str]) -> Optional[str]:
    prompt = f"Hub port USB1..USB16 [{current}]: "
    while True:
        val = input(prompt).strip()
        if not val:
            return current
        if val in HUB_PORTS:
            return val
        print("Invalid hub port. Use USB1 .. USB16.")


def main() -> None:
    cfg_path = Path(CONFIG_FILE)
    cfg = json.load(open(cfg_path, "r", encoding="utf-8"))

    found_serials = set()

    for srv_name, srv in cfg.get("servers", {}).items():
        host = srv["hostnames"][0]
        user = srv.get("user", "pi")
        key = srv.get("key", "~/.ssh/id_rsa")
        stm32cli = srv.get("stm32cli", "/usr/bin/STM32_Programmer_CLI")

        print(f"[INFO] Scanning {srv_name} ({host})")
        client = ssh_connect(host, user, key)
        try:
            out = ssh_exec(client, f"{stm32cli} -l")
        finally:
            client.close()

        found = parse_stm32_list(out)
        if not found:
            print("[INFO] No STM32 devices detected")
            continue

        for devinfo in found:
            serial = devinfo["serial"]
            iface = devinfo["interface"]
            found_serials.add(serial)

            existing: Optional[Tuple[str, dict]] = None
            for alias, dev in cfg.get("devices", {}).items():
                if dev.get("serial") == serial:
                    existing = (alias, dev)
                    break

            if existing:
                alias, dev = existing
                print(f"\n[FOUND] {alias} ({iface})")
                print_device_cfg(dev)

                keep = input("Keep existing config? [Y/n]: ").strip().lower()
                if keep in ("", "y", "yes"):
                    continue
            else:
                print_detected_info(devinfo)
                alias = serial
                dev = {}

            alias_new = input(f"Alias name [{alias}]: ").strip() or alias
            hub_port = ask_hub_port(dev.get("hub_port"))
            boot = (input("Supports BOOT pin? [y/N]: ").strip().lower() in ("y", "yes"))
            nrst = (input("Supports NRST pin? [y/N]: ").strip().lower() in ("y", "yes"))

            cfg.setdefault("devices", {})[alias_new] = {
                "serial": serial,
                "server": srv_name,
                "hub_port": hub_port,
                "default_interface": iface,
                "signals": {"boot": boot, "nrst": nrst},
            }

            if alias_new != alias and alias in cfg["devices"]:
                if cfg["devices"][alias].get("serial") == serial:
                    del cfg["devices"][alias]

    missing = {
        name: dev
        for name, dev in cfg.get("devices", {}).items()
        if dev.get("serial") not in found_serials
    }
    if missing:
        ans = input("\nSome devices not detected. Review for removal? [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            for name in list(missing.keys()):
                rm = input(f"  Remove {name}? [y/N]: ").strip().lower()
                if rm in ("y", "yes"):
                    del cfg["devices"][name]

    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), indent=4)
    print("\n[INFO] config.json updated successfully.\n")


if __name__ == "__main__":
    main()
