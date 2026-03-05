# Raspberry Pi BLE Wi-Fi Provisioning

BLE Wi-Fi provisioning service for Raspberry Pi using: - BlueZ D-Bus -
python3-dbus-next - NetworkManager

Tested on Raspberry Pi OS Bookworm.

------------------------------------------------------------------------

# 0) Prerequisites

-   Raspberry Pi OS (Bookworm recommended)
-   SSH access
-   sudo privileges
-   Bluetooth adapter available as `hci0`

------------------------------------------------------------------------

# 1) Clean Previous Installation

``` bash
sudo systemctl disable --now ble-wifi-bluez.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/ble-wifi-bluez.service
sudo rm -f /usr/local/bin/ble_wifi_bluez.py
sudo systemctl daemon-reload
sudo systemctl reset-failed ble-wifi-bluez.service 2>/dev/null || true
```

Verify:

``` bash
ls -l /usr/local/bin/ble_wifi_bluez.py || echo "OK: script removed"
```

------------------------------------------------------------------------

# 2) Install Required Packages

``` bash
sudo apt-get update
sudo apt-get install -y bluetooth bluez rfkill python3 python3-dbus-next network-manager
```

Enable NetworkManager:

``` bash
sudo systemctl enable --now NetworkManager
```

------------------------------------------------------------------------

# 3) Enable Bluetooth

``` bash
sudo rfkill unblock bluetooth || true
sudo systemctl enable --now bluetooth
sudo bluetoothctl power on || true
```

Verify:

``` bash
bluetoothctl show | sed -n '1,25p'
```

Expected:

    Powered: yes

------------------------------------------------------------------------

# 4) Create BLE Script

``` bash
sudo nano /usr/local/bin/ble_wifi_bluez.py
```

Paste your full BLE script.

Make executable:

``` bash
sudo chmod +x /usr/local/bin/ble_wifi_bluez.py
```

Verify header:

``` bash
head -n 5 /usr/local/bin/ble_wifi_bluez.py
```

First line must be:

    #!/usr/bin/env python3

------------------------------------------------------------------------

# 5) Create systemd Service

``` bash
sudo nano /etc/systemd/system/ble-wifi-bluez.service
```

Paste:

``` ini
[Unit]
Description=BLE WiFi Provisioning (BlueZ D-Bus)
After=bluetooth.service NetworkManager.service
Requires=bluetooth.service

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /usr/local/bin/ble_wifi_bluez.py
Restart=on-failure
RestartSec=2
TimeoutStartSec=30

[Install]
WantedBy=multi-user.target
```

Enable + start:

``` bash
sudo systemctl daemon-reload
sudo systemctl enable --now ble-wifi-bluez.service
```

------------------------------------------------------------------------

# 6) Verify Advertising

``` bash
sudo systemctl status ble-wifi-bluez.service --no-pager
sudo journalctl -u ble-wifi-bluez.service -n 80 --no-pager
sudo btmgmt advinfo
```

Expected:

-   Service: active (running)
-   Log shows: `Advertising started (RPI-WIFI)`
-   `Instances list with 1 items`

------------------------------------------------------------------------

# 7) BLE Service Definition

Device Name:

    RPI-WIFI

Service UUID:

    12345678-1234-5678-1234-56789abc0000

Characteristics:

  Name     UUID Suffix   Access
  -------- ------------- ------------
  SSID     0001          read/write
  PASS     0002          write
  APPLY    0003          write
  STATUS   0004          read
  IP       0005          read

------------------------------------------------------------------------

# 8) Provisioning Flow

1.  Connect to **RPI-WIFI**
2.  Read `STATUS` → should be `idle`
3.  Write SSID (UTF-8)
4.  Write PASS (UTF-8)
5.  Write APPLY command:

Preferred (hex):

    01

Also accepted:

    "01"
    "0x01"

Poll STATUS:

Possible values:

    idle
    connecting
    connected
    error:no_ssid
    error:timeout
    error:keymgmt
    error:perm
    error:nmcli
    error:bad_cmd
    error:busy

To refresh SSID + IP:

    02

------------------------------------------------------------------------

# Troubleshooting

Restart stack:

``` bash
sudo systemctl restart bluetooth
sudo systemctl restart ble-wifi-bluez.service
```

Check adapter:

``` bash
hciconfig
```

Check Wi-Fi:

``` bash
nmcli device status
```
