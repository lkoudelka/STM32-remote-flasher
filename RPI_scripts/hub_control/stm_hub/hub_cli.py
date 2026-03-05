#!/usr/bin/env python3
import sys
from hub import STM32HubController

def main():
    if len(sys.argv) < 2:
        print("Usage: hub_cli.py <command> [args]")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    args = sys.argv[2:]
    hub = STM32HubController()

    try:
        if cmd == "power_on":
            mask = int(args[0], 16)
            print(hub.power_on(mask))
        elif cmd == "power_off":
            mask = int(args[0], 16)
            print(hub.power_off(mask))
        elif cmd == "set_power_state":
            mask = int(args[0], 16)
            print(hub.set_power_state(mask))
        elif cmd == "get_power_state":
            print(hub.get_power_state())
        elif cmd == "boot_on":
            mask = int(args[0], 16)
            print(hub.boot_on(mask))
        elif cmd == "boot_off":
            mask = int(args[0], 16)
            print(hub.boot_off(mask))
        elif cmd == "set_boot_state":
            mask = int(args[0], 16)
            print(hub.set_boot_state(mask))
        elif cmd == "get_boot_state":
            print(hub.get_boot_state())
        elif cmd == "nrst_on":
            mask = int(args[0], 16)
            print(hub.nrst_on(mask))
        elif cmd == "nrst_off":
            mask = int(args[0], 16)
            print(hub.nrst_off(mask))
        elif cmd == "set_nrst_state":
            mask = int(args[0], 16)
            print(hub.set_nrst_state(mask))
        elif cmd == "get_nrst_state":
            print(hub.get_nrst_state())
        elif cmd == "stlink_mux":
            port = int(args[0])
            print(hub.set_stlink_mux(port))
        elif cmd == "get_stlink_mux":
            print(hub.get_stlink_mux())
        else:
            print("Unknown command:", cmd)
    finally:
        hub.close()

if __name__ == "__main__":
    main()
