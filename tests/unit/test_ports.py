import socket

import pytest

from workbench.envs.ports import PortPool, PortPoolExhaustedError


def _free_range(n: int) -> tuple[int, int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        base = s.getsockname()[1]
    return base, base + n - 1


def test_allocate_and_release() -> None:
    lo, hi = _free_range(2)
    pool = PortPool("127.0.0.1", lo, hi)
    a = pool.allocate()
    b = pool.allocate()
    assert {a, b} == {lo, hi}
    with pytest.raises(PortPoolExhaustedError):
        pool.allocate()
    pool.release(a)
    assert pool.allocate() == a


def test_skips_ports_bound_by_other_processes() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        pool = PortPool("127.0.0.1", port, port)
        with pytest.raises(PortPoolExhaustedError):
            pool.allocate()


def test_invalid_range() -> None:
    with pytest.raises(ValueError):
        PortPool("127.0.0.1", 10, 5)
