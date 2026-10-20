"""Loaded in offline test processes and Python children; never in production."""
import os
import socket
import subprocess


def denied(*args, **kwargs):
    raise RuntimeError("Offline test: network access denied")


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
_original_popen = subprocess.Popen


class OfflineProcess(_original_popen):
    def __init__(self, args, *positional, **kwargs):
        command = args if isinstance(args, (list, tuple)) else [args]
        binary = os.path.basename(str(command[0])) if command else ""
        if kwargs.get("shell") or binary in {"snow", "cortex", "curl", "wget", "ssh"}:
            raise RuntimeError("Offline test: external command denied: " + binary)
        super().__init__(args, *positional, **kwargs)


subprocess.Popen = OfflineProcess
