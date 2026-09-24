"""Port pool: hands out ports from a configured range and takes them back."""

from __future__ import annotations

import socket
import threading


class PortPoolExhaustedError(RuntimeError):
    """No free port left in the configured range."""


def is_bindable(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


class PortPool:
    def __init__(self, host: str, port_min: int, port_max: int) -> None:
        if port_min > port_max:
            raise ValueError("port_min must be <= port_max")
        self.host = host
        self._range = range(port_min, port_max + 1)
        self._leased: set[int] = set()
        self._lock = threading.Lock()

    @property
    def leased(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._leased)

    def allocate(self) -> int:
        """Lease the lowest port that is neither leased nor bound by another process."""
        with self._lock:
            for port in self._range:
                if port in self._leased:
                    continue
                if is_bindable(self.host, port):
                    self._leased.add(port)
                    return port
        raise PortPoolExhaustedError(f"no free port in {self._range.start}-{self._range.stop - 1}")

    def release(self, port: int) -> None:
        with self._lock:
            self._leased.discard(port)
