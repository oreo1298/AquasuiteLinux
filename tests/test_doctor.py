"""``aquactl doctor`` against a fake sysfs and the simulator."""

import pytest
from conftest import quadro_sysfs

from aquasuitelinux.core import doctor
from aquasuitelinux.core.errors import DeviceError
from aquasuitelinux.core.simulator import SimDevice, SimTransport, World


class Bulk:
    def __init__(self, sim, mode="deliver"):
        self.sim, self.mode = sim, mode
        self.sent, self.closed = [], 0

    def write(self, data):
        if self.mode == "fail":
            raise DeviceError("/dev/bus/usb/001/005: sending to endpoint 0x02 failed: Connection timed out")
        self.sent.append(data)
        if self.mode == "deliver":
            SimTransport(self.sim).write_output(data)

    def close(self):
        self.closed += 1

    def describe(self):
        return "USB bulk endpoint 0x02 (interface 0, test)"


def make(tmp_path, mode="deliver", api=None):
    root = quadro_sysfs(tmp_path)
    rules = tmp_path / "rules.d"
    rules.mkdir()
    (rules / doctor.UDEV_RULE).write_text('SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0c70"\n')
    sim = SimDevice(World(speed=4.0), "quadro")
    bulk = Bulk(sim, mode)
    lines = []
    doc = doctor.Doctor(out=lines.append, hidraw_root=root, dev_usb=tmp_path / "dev", udev_dirs=[rules],
                        open_transport=lambda path: SimTransport(sim), channel_for=lambda node: ("bulk", bulk),
                        api=api)
    return doc, lines, bulk, sim


def test_report_lists_device_usb_layout_and_settings(tmp_path):
    doc, lines, bulk, _sim = make(tmp_path)
    doc.run()
    text = "\n".join(lines)
    assert "/dev/hidraw8  0c70:f00d  QUADRO  interface 1" in text
    assert "reports: 0x01 input 220, 0x02 output 11, 0x03 feature 961" in text
    assert "interface 0 class ff, driver none: 0x02 Bulk out, 0x81 Bulk in" in text
    assert "software sensor data would go to: USB bulk endpoint 0x02" in text
    assert "old version: no rule for the USB device node" in text
    assert "serial 10234-55001" in text and "status reports:" in text
    assert "fan1: curve, input virt1" in text and "checksum OK" in text
    assert "outputs: fan1 " in text and " rpm" in text
    assert "names given in aquasuite: temp1 “Water Temp”, temp2 “Ambient”, virt1 “Delta T”" in text
    assert "== Summary ==" in text
    assert bulk.sent == []                                  # without --feed-test nothing is sent


def test_feed_test_confirms_working_software_sensors(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "TEST_SENDS", 3)
    doc, lines, bulk, sim = make(tmp_path)
    doc.run(feed_test=True)
    text = "\n".join(lines)
    assert "sending software sensor 16 = 21.37" in text      # the highest slot no fan uses
    assert "reports the value back" in text
    assert bulk.sent[-1][0x10:0x20] != bulk.sent[0][0x10:0x20]   # the slot is cleared afterwards
    assert sim.soft[15] is None and bulk.closed
    assert "still answers, settings report readable" in text


def test_feed_test_reports_data_the_device_ignores(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "TEST_SENDS", 2)
    doc, lines, _bulk, _sim = make(tmp_path, mode="swallow")
    assert doc.run(feed_test=True) == 1
    text = "\n".join(lines)
    assert "accepted by USB, but the QUADRO doesn't show it" in text


def test_feed_test_reports_a_failing_transfer(tmp_path):
    doc, lines, _bulk, _sim = make(tmp_path, mode="fail")
    doc.run(feed_test=True)
    text = "\n".join(lines)
    assert "send 1: FAILED" in text and "Connection timed out" in text
    assert "afterwards the QUADRO still answers" in text


def test_report_includes_what_the_service_does(tmp_path):
    from aquasuitelinux.core.session import local_engine
    api = local_engine(demo=True)
    try:
        api.engine.tick()
        doc, lines, bulk, _sim = make(tmp_path, api=api)
        doc.run(feed_test=True)
        text = "\n".join(lines)
        assert "== What the service does ==" in text
        assert "sending sensor values to devices: on" in text
        assert "quadro-10234-55001/fan1 (Radiator top 1):" in text
        assert "recent events:" in text
        assert "Stop it, run the test" in text and bulk.sent == []
    finally:
        api.close()


@pytest.mark.parametrize("args", [["doctor"], ["doctor", "--feed-test"]])
def test_cli_doctor_runs_without_devices(args, capsys):
    from aquasuitelinux.cli import main
    main(args)
    out = capsys.readouterr().out
    assert "== System ==" in out and "== Summary ==" in out


def _engine_api(cfg=None):
    from aquasuitelinux.core.api import LocalAPI
    from aquasuitelinux.core.config import Config
    from aquasuitelinux.core.demo import demo_provider
    from aquasuitelinux.core.engine import Engine
    eng = Engine(cfg or Config(), demo_provider(), mode="service")
    eng.tick()
    return LocalAPI(eng)


def _user_setup():
    from aquasuitelinux.core import config as config_mod
    from aquasuitelinux.core.demo import demo_config
    path = config_mod.user_config_path()
    config_mod.save(demo_config(), path)
    return path


def test_empty_service_with_a_setup_left_in_the_user_config(tmp_path):
    """What happened on real hardware: the service was enabled by hand and started with no setup."""
    path = _user_setup()
    api = _engine_api()
    try:
        doc, lines, _bulk, _sim = make(tmp_path, api=api)
        doc.run()
        text = "\n".join(lines)
        assert "background service: 0 virtual sensors, 0 controllers, 0 fans assigned" in text
        assert "2 virtual sensors, 5 controllers, 5 fans assigned, 2 alarms" in text
        assert f"aquactl config --load {path}" in text
        assert any("the service has no fan setup" in f for f in doc.findings)
    finally:
        api.close()


def test_stored_curve_on_an_unfed_software_sensor_is_reported(tmp_path):
    doc, _lines, _bulk, _sim = make(tmp_path)
    doc.run()
    # the simulated QUADRO comes with curves on software sensor 1, like a QUADRO set up by aquasuite
    assert any("fan1 runs a curve stored on the device that reads software sensor 1" in f for f in doc.findings)
