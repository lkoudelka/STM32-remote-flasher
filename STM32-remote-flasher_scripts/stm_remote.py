#!/usr/bin/env python3
"""
stm_remote_paramiko_tunnel.py

Paramiko-based remote STM32 flashing (platform-independent host PC: Windows/macOS/Linux).

Key points
----------
- Uses Paramiko SSH + SFTP (no external `ssh`/`scp` binaries required).
- Optional parallel flashing per server: servers.<name>.max_parallel
- Optional HUB daemon fast-path ("hubd") via Paramiko "direct-tcpip" channel:
    - Enable by adding `hubd` to the server config.
    - When used, you will see:
        [INFO][hub] Using hubd via SSH tunnel to 127.0.0.1:9999
  If hubd is not configured (or fails), hub operations fall back to running hub_cli.py over SSH.

Important: remote firmware caching (fixes concurrency + regressions)
-------------------------------------------------------------------
When flashing multiple devices with the SAME firmware file in one run, e.g.
  python3 stm_remote_paramiko_tunnel.py -v 1 flash_auto pi1 WB1=bin/fw.hex WB2=bin/fw.hex ...

we now:
- Upload each distinct firmware ONCE per run into a server-side cache path (unique by file signature).
- Reuse that remote file for all devices.
- Delete cached remote firmware files only at the VERY END of the run (after all devices finish),
  preventing "file missing" and SFTP "size mismatch" errors caused by concurrent puts/removes.

Verbosity
---------
- -v 0 : minimal
- -v 1 : info
- -v 2 : debug
- -v 3 : includes STM32_Programmer_CLI output
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import paramiko

CONFIG_FILE = "config.json"
REMOTE_CACHE_DIR = "/tmp/stm_remote_cache"


# -------------------------- utilities --------------------------

def hub_port_to_mask(port: str) -> int:
    """
    USB1  -> 0x0001
    USB2  -> 0x0002
    USB3  -> 0x0004
    ...
    USB16 -> 0x8000
    """
    n = int(port.replace("USB", ""))
    return 1 << (n - 1)


def sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _expanduser(p: str) -> str:
    return os.path.expanduser(p)


def _server_host(server: dict) -> str:
    # keep your config.json shape ("hostnames": [...])
    return server["hostnames"][0]


# -------------------------- Paramiko client helpers --------------------------

def connect_paramiko(server: dict, verbose: int) -> paramiko.SSHClient:
    host = _server_host(server)
    user = server["user"]
    key_path = _expanduser(server["key"])

    if verbose >= 2:
        print(f"[DEBUG] Paramiko connecting to {user}@{host} using key {key_path}")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        key_filename=key_path,
        allow_agent=True,
        look_for_keys=True,
        timeout=10,
    )
    return client


def ssh_exec(client: paramiko.SSHClient, cmd: str, verbose: int, check: bool = True) -> Tuple[int, str, str]:
    if verbose >= 2:
        print(f"[DEBUG] SSH: {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    if check and rc != 0:
        raise RuntimeError(f"Remote command failed (rc={rc}): {cmd}\n{err}".strip())
    return rc, out, err


# -------------------------- hubd tunnel + RPC --------------------------

class HubdTunnel:
    """
    Local TCP port -> remote 127.0.0.1:hubd_port over the existing Paramiko transport.
    """

    def __init__(self, client: paramiko.SSHClient, remote_bind_host: str, remote_port: int, verbose: int):
        self.client = client
        self.remote_bind_host = remote_bind_host
        self.remote_port = remote_port
        self.verbose = verbose

        self.transport = client.get_transport()
        if self.transport is None:
            raise RuntimeError("Paramiko transport not available")

        self._listen_sock: Optional[socket.socket] = None
        self.local_port: Optional[int] = None

        self._stop = False

    def start(self) -> Tuple[str, int]:
        # bind to localhost random free port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(50)
        self._listen_sock = s
        self.local_port = s.getsockname()[1]

        if self.verbose >= 1:
            print(f"[INFO][hub] Using hubd via SSH tunnel to {self.remote_bind_host}:{self.remote_port} "
                  f"(local 127.0.0.1:{self.local_port})")

        # accept loop runs in a background thread-like via paramiko forward handler style
        import threading
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        return "127.0.0.1", self.local_port

    def _accept_loop(self):
        assert self._listen_sock is not None
        while not self._stop:
            try:
                client_sock, _addr = self._listen_sock.accept()
            except OSError:
                break
            # open channel to remote
            try:
                chan = self.transport.open_channel(
                    kind="direct-tcpip",
                    dest_addr=(self.remote_bind_host, self.remote_port),
                    src_addr=("127.0.0.1", 0),
                )
            except Exception:
                client_sock.close()
                continue

            import threading
            threading.Thread(target=self._pipe, args=(client_sock, chan), daemon=True).start()
            threading.Thread(target=self._pipe, args=(chan, client_sock), daemon=True).start()

    @staticmethod
    def _pipe(src, dst):
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass
            try:
                src.close()
            except Exception:
                pass

    def close(self):
        self._stop = True
        if self._listen_sock:
            try:
                self._listen_sock.close()
            except Exception:
                pass
            self._listen_sock = None


def hubd_call(host: str, port: int, cmd: str, args: List[str], verbose: int) -> str:
    """
    Simple request/response line protocol:
      request:  CMD arg1 arg2...\n
      response: one line JSON or text (server-defined)
    """
    line = " ".join([cmd] + args) + "\n"
    if verbose >= 3:
        print(f"[DEBUG][hub] hubd_call -> {line.strip()}")
    s = socket.create_connection((host, port), timeout=2.0)
    try:
        s.sendall(line.encode("utf-8"))
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        return data.decode("utf-8", errors="replace").strip()
    finally:
        s.close()


# -------------------------- firmware pre-upload cache --------------------------

def remote_name_preserve_ext(local_fw: str, digest: str) -> str:
    """
    IMPORTANT: extension must be the last suffix for STM32_Programmer_CLI.
    Example:
      local: CDC_Standalone_fast.hex
      remote: CDC_Standalone_fast.<hash>.hex
    """
    p = Path(local_fw)
    stem = p.stem          # CDC_Standalone_fast
    suffix = p.suffix      # .hex
    return f"{stem}.{digest[:12]}{suffix}"


def preupload_firmwares(server: dict, verbose: int, tasks: List[Tuple[str, str]]) -> Dict[str, str]:
    """
    Upload each UNIQUE local firmware once, return map: local_fw_path -> remote_fw_path.
    Runs sequentially to avoid SFTP put races.
    """
    unique_fw = sorted({fw for _name, fw in tasks})
    if not unique_fw:
        return {}

    client = connect_paramiko(server, verbose)
    try:
        ssh_exec(client, f"mkdir -p {REMOTE_CACHE_DIR}", verbose, check=True)
        sftp = client.open_sftp()

        fw_map: Dict[str, str] = {}
        for fw in unique_fw:
            digest = sha1_file(fw)
            remote_file = remote_name_preserve_ext(fw, digest)
            remote_path = f"{REMOTE_CACHE_DIR}/{remote_file}"

            # If already exists with same size, skip (cheap speed-up).
            local_size = os.path.getsize(fw)
            skip = False
            try:
                st = sftp.stat(remote_path)
                if st.st_size == local_size:
                    skip = True
            except IOError:
                pass

            if not skip:
                if verbose >= 2:
                    print(f"[DEBUG] SFTP PUT {fw} -> {remote_path}")
                tmp_remote = remote_path + ".uploading"
                # upload to temp then atomic rename
                sftp.put(fw, tmp_remote)
                sftp.rename(tmp_remote, remote_path)

            fw_map[fw] = remote_path

        sftp.close()
        return fw_map
    finally:
        try:
            client.close()
        except Exception:
            pass


def cleanup_remote_firmwares(server: dict, verbose: int, remote_paths: List[str]) -> None:
    """
    Remove remote cached firmware files. Called only at the very end.
    """
    if not remote_paths:
        return
    client = connect_paramiko(server, verbose)
    try:
        sftp = client.open_sftp()
        for rp in sorted(set(remote_paths)):
            try:
                if verbose >= 2:
                    print(f"[DEBUG] SFTP RM {rp}")
                sftp.remove(rp)
            except IOError:
                pass
        sftp.close()
    finally:
        try:
            client.close()
        except Exception:
            pass


# -------------------------- flashing logic --------------------------

def flash_device(
    server: dict,
    dev: dict,
    local_fw: str,
    remote_fw: str,
    verbose: int,
    alias: str,
    hubd_endpoint: Optional[Tuple[str, int]],
) -> bool:
    def log(level: str, msg: str) -> None:
        print(f"[{level}][{alias}] {msg}")

    serial = dev["serial"]
    iface = dev["default_interface"]  # USB or SWD
    hub_port = dev.get("hub_port")
    nrst_supported = dev.get("signals", {}).get("nrst", False)

    client = connect_paramiko(server, verbose)
    try:
        def attempt(tag: str) -> bool:
            if verbose >= 1:
                log("INFO", f"Flash attempt ({tag})")

            port_arg = "USB" if iface == "USB" else "SWD"
            cmd = (
                f'{server["stm32cli"]} '
                f'-c port={port_arg} SN={serial} '
                f'-w {remote_fw} -v -g'
            )
            rc, out, err = ssh_exec(client, cmd, verbose, check=False)
            if verbose >= 3:
                # show full programmer output
                sys.stdout.write(out)
                sys.stdout.write(err)

            # CubeProgrammer tends to print "Error:" lines
            combined = (out + "\n" + err)
            return ("Error:" not in combined) and (rc == 0)

        # initial attempt
        if attempt("initial"):
            log("INFO", "Flash successful (initial)")
            return True

        log("WARN", "Flash failed (initial)")

        # DFU recovery path
        if iface == "USB":
            log("WARN", "DFU not visible, attempting recovery")

            # If NRST control is wired through hub control later, this is where it would go.
            if nrst_supported:
                # Placeholder (no physical line toggle here)
                if verbose >= 1:
                    log("INFO", "NRST pulse (placeholder only)")
                time.sleep(0.02)

            elif server.get("power_control") and hub_port:
                mask = hub_port_to_mask(hub_port)
                log("INFO", f"Power-cycling {hub_port} (mask 0x{mask:04X})")

                # Prefer hubd if configured and tunnel is active
                if hubd_endpoint:
                    h, p = hubd_endpoint
                    hubd_call(h, p, "power_off", [f"0x{mask:04X}"], verbose)
                    time.sleep(0.1)
                    hubd_call(h, p, "power_on", [f"0x{mask:04X}"], verbose)
                    time.sleep(0.1)
                else:
                    # fallback: run hub_cli.py over SSH
                    ssh_exec(client, f'{server["hub_cli"]} power_off 0x{mask:04X}', verbose, check=False)
                    time.sleep(0.1)
                    ssh_exec(client, f'{server["hub_cli"]} power_on 0x{mask:04X}', verbose, check=False)
                    time.sleep(0.1)

            ok = attempt("recovery")
            if ok:
                log("INFO", "Flash successful (recovery)")
            else:
                log("ERROR", "Flash failed (recovery)")
            return ok

        # non-USB failed
        return False

    finally:
        try:
            client.close()
        except Exception:
            pass


def flash_task(
    server: dict,
    devices: dict,
    name: str,
    fw: str,
    remote_fw_map: Dict[str, str],
    verbose: int,
    hubd_endpoint: Optional[Tuple[str, int]],
) -> bool:
    print(f"[INFO] Flashing {name}")
    remote_fw = remote_fw_map[fw]
    ok = flash_device(server, devices[name], fw, remote_fw, verbose, name, hubd_endpoint)
    if not ok:
        print(f"[ERROR][{name}] Flash failed")
    return ok


# -------------------------- main --------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", type=int, default=0, help="Verbosity level (0-3)")
    ap.add_argument("cmd", choices=["flash_auto", "hub"])
    ap.add_argument("server")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()

    cfg = load_config()
    server = cfg["servers"][a.server]

    # hub passthrough (mask-based)
    if a.cmd == "hub":
        # For hub passthrough we just run hub_cli.py over SSH (keeps compatibility).
        client = connect_paramiko(server, a.v)
        try:
            ssh_exec(client, f'{server["hub_cli"]} {" ".join(a.args)}', a.v, check=False)
        finally:
            client.close()
        return

    # parse tasks
    tasks: List[Tuple[str, str]] = []
    for arg in a.args:
        name, fw = arg.split("=", 1)
        tasks.append((name, fw))

    if not tasks:
        print("No flash tasks provided.")
        return

    # parallelism
    max_parallel = int(server.get("max_parallel", 1))
    if max_parallel < 1:
        max_parallel = 1

    # --- OPTIONAL hubd tunnel setup (single tunnel for the whole run) ---
    hubd_endpoint: Optional[Tuple[str, int]] = None
    tunnel: Optional[HubdTunnel] = None
    tunnel_client: Optional[paramiko.SSHClient] = None

    hubd_cfg = server.get("hubd", {}) if isinstance(server.get("hubd", {}), dict) else {}
    use_hubd = bool(hubd_cfg.get("enabled", False))
    if use_hubd:
        bind = str(hubd_cfg.get("bind", "127.0.0.1"))
        port = int(hubd_cfg.get("port", 9999))
        # create a dedicated client for the tunnel lifetime
        tunnel_client = connect_paramiko(server, a.v)
        tunnel = HubdTunnel(tunnel_client, bind, port, a.v)
        hubd_endpoint = tunnel.start()

    try:
        # --- PREUPLOAD ONCE (sequential) ---
        remote_fw_map = preupload_firmwares(server, a.v, tasks)

        if max_parallel == 1:
            for name, fw in tasks:
                flash_task(server, cfg["devices"], name, fw, remote_fw_map, a.v, hubd_endpoint)
        else:
            print(f"[INFO] Parallel flashing enabled ({max_parallel} workers)")
            with ThreadPoolExecutor(max_workers=max_parallel) as pool:
                futures = [
                    pool.submit(
                        flash_task,
                        server,
                        cfg["devices"],
                        name,
                        fw,
                        remote_fw_map,
                        a.v,
                        hubd_endpoint,
                    )
                    for name, fw in tasks
                ]
                for f in as_completed(futures):
                    _ = f.result()

        # --- CLEANUP LAST (after all tasks) ---
        cleanup_remote_firmwares(server, a.v, list(remote_fw_map.values()))

    finally:
        if tunnel:
            tunnel.close()
        if tunnel_client:
            try:
                tunnel_client.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
