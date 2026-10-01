"""aquactl against the simulated devices, the service socket and the config file."""

import json

import pytest

from aquasuitelinux import cli


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_status_and_sensors_demo(capsys):
    code, out, _ = run(capsys, "--demo", "status")
    assert code == 0 and "QUADRO" in out and "Coolant ΔT" in out and "Radiator top 1" in out
    code, out, _ = run(capsys, "--demo", "sensors", "--json")
    data = json.loads(out)
    assert any(r["id"] == "virtual/deltat" for r in data)


def test_get_outputs_devices(capsys):
    code, out, _ = run(capsys, "--demo", "get", "virtual/deltat", "--raw")
    assert code == 0 and float(out.strip()) > 0
    code, out, _ = run(capsys, "--demo", "outputs", "-v")
    assert "device" in out and "software" in out
    code, out, _ = run(capsys, "--demo", "devices")
    assert "quadro-10234-55001" in out and "device_curves" in out


def test_import_dry_run_from_device_and_file(capsys):
    code, out, _ = run(capsys, "--demo", "import", "quadro-10234-55001", "--dry-run")
    assert code == 0 and "software sensor 1" in out
    code, out, _ = run(capsys, "import", "--file", "tests/data/quadro_control.bin", "--dry-run")
    assert code == 0 and "QUADRO" in out


def test_config_commands_edit_user_config(capsys, tmp_path):
    cfg_path = tmp_path / "c.json"
    code, out, _ = run(capsys, "--standalone", "--config", str(cfg_path), "deltat", "q/temp1", "q/temp2")
    assert code == 0 and "saved to" in out
    data = json.loads(cfg_path.read_text())
    assert data["virtual_sensors"][0]["inputs"] == ["q/temp1", "q/temp2"]
    code, out, _ = run(capsys, "--standalone", "--config", str(cfg_path), "profile")
    assert "* Default" in out


def test_set_requires_engine(capsys, tmp_path):
    with pytest.raises(SystemExit, match="needs the running app"):
        cli.main(["--standalone", "--config", str(tmp_path / "c.json"), "set", "q/fan1", "50"])


def test_cli_against_service(capsys, tmp_path, monkeypatch):
    from aquasuitelinux.core.api import LocalAPI
    from aquasuitelinux.core.demo import demo_config, demo_provider
    from aquasuitelinux.core.engine import Engine
    from aquasuitelinux.core.ipc import Server
    eng = Engine(demo_config(), demo_provider(), mode="service")
    eng.tick()
    sock = tmp_path / "s.sock"
    server = Server(LocalAPI(eng), sock)
    server.start()
    monkeypatch.setenv("AQUASUITELINUX_SOCKET", str(sock))
    try:
        code, out, _ = run(capsys, "status")
        assert code == 0 and "service" in out
        code, out, _ = run(capsys, "profile", "Performance")
        assert "Performance" in out
        assert eng.config.profile(eng.config.active_profile).name == "Performance"
        code, out, _ = run(capsys, "assign", "quadro-10234-55001/fan4", "none")
        assert eng.config.outputs["quadro-10234-55001/fan4"].controller == ""
        code, out, _ = run(capsys, "backup", "quadro-10234-55001", "-o", str(tmp_path / "b.json"))
        assert json.loads((tmp_path / "b.json").read_text())["kind"] == "quadro"
        code, out, _ = run(capsys, "settings", "quadro-10234-55001", "--flow-pulses", "250")
        assert eng.device_settings("quadro-10234-55001")["flow_pulses"] == 250
    finally:
        server.close()
        eng.stop()


def test_help_and_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "aquactl" in capsys.readouterr().out


def test_import_aquasuite_backup_sets_up_the_delta_t(capsys, tmp_path):
    from test_import import aquasuite_device_backup
    f = tmp_path / "quadro_profile.xml"
    f.write_bytes(aquasuite_device_backup(serial="10234-55001"))
    assert cli.main(["--demo", "import", "--file", str(f), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "aquasuite backup of QUADRO 10234-55001" in out
    assert "Fan 1: curve on software sensor 1 “Delta T”" in out
    # the demo already has a Delta T over the same two sensors: it is reused
    assert "software sensor 1 “Delta T” ← virtual/deltat" in out
    assert "sensor names: temp1 “Water Temp”, temp2 “Ambient”" in out
