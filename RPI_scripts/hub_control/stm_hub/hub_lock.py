#!/usr/bin/env python3
import fcntl

class HubLock:
    """
    Simple file-based lock for concurrent hub access.
    """
    def __init__(self, path="/tmp/stm_hub_uart.lock"):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, "w")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()
