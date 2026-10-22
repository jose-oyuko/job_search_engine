import socket

import pytest

from conftest import NetworkBlockedError


def test_external_connection_is_blocked() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlockedError):
            sock.connect(("93.184.216.34", 80))
    finally:
        sock.close()


def test_local_connection_is_not_blocked_by_the_guard() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        # Nothing listens on port 9, so the OS refuses; the guard itself must not raise.
        with pytest.raises(OSError) as excinfo:
            sock.connect(("127.0.0.1", 9))
        assert not isinstance(excinfo.value, NetworkBlockedError)
    finally:
        sock.close()
