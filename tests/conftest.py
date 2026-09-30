import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = Path(__file__).parent / "data"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AQUASUITELINUX_SOCKET", str(tmp_path / "no-service.sock"))


@pytest.fixture
def quadro_control() -> bytes:
    return (DATA / "quadro_control.bin").read_bytes()


@pytest.fixture
def quadro_status() -> bytes:
    return (DATA / "quadro_sensors.bin").read_bytes()


@pytest.fixture
def quadro_soft() -> bytes:
    return (DATA / "quadro_virt_sensors.bin").read_bytes()


@pytest.fixture
def demo_engine():
    from aquasuitelinux.core.demo import demo_config, demo_provider
    from aquasuitelinux.core.engine import Engine
    eng = Engine(demo_config(), demo_provider(speed=4.0), mode="demo")
    yield eng
    eng.stop()


def run_ticks(engine, n: int, pause: float = 0.22):
    import time
    snap = None
    for _ in range(n):
        snap = engine.tick()
        time.sleep(pause)
    return snap
