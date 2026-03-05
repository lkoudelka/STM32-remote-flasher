#!/usr/bin/env python3
import asyncio
import logging
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, PropertyAccess
from dbus_next.service import ServiceInterface, method, dbus_property

# -----------------------------
# Logging (goes to journald via systemd)
# -----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# -----------------------------
# BlueZ / D-Bus constants
# -----------------------------
BLUEZ = "org.bluez"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
GATT_MGR_IFACE = "org.bluez.GattManager1"
LE_ADV_MGR_IFACE = "org.bluez.LEAdvertisingManager1"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"
LE_ADV_IFACE = "org.bluez.LEAdvertisement1"

# -----------------------------
# Unique 4-digit ID from CPU serial
# -----------------------------
def get_unique_suffix():
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("Serial"):
                    serial = line.split(":")[1].strip()
                    return serial[-4:].upper()
    except Exception:
        pass
    return "0000"

# -----------------------------
# Your BLE config (only name changed)
# -----------------------------
DEVICE_NAME = f"RPI-WIFI-{get_unique_suffix()}"

WIFI_SERVICE_UUID = "12345678-1234-5678-1234-56789abc0000"
SSID_CHAR_UUID     = "12345678-1234-5678-1234-56789abc0001"
PASS_CHAR_UUID     = "12345678-1234-5678-1234-56789abc0002"
APPLY_CHAR_UUID    = "12345678-1234-5678-1234-56789abc0003"
STATUS_CHAR_UUID   = "12345678-1234-5678-1234-56789abc0004"
IP_CHAR_UUID       = "12345678-1234-5678-1234-56789abc0005"

# -----------------------------
# Helpers
# -----------------------------
def v(sig: str, val: Any) -> Variant:
    return Variant(sig, val)

def b2s(b: bytes) -> str:
    return b.decode("utf-8", errors="ignore")

def s2b(s: str) -> bytes:
    return s.encode("utf-8")

def run_cmd(cmd: List[str], timeout: int = 20) -> str:
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        stderr = (p.stderr or "").strip()
        stdout = (p.stdout or "").strip()
        detail = stderr if stderr else stdout
        raise RuntimeError(f"{' '.join(cmd)} failed rc={p.returncode}: {detail}")
    return (p.stdout or "").strip()

def nmcli_ok() -> bool:
    try:
        run_cmd(["nmcli", "-v"], timeout=5)
        return True
    except Exception:
        return False

def clamp_text(s: str, max_len: int) -> str:
    s = s.strip()
    if len(s) > max_len:
        return s[:max_len]
    return s
def get_current_ssid() -> str:
    """
    Return currently active SSID, if any.
    """
    # Prefer nmcli
    try:
        out = run_cmd(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], timeout=10)
        for line in out.splitlines():
            # yes:MySSID
            if line.startswith("yes:"):
                return line.split(":", 1)[1]
    except Exception:
        pass

    # Fallback: iwgetid (may not be installed)
    try:
        return run_cmd(["iwgetid", "-r"], timeout=5).strip()
    except Exception:
        return ""

def get_ip_v4() -> str:
    """
    Return best-effort IPv4 for wlan0, else first non-loopback address.
    """
    try:
        out = run_cmd(["ip", "-4", "addr", "show", "dev", "wlan0"], timeout=5)
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass

    try:
        out = run_cmd(["hostname", "-I"], timeout=5)
        for token in out.split():
            if token and not token.startswith("127."):
                return token
    except Exception:
        pass

    return ""

async def find_adapter(bus: MessageBus) -> str:
    """
    Find adapter path via ObjectManager on "/".
    """
    intro = await bus.introspect(BLUEZ, "/")
    obj = bus.get_proxy_object(BLUEZ, "/", intro)
    om = obj.get_interface(DBUS_OM_IFACE)
    managed = await om.call_get_managed_objects()
    for path, ifaces in managed.items():
        if "org.bluez.Adapter1" in ifaces:
            return path
    raise RuntimeError("No Bluetooth adapter found")

def parse_apply_cmd(raw: bytes) -> Optional[int]:
    """
    Accept:
      - single byte 0x01
      - ASCII "01"
      - ASCII "0x01"
    """
    if not raw:
        return None
    if len(raw) == 1:
        return raw[0]
    s = b2s(raw).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if re.fullmatch(r"[0-9a-f]{1,2}", s):
        return int(s, 16)
    return None

def nm_escape_con_name(ssid: str) -> str:
    """
    Use a stable, safe connection name derived from SSID.
    Avoid collisions with user's own profiles by prefixing.
    """
    # Connection name cannot be empty; keep it simple ASCII-ish
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", ssid.strip())
    cleaned = cleaned.strip("_")
    if not cleaned:
        cleaned = "wifi"
    # Prefix to avoid clobbering existing manually-created profile named exactly SSID
    return f"BLE_{cleaned}"

def nm_get_active_connection() -> Tuple[str, str]:
    """
    Return (device, connection-name) for wlan0 if active.
    """
    try:
        dev = run_cmd(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"], timeout=5)
        for line in dev.splitlines():
            # wlan0:wifi:connected:MyConn
            parts = line.split(":")
            if len(parts) >= 4 and parts[0] == "wlan0" and parts[1] == "wifi" and parts[2] == "connected":
                return parts[0], parts[3]
    except Exception:
        pass
    return ("", "")

def nm_connect_wifi(ssid: str, password: str, ifname: str = "wlan0", timeout: int = 45) -> None:
    """
    Robust NetworkManager connect:
    - create/update a dedicated connection profile with explicit key-mgmt
    - bring it up

    This avoids the '802-11-wireless-security.key-mgmt: property is missing' issue.
    """
    ssid = clamp_text(ssid, 32)
    password = password.rstrip("\n")  # keep spaces, but remove trailing newlines

    if not ssid:
        raise RuntimeError("no_ssid")

    con_name = nm_escape_con_name(ssid)

    # Ensure device is managed and Wi-Fi is enabled
    # (these are safe no-ops if already OK)
    run_cmd(["nmcli", "radio", "wifi", "on"], timeout=10)
    run_cmd(["nmcli", "dev", "set", ifname, "managed", "yes"], timeout=10)

    # If the connection exists, modify it. Otherwise create it.
    con_list = run_cmd(["nmcli", "-t", "-f", "NAME", "con", "show"], timeout=10).splitlines()
    exists = con_name in con_list

    if not exists:
        run_cmd(["nmcli", "con", "add", "type", "wifi", "ifname", ifname, "con-name", con_name, "ssid", ssid], timeout=15)
    else:
        # Ensure SSID matches (in case user changed SSID while keeping same derived name)
        run_cmd(["nmcli", "con", "modify", con_name, "802-11-wireless.ssid", ssid], timeout=10)

    # Security handling:
    # If password is empty -> open network
    if password.strip() == "":
        run_cmd(["nmcli", "con", "modify", con_name, "wifi-sec.key-mgmt", "none"], timeout=10)
        # Remove any old secrets to prevent confusion
        try:
            run_cmd(["nmcli", "con", "modify", con_name, "-wifi-sec.psk"], timeout=10)
        except Exception:
            pass
    else:
        run_cmd(["nmcli", "con", "modify", con_name, "wifi-sec.key-mgmt", "wpa-psk"], timeout=10)
        run_cmd(["nmcli", "con", "modify", con_name, "wifi-sec.psk", password], timeout=10)

    # Make it persistent & preferred
    run_cmd(["nmcli", "con", "modify", con_name, "connection.autoconnect", "yes"], timeout=10)
    run_cmd(["nmcli", "con", "modify", con_name, "connection.autoconnect-priority", "50"], timeout=10)

    # Bring up connection (this might disconnect current wifi)
    run_cmd(["nmcli", "con", "up", con_name], timeout=timeout)

# -----------------------------
# D-Bus GATT objects
# -----------------------------
class Application(ServiceInterface):
    def __init__(self):
        super().__init__(DBUS_OM_IFACE)
        self._objects: Dict[str, Dict[str, Dict[str, Variant]]] = {}

    def add_managed_object(self, path: str, iface: str, props: Dict[str, Variant]) -> None:
        self._objects.setdefault(path, {})
        self._objects[path][iface] = props

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        return self._objects

class Service(ServiceInterface):
    def __init__(self, path: str, uuid: str):
        super().__init__(GATT_SERVICE_IFACE)
        self.path = path
        self.uuid = uuid

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> "as":
        return []

class Characteristic(ServiceInterface):
    """
    Basic characteristic with optional notify support.
    """
    def __init__(self, path: str, service_path: str, uuid: str, flags: List[str], initial: bytes = b""):
        super().__init__(GATT_CHRC_IFACE)
        self.path = path
        self.service_path = service_path
        self.uuid = uuid
        self.flags = flags
        self._value: bytes = bytes(initial)
        self._notifying: bool = False

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return self.service_path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return self.flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self._value

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        return self._value

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        self._value = bytes(value)

    @method()
    def StartNotify(self):
        if "notify" not in self.flags:
            return
        self._notifying = True

    @method()
    def StopNotify(self):
        if "notify" not in self.flags:
            return
        self._notifying = False

    def set_value(self, b: bytes, notify: bool = True):
        self._value = bytes(b)
        if notify and self._notifying and "notify" in self.flags:
            # Emit PropertiesChanged for Value
            self.emit_properties_changed({"Value": Variant("ay", self._value)}, [])

class Advertisement(ServiceInterface):
    def __init__(self, path: str, local_name: str, service_uuids: List[str]):
        super().__init__(LE_ADV_IFACE)
        self.path = path
        self.local_name = local_name
        self.service_uuids = service_uuids

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return self.service_uuids

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self.local_name

    @dbus_property(access=PropertyAccess.READ)
    def IncludeTxPower(self) -> "b":
        return True

    @method()
    def Release(self):
        logging.info("Advertisement released")

# -----------------------------
# State + main logic
# -----------------------------
class WifiState:
    def __init__(self):
        self.status: str = "idle"

    def set_status(self, s: str):
        self.status = s
        logging.info("STATUS=%s", s)

async def main():
    if not nmcli_ok():
        logging.warning("nmcli not available; install/enable NetworkManager for connect support")

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    adapter_path = await find_adapter(bus)
    logging.info("Using adapter: %s", adapter_path)

    app = Application()

    base = "/com/example/wifi"
    svc_path   = f"{base}/service0"
    ssid_path  = f"{svc_path}/char0"
    pass_path  = f"{svc_path}/char1"
    apply_path = f"{svc_path}/char2"
    stat_path  = f"{svc_path}/char3"
    ip_path    = f"{svc_path}/char4"
    adv_path   = f"{base}/adv0"

    svc = Service(svc_path, WIFI_SERVICE_UUID)
    wifi = WifiState()

    # Cached read-back values
    ssid_cache = get_current_ssid()
    ip_cache   = get_ip_v4()

    # Characteristics:
    # - SSID: read/write (read-back current ssid; can also be written for connect)
    # - PASS: write only
    # - APPLY: write only commands
    # - STATUS: read + notify
    # - IP: read-back
    ssid = Characteristic(ssid_path, svc_path, SSID_CHAR_UUID, ["read", "write"], s2b(ssid_cache))
    pwd  = Characteristic(pass_path, svc_path, PASS_CHAR_UUID, ["write"], b"")
    stat = Characteristic(stat_path, svc_path, STATUS_CHAR_UUID, ["read", "notify"], s2b(wifi.status))
    ipch = Characteristic(ip_path,   svc_path, IP_CHAR_UUID,   ["read"], s2b(ip_cache))

    connect_task: Optional[asyncio.Task] = None

    def refresh_readbacks():
        nonlocal ssid_cache, ip_cache
        ssid_cache = get_current_ssid()
        ip_cache = get_ip_v4()
        ssid.set_value(s2b(ssid_cache), notify=False)  # not required by your contract
        ipch.set_value(s2b(ip_cache), notify=False)
        logging.info("Refresh: ssid=%r ip=%r", ssid_cache, ip_cache)

    def set_status(s: str):
        wifi.set_status(s)
        stat.set_value(s2b(wifi.status), notify=True)

    async def do_connect():
        nonlocal ssid_cache, ip_cache
        try:
            target_ssid = clamp_text(b2s(ssid.Value), 32)
            target_pwd  = b2s(pwd.Value)

            if not target_ssid:
                set_status("error:no_ssid")
                return

            set_status("connecting")

            # Use robust NM profile approach
            nm_connect_wifi(target_ssid, target_pwd, ifname="wlan0", timeout=60)

            # After successful NM up, refresh readbacks
            refresh_readbacks()
            set_status("connected")

        except subprocess.TimeoutExpired:
            set_status("error:timeout")
        except Exception as e:
            # Map common causes into better UX strings
            msg = str(e).lower()
            if "no_ssid" in msg:
                set_status("error:no_ssid")
            elif "not authorized" in msg or "permission" in msg:
                set_status("error:perm")
            elif "key-mgmt" in msg:
                set_status("error:keymgmt")
            else:
                # Keep short and stable for BLE UX
                set_status("error:nmcli")
            logging.exception("Connect failed: %s", e)

    class ApplyCharacteristic(Characteristic):
        @method()
        def WriteValue(self, value: "ay", options: "a{sv}"):
            nonlocal connect_task
            raw = bytes(value)
            cmd = parse_apply_cmd(raw)
            if cmd is None:
                set_status("error:bad_cmd")
                return

            logging.info("APPLY cmd=0x%02X raw=%s", cmd, raw.hex())

            if cmd == 0x01:
                # connect (async; do not block dbus)
                if connect_task and not connect_task.done():
                    set_status("error:busy")
                    return
                connect_task = asyncio.create_task(do_connect())
                return

            if cmd == 0x02:
                refresh_readbacks()
                set_status("idle")
                return

            set_status("error:bad_cmd")

    ap = ApplyCharacteristic(apply_path, svc_path, APPLY_CHAR_UUID, ["write"], b"")

    # Export DBus objects
    bus.export(base, app)
    for obj_path, obj in [
        (svc_path, svc),
        (ssid_path, ssid),
        (pass_path, pwd),
        (apply_path, ap),
        (stat_path, stat),
        (ip_path, ipch),
    ]:
        bus.export(obj_path, obj)

    # ObjectManager tree
    app.add_managed_object(
        svc_path, GATT_SERVICE_IFACE,
        {"UUID": v("s", WIFI_SERVICE_UUID), "Primary": v("b", True), "Includes": v("as", [])}
    )
    for c in [ssid, pwd, ap, stat, ipch]:
        app.add_managed_object(
            c.path, GATT_CHRC_IFACE,
            {
                "UUID": v("s", c.uuid),
                "Service": v("o", svc_path),
                "Flags": v("as", c.flags),
                "Value": v("ay", c.Value),
            }
        )

    # Register with BlueZ
    intro = await bus.introspect(BLUEZ, adapter_path)
    adapter_obj = bus.get_proxy_object(BLUEZ, adapter_path, intro)
    gatt_mgr = adapter_obj.get_interface(GATT_MGR_IFACE)
    adv_mgr = adapter_obj.get_interface(LE_ADV_MGR_IFACE)

    logging.info("Registering GATT application...")
    await gatt_mgr.call_register_application(base, {})

    adv = Advertisement(adv_path, DEVICE_NAME, [WIFI_SERVICE_UUID])
    bus.export(adv_path, adv)

    logging.info("Registering advertisement...")
    await adv_mgr.call_register_advertisement(adv_path, {})

    logging.info("Advertising started (%s)", DEVICE_NAME)
    await asyncio.get_running_loop().create_future()

if __name__ == "__main__":
    asyncio.run(main())
