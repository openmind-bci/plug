"""The offline check of examples/validate_ironbci.py sends nothing on the network."""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "validate_ironbci.py"


@pytest.fixture(scope="module")
def validate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_ironbci", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


class RecordingSocket:
    """A socket that records what is done with it and never touches the network."""

    calls: ClassVar[list[tuple[str, Any]]] = []
    route: ClassVar[bool] = True

    def __init__(self, family: int = socket.AF_INET, kind: int = socket.SOCK_STREAM) -> None:
        RecordingSocket.calls.append(("socket", kind))

    def __enter__(self) -> RecordingSocket:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def connect(self, address: tuple[str, int]) -> None:
        RecordingSocket.calls.append(("connect", address))
        if not RecordingSocket.route:
            raise OSError(101, "Network is unreachable")

    def __getattr__(self, name: str) -> Any:  # send, sendto, ... must not be used
        def forbidden(*args: object, **kwargs: object) -> None:
            RecordingSocket.calls.append((name, args))

        return forbidden


@pytest.mark.parametrize("route", [True, False])
def test_internet_check_only_asks_for_a_route(
    validate: ModuleType, monkeypatch: pytest.MonkeyPatch, route: bool
) -> None:
    RecordingSocket.calls = []
    RecordingSocket.route = route
    monkeypatch.setattr(validate.socket, "socket", RecordingSocket)
    monkeypatch.setattr(
        validate.socket,
        "create_connection",
        lambda *a, **k: RecordingSocket.calls.append(("create_connection", a)),
    )
    assert validate._internet_reachable() is route
    # A UDP connect() only looks up a route: no TCP connection, nothing sent.
    assert RecordingSocket.calls[0] == ("socket", socket.SOCK_DGRAM)
    assert [name for name, _ in RecordingSocket.calls] == ["socket", "connect"]
