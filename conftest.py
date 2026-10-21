"""Root conftest: applies to every test in the repo."""

import socket
from typing import Any

import pytest

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}  # noqa: S104


class NetworkBlockedError(RuntimeError):
    """Raised when a test tries to reach a non-local host (spec §19)."""


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        # Unix sockets pass a str/bytes path; inet sockets pass a (host, port, ...) tuple.
        if isinstance(address, tuple) and address[0] not in _LOCAL_HOSTS:
            raise NetworkBlockedError(f"Network access blocked in tests: {address[0]}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
