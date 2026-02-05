# STM32 Remote Flasher – Scripts

This directory contains the **core Python scripts** used to remotely discover,
configure, flash, and recover STM32 devices connected to a Linux-based
**flash server** (typically a Raspberry Pi).

The scripts are designed for **explicit, controlled, and reproducible**
programming in lab, manufacturing, and CI-like environments.

---

## Directory Contents

stm32-remote-flasher_scripts/
├── stm_remote.py      # Remote flashing + DFU recovery + hub power control
├── update_config.py   # Interactive STM32 discovery & config maintenance
├── config.json        # Server & device inventory
└── README.md          # This file

---

## Architecture Overview

Host PC
│
│  SSH / SCP
│
▼
Flash Server (Linux, e.g. Raspberry Pi)
├── STM32_Programmer_CLI
├── hub_cli.py (USB hub power control)
└── USB Hub
    ├── STM32 via DFU (USB)
    └── STM32 via ST-LINK (SWD)

---

## Roles & Concepts

### Host PC
- Runs `stm_remote.py` and `update_config.py`
- Does **not** require STM32 tools installed
- Connects to flash server via SSH

### Flash Server
- Linux system physically connected to STM32 devices
- Runs STM32CubeProgrammer
- Optionally controls USB hub power
- Executes all flashing operations

### STM32 Devices
- Accessed using:
  - USB DFU (ROM bootloader)
  - SWD via ST-LINK

---

## Requirements

### Host PC
- Python 3.9+
- ssh, scp available in PATH
- Network access to flash server

### Flash Server
- Linux (tested on Raspberry Pi)
- Python 3.9+
- STM32CubeProgrammer installed
- SSH server enabled
- Optional: USB hub with power-control API

---

## Configuration File (`config.json`)

### Server Configuration

Example:

{
  "servers": {
    "pi1": {
      "hostnames": ["192.168.0.21"],
      "user": "pi",
      "key": "~/.ssh/id_rsa",
      "stm32cli": "/usr/bin/STM32_Programmer_CLI",
      "hub_cli": "/home/pi/stm_hub/hub_cli.py",
      "max_parallel": 4,
      "power_control": true
    }
  }
}

Notes:
- IP address is only an example
- Multiple hostnames may be listed
- max_parallel controls concurrency

---

### Device Configuration

Example:

{
  "devices": {
    "dfu1": {
      "serial": "208230913036",
      "server": "pi1",
      "hub_port": "USB3",
      "default_interface": "USB",
      "signals": {
        "boot": false,
        "nrst": false
      }
    }
  }
}

---

## USB Hub Port Mapping

Human-readable USB port names are used.

USB1  → 0x0001  
USB2  → 0x0002  
USB3  → 0x0004  
USB4  → 0x0008  
...  
USB16 → 0x8000  

---

## Device Discovery (`update_config.py`)

### Purpose
- Runs STM32_Programmer_CLI -l remotely
- Detects DFU and ST-LINK devices
- Extracts serial numbers and board names
- Updates config.json interactively
- Optionally removes missing devices

### Usage

python3 update_config.py

### Example Detection Output

[NEW DEVICE DETECTED]
  Interface : SWD via ST-LINK
  Serial    : 066DFF515754888367131638
  Board     : NUCLEO-G0B1RE
  Info      : Serial is ST-LINK probe serial number

User is prompted for:
- Alias name
- Hub port (USB1..USB16)
- BOOT pin support
- NRST pin support

---

## Flashing (`stm_remote.py`)

### Command Syntax

python3 stm_remote.py [options] flash_auto <server> <device>=<firmware> [...]

### Examples

Single device:

python3 stm_remote.py flash_auto pi1 dfu1=bin/app.hex

Multiple devices (parallel):

python3 stm_remote.py -v 1 flash_auto pi1 \
  dfu1=bin/dfu.hex \
  g0=bin/g0.hex

---

## Verbosity Levels

-v 0  Minimal output  
-v 1  High-level progress  
-v 2  SSH / SCP commands  
-v 3  Full STM32CubeProgrammer output  

---

## DFU Recovery Logic

1. Initial flash attempt
2. If DFU not visible:
   - NRST pulse (if supported)
   - OR hub power-cycle
3. Second flash attempt

---

## Hub Control API

The hub control script must implement:

power_on <hex_mask>  
power_off <hex_mask>  
get_power_state  

Manual example:

python3 stm_remote.py hub pi1 power_off 0x0004  
python3 stm_remote.py hub pi1 power_on  0x0004  

---

## Parallel Flashing

- Controlled by max_parallel in config.json
- Implemented using ThreadPoolExecutor
- Logs are prefixed with device alias

Example:

[INFO][dfu1] Flash successful (recovery)  
[INFO][g0]   Flash successful (initial)  

---

## Hardware Notes

Important:
- USB power cycling is tested only with a **custom USB hub**
- Generic hubs are not supported without a compatible control API
- Hub hardware and firmware are expected to be documented separately

---

## Known Limitations

- No automatic USB topology detection
- No background daemon
- Assumes exclusive access to devices
- Recovery depends on DFU or hub power control

---

## License

MIT License

