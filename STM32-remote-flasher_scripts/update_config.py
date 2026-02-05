#!/usr/bin/env python3
"""
update_config.py
Safe STM32 discovery & config update

Discovers STM32 devices connected to each configured server (Raspberry Pi),
using STM32_Programmer_CLI -l, then updates config.json interactively.

Key points
----------
- Uses SSH (subprocess) to run STM32_Programmer_CLI -l remotely.
- Forces UTF-8 locale remotely to silence Qt locale warnings.
- Detects:
  - USB DFU devices (MCU DFU serial)
  - SWD devices via ST-LINK (ST-LINK probe SN + Board Name)
- Prevents duplicates (common due to multiple interfaces / repeated lines).
- For existing devices:
  - Shows current config and asks whether to keep or edit.
- For new devices:
  - Prints a short "what is this serial" explanation and board name if present.
- Offers optional removal of not-detected devices (default: no).

Usage
-----
    python3 update_config.py
"""

import json
import os
import subprocess
from pathlib import Path

CONFIG_FILE = "config.json"

# Valid hub ports (manual entry)
HUB_PORTS = {f"USB{i}" for i in range(1, 17)}


def run_ssh(host: str, user: str, key: str, cmd: str) -> str:
    """
    Run a remote command via SSH and return stdout as text.
    Forces UTF-8 locale to avoid STM32CubeProgrammer locale warnings.
    """
    key = os.path.expanduser(key)
    cmd = f"LC_ALL=C.UTF-8 {cmd}"

    full = ["ssh", "-i", key, f"{user}@{host}", cmd]
    return subprocess.check_output(full, text=True)


def parse_stm32_list(output: str) -> list[dict]:
    """
    Parse STM32_Programmer_CLI -l output.

    Returns a list of:
      {
        "serial": <str>,
        "interface": "USB" | "SWD",
        "board": <str|None>   # for SWD/ST-LINK only
      }

    Notes:
    - DFU serial comes from "Serial number"
    - ST-LINK serial comes from "ST-LINK SN"
    - Board name comes from "Board Name"
    - Duplicates can occur; we dedupe by (serial, interface)
    """
    devices: list[dict] = []
    seen = set()

    current_iface = None
    current_stlink_sn = None

    for raw in output.splitlines():
        line = raw.strip()

        # Section detection
        if "=====  DFU Interface" in line:
            current_iface = "USB"
            continue
        if "===== STLink Interface" in line:
            current_iface = "SWD"
            continue

        # DFU: MCU serial
        if current_iface == "USB" and "Serial number" in line:
            serial = line.split(":")[-1].strip()
            key = (serial, "USB")
            if key not in seen:
                seen.add(key)
                devices.append({"serial": serial, "interface": "USB", "board": None})
            continue

        # ST-LINK: probe serial (board name may come later)
        if current_iface == "SWD" and "ST-LINK SN" in line:
            current_stlink_sn = line.split(":")[-1].strip()
            continue

        # ST-LINK: board name finalizes a SWD entry
        if current_iface == "SWD" and "Board Name" in line and current_stlink_sn:
            board = line.split(":")[-1].strip()
            key = (current_stlink_sn, "SWD")
            if key not in seen:
                seen.add(key)
                devices.append({"serial": current_stlink_sn, "interface": "SWD", "board": board})
            current_stlink_sn = None
            continue

    return devices


def print_device_cfg(cfg: dict) -> None:
    """Pretty-print one device config block."""
    print("Current config:")
    for k, v in cfg.items():
        print(f"  {k:<18}: {v}")
    print()


def print_detected_info(dev: dict) -> None:
    """Print short human-friendly description for a newly detected device."""
    print("\n[NEW DEVICE DETECTED]")

    iface_desc = "USB DFU (ROM bootloader)" if dev["interface"] == "USB" else "SWD via ST-LINK"
    print(f"  Interface : {iface_desc}")
    print(f"  Serial    : {dev['serial']}")

    if dev.get("board"):
        print(f"  Board     : {dev['board']}")

    if dev["interface"] == "SWD":
        print("  Info      : Serial is ST-LINK probe serial number")
    else:
        print("  Info      : Serial is MCU DFU USB identifier")

    print()


def ask_hub_port(current: str | None) -> str | None:
    """
    Ask user for hub port label USB1..USB16, keeping current on empty input.
    """
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
    cfg = json.load(open(cfg_path))

    # Track every serial observed across all scanned servers this run
    found_serials = set()

    for srv_name, srv in cfg["servers"].items():
        host = srv["hostnames"][0]
        print(f"[INFO] Scanning {host}")

        out = run_ssh(
            host,
            srv["user"],
            srv["key"],
            f'{srv["stm32cli"]} -l'
        )

        found = parse_stm32_list(out)

        if not found:
            print("[INFO] No STM32 devices detected")
            continue

        for devinfo in found:
            serial = devinfo["serial"]
            iface = devinfo["interface"]
            found_serials.add(serial)

            # Find existing device by serial (alias is key name in cfg["devices"])
            existing = None
            for alias, dev in cfg["devices"].items():
                if dev["serial"] == serial:
                    existing = (alias, dev)
                    break

            if existing:
                alias, dev = existing
                print(f"\n[FOUND] {alias} ({iface})")
                print_device_cfg(dev)

                keep = input("Keep existing config? [Y/n]: ").strip().lower()
                if keep in ("", "y"):
                    continue
            else:
                # New device: provide context before asking for alias
                print_detected_info(devinfo)
                alias = serial
                dev = {}

            # --- user input for (new or edited) device ---
            alias = input(f"Alias name [{alias}]: ").strip() or alias
            hub_port = ask_hub_port(dev.get("hub_port"))
            boot = input("Supports BOOT pin? [y/N]: ").lower() == "y"
            nrst = input("Supports NRST pin? [y/N]: ").lower() == "y"

            cfg["devices"][alias] = {
                "serial": serial,
                "server": srv_name,
                "hub_port": hub_port,
                "default_interface": iface,
                "signals": {"boot": boot, "nrst": nrst},
            }

    # Offer optional removal of devices not detected in this scan
    missing = {
        name: dev
        for name, dev in cfg["devices"].items()
        if dev["serial"] not in found_serials
    }

    if missing:
        ans = input("\nSome devices not detected. Review for removal? [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            for name in list(missing):
                rm = input(f"  Remove {name}? [y/N]: ").strip().lower()
                if rm in ("y", "yes"):
                    del cfg["devices"][name]

    json.dump(cfg, open(cfg_path, "w"), indent=4)
    print("\n[INFO] config.json updated successfully.\n")


if __name__ == "__main__":
    main()

