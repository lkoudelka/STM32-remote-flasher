#!/usr/bin/env python3
"""
stm_remote.py

Remote STM32 flashing via a Raspberry Pi "server" that has:
- STM32CubeProgrammer CLI installed (STM32_Programmer_CLI)
- (Optional) USB hub power control CLI (hub_cli.py)

Main features
-------------
- Flash devices over SWD (ST-LINK) or USB DFU based on config.json
- DFU recovery: if DFU flashing fails, attempt recovery via:
  - NRST pulse (placeholder; no physical control implemented here)
  - or hub port power-cycle if server supports it
- Optional parallel flashing per server using max_parallel

Usage examples
--------------
Flash two devices on server "pi1":
    python3 stm_remote.py -v 1 flash_auto pi1 dfu1=bin/fw.hex g0=bin/fw2.hex

Send hub command as-is to remote hub_cli.py:
    python3 stm_remote.py -v 2 hub pi1 power_off 0x0004

Verbosity
---------
- -v 0 : minimal output
- -v 1 : info output
- -v 2 : debug SSH/SCP commands
- -v 3 : includes full STM32_Programmer_CLI output
"""

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CONFIG_FILE = "config.json"
REMOTE_TMP = "/tmp"


def hub_port_to_mask(port: str) -> int:
    """
    Convert "USBn" hub port label to bitmask used by hub_cli.py.

    USB1  -> 0x0001
    USB2  -> 0x0002
    USB3  -> 0x0004
    ...
    USB16 -> 0x8000
    """
    n = int(port.replace("USB", ""))
    return 1 << (n - 1)


def load_config() -> dict:
    """Load config.json."""
    with open(CONFIG_FILE) as f:
        return json.load(f)


def ssh(server: dict, cmd: str, verbose: int = 0, check: bool = True) -> str:
    """
    Run a remote command over SSH.
    Returns stdout (text). If check=True, non-zero return code raises RuntimeError.
    """
    if verbose >= 2:
        print(f"[DEBUG] SSH: {cmd}")

    p = subprocess.run(
        [
            "ssh",
            "-i", server["key"],
            f'{server["user"]}@{server["hostnames"][0]}',
            cmd,
        ],
        capture_output=True,
        text=True,
    )

    if check and p.returncode != 0:
        raise RuntimeError(p.stderr)

    return p.stdout


def scp(server: dict, local: str, remote: str, verbose: int) -> None:
    """
    Copy a file to the remote server via SCP.
    """
    if verbose >= 2:
        print(f"[DEBUG] SCP {local} -> {remote}")

    subprocess.check_call(
        [
            "scp",
            "-i", server["key"],
            local,
            f'{server["user"]}@{server["hostnames"][0]}:{remote}',
        ]
    )


def flash_device(server: dict, dev: dict, fw: str, verbose: int, alias: str) -> bool:
    """
    Flash one device defined in config.json.

    Returns True if flashing succeeded (initial or recovery), otherwise False.
    Always tries to remove the transferred firmware file from remote tmp.
    """

    def log(level: str, msg: str) -> None:
        # Prefix logs with device alias so output stays readable in parallel runs
        print(f"[{level}][{alias}] {msg}")

    serial = dev["serial"]
    iface = dev["default_interface"]  # "USB" or "SWD"
    hub_port = dev.get("hub_port")    # "USBn" or None
    nrst = dev["signals"].get("nrst", False)

    # Upload firmware to remote temporary location
    remote_fw = f"{REMOTE_TMP}/{Path(fw).name}"
    scp(server, fw, remote_fw, verbose)

    def attempt(tag: str) -> bool:
        """
        One programming attempt (initial or recovery).
        Returns True if STM32_Programmer_CLI output does not contain "Error".
        """
        if verbose:
            log("INFO", f"Flash attempt ({tag})")

        port_arg = "USB" if iface == "USB" else "SWD"
        cmd = (
            f'{server["stm32cli"]} '
            f'-c port={port_arg} SN={serial} '
            f'-w {remote_fw} -v -g'
        )

        out = ssh(server, cmd, verbose, check=False)

        if verbose >= 3:
            # Raw STM32CubeProgrammer output
            print(out)

        return "Error" not in out

    # ---------------- initial attempt ----------------
    if attempt("initial"):
        log("INFO", "Flash successful (initial)")
        ssh(server, f"rm -f {remote_fw}", verbose, check=False)
        return True

    log("WARN", "Flash failed (initial)")

    # ---------------- DFU recovery path ----------------
    # Only USB DFU devices have a recovery flow here.
    if iface == "USB":
        log("WARN", "DFU not visible, attempting recovery")

        if nrst:
            # NOTE: This is a placeholder. No actual NRST control is implemented here.
            # Kept for forward compatibility with external GPIO/reset control.
            if verbose:
                log("INFO", "NRST pulse")
            time.sleep(0.02)

        elif server.get("power_control") and hub_port:
            # Hub power-cycle (mask-based)
            mask = hub_port_to_mask(hub_port)
            log("INFO", f"Power-cycling {hub_port}")
            ssh(server, f'{server["hub_cli"]} power_off 0x{mask:04X}', verbose)
            time.sleep(0.1)
            ssh(server, f'{server["hub_cli"]} power_on 0x{mask:04X}', verbose)
            time.sleep(0.1)

        ok = attempt("recovery")

        if ok:
            log("INFO", "Flash successful (recovery)")
        else:
            log("ERROR", "Flash failed (recovery)")

        ssh(server, f"rm -f {remote_fw}", verbose, check=False)
        return ok

    # Non-USB device and initial failed -> done
    ssh(server, f"rm -f {remote_fw}", verbose, check=False)
    return False


def flash_task(server: dict, devices: dict, name: str, fw: str, verbose: int) -> bool:
    """
    Thread-pool wrapper that flashes one device by alias name.
    """
    print(f"[INFO] Flashing {name}")
    ok = flash_device(server, devices[name], fw, verbose, name)
    if not ok:
        print(f"[ERROR][{name}] Flash failed")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", type=int, default=0, help="Verbosity level (0-3)")
    ap.add_argument("cmd", choices=["flash_auto", "hub"])
    ap.add_argument("server")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()

    cfg = load_config()
    server = cfg["servers"][a.server]

    # Hub passthrough command: user provides exact hub_cli.py args (mask-based)
    if a.cmd == "hub":
        ssh(server, f'{server["hub_cli"]} {" ".join(a.args)}', a.v)
        return

    # Determine parallelism for this server (default to 1)
    max_parallel = int(server.get("max_parallel", 1))
    if max_parallel < 1:
        max_parallel = 1

    # Parse device=firmware arguments
    tasks = []
    for arg in a.args:
        name, fw = arg.split("=", 1)
        tasks.append((name, fw))

    # Sequential mode
    if max_parallel == 1:
        for name, fw in tasks:
            flash_task(server, cfg["devices"], name, fw, a.v)
        return

    # Parallel mode
    print(f"[INFO] Parallel flashing enabled ({max_parallel} workers)")
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [
            pool.submit(flash_task, server, cfg["devices"], name, fw, a.v)
            for name, fw in tasks
        ]
        for f in as_completed(futures):
            _ = f.result()


if __name__ == "__main__":
    main()

