#!/usr/bin/env python3
import serial
import time

class STM32HubController:
    """
    UART abstraction for STM32 hub control.
    """

    def __init__(self, port="/dev/serial0", baudrate=115200, timeout=1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        self.open()

    def open(self):
        """Open serial port."""
        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)

    def close(self):
        """Close serial port."""
        if self.ser and self.ser.is_open:
            self.ser.close()

    def send(self, cmd, response_timeout=1.0):
        """
        Send AT command with CR+LF and return hub response.
        If hub does not respond, return ['OK'] as default.
        """
        if not self.ser or not self.ser.is_open:
            self.open()
        self.ser.reset_input_buffer()
        full_cmd = cmd + "\r\n"
        self.ser.write(full_cmd.encode("ascii"))
        self.ser.flush()
        time.sleep(0.05)
        resp = self._read_response(timeout=response_timeout)
        if not resp:
            # Assume hub executed command if no ERROR received
            return ["OK"]
        return resp

    def _read_response(self, timeout=1.0):
        """Read lines until OK or ERROR, or timeout."""
        lines = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            if self.ser.in_waiting:
                line = self.ser.readline().decode(errors="ignore").strip()
                if line:
                    lines.append(line)
                    if line.upper() == "OK" or line.upper().startswith("ERROR"):
                        break
            else:
                time.sleep(0.01)
        return lines

    # ------------------------
    # Power control
    # ------------------------
    def power_on(self, mask):
        return self.send(f"AT+POWERON=0x{mask:04X}")

    def power_off(self, mask):
        return self.send(f"AT+POWEROFF=0x{mask:04X}")

    def set_power_state(self, mask):
        return self.send(f"AT+POWERSTATE=0x{mask:04X}")

    def get_power_state(self):
        return self.send("AT+POWERSTATE?")

    # ------------------------
    # BOOT pin control
    # ------------------------
    def boot_on(self, mask):
        return self.send(f"AT+BOOTON=0x{mask:04X}")

    def boot_off(self, mask):
        return self.send(f"AT+BOOTOFF=0x{mask:04X}")

    def set_boot_state(self, mask):
        return self.send(f"AT+BOOTSTATE=0x{mask:04X}")

    def get_boot_state(self):
        return self.send("AT+BOOTSTATE?")

    # ------------------------
    # NRST pin control
    # ------------------------
    def nrst_on(self, mask):
        return self.send(f"AT+NRSTON=0x{mask:04X}")

    def nrst_off(self, mask):
        return self.send(f"AT+NRSTOFF=0x{mask:04X}")

    def set_nrst_state(self, mask):
        return self.send(f"AT+NRSTSTATE=0x{mask:04X}")

    def get_nrst_state(self):
        return self.send("AT+NRSTSTATE?")

    # ------------------------
    # STLINK MUX control
    # ------------------------
    def set_stlink_mux(self, port):
        if not (1 <= port <= 16):
            raise ValueError("STLINK MUX port must be between 1 and 16")
        return self.send(f"AT+STLINKMUX={port}")

    def get_stlink_mux(self):
        return self.send("AT+STLINKMUX?")
