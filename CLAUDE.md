# AquasuiteLinux — repository notes for Claude

A Linux replacement for Aquacomputer's aquasuite: monitoring, virtual sensors (Delta T …),
curve/PID/two-point/fixed/follow/mix controllers, alarms, profiles, aquasuite import, a background
service (`aquasuited`) and the `aquactl` CLI. Python ≥ 3.10; PySide6 for the GUI; primary target
Arch Linux (the user runs a QUADRO).

## Layout
- `aquasuitelinux/core/` — stdlib only, no Qt imports:
  `devices.py` (per-device offsets: status report, settings report, fans, software sensors),
  `status.py` (decode/encode status reports), `control.py` (settings report codec, software sensor
  and Leakshield feed reports, aquaero / aquastream XT manual writes), `crc.py` (CRC-16/USB),
  `transport.py` (hidraw ioctls, report descriptor parser, sysfs enumeration), `device.py`
  (`HidDevice` over any transport, `HwmonDevice` fallback), `discovery.py` (`HardwareProvider`),
  `simulator.py` (water-loop model behind a fake transport; demo + tests), `virtual.py`,
  `controllers.py`, `alarms.py`, `system.py` (PC sensors), `engine.py` (the loop), `api.py`
  (`LocalAPI`/`ServiceAPI`, same methods), `ipc.py` (UNIX socket JSON lines, SO_PEERCRED),
  `service.py` (`aquasuited`), `session.py` (service or in-process engine), `aquasuite.py`
  (import + backups), `config.py` (dataclasses ↔ JSON), `demo.py`.
- `aquasuitelinux/gui/` — PySide6. `bridge.py` polls snapshots on a worker thread and serialises
  config writes; pages: `overview.py`, `sensors_page.py`, `controllers_page.py` (+ `curve_editor.py`),
  `outputs_page.py`, `alarms_page.py`; `dialogs.py`; `charts.py` (painted, no QtCharts).
- `aquasuitelinux/cli.py` — `aquactl`. `tools/screenshot.py` — renders docs/screenshots.
- `tests/data/` — QUADRO reports captured from real hardware (from the aquacomputer_d5next re-docs).

## Invariants / gotchas
- Settings reports: always read-modify-write the full report, `seal()` the CRC, then send the save
  report (`HidDevice.write_control`). Never guess offsets that aren't in `devices.py`/`docs/PROTOCOL.md`.
- Flash wear: device placement writes settings only when the desired curve/limits differ from what's
  stored (`Engine._ensure_device_settings` compares checksums). Software placement writes only on
  ≥ 1 % change and at most every `write_interval` s. Keep both properties.
- Software sensor (and Leakshield) data goes over usbfs bulk to endpoint 0x02 on the vendor interface
  (`transport.UsbBulkChannel`, found by `feed_channel`). hidraw `write()` does NOT work on a real QUADRO:
  its HID interface has no OUT endpoint, so it becomes SET_REPORT and fails with EPROTO/ETIMEDOUT after
  blocking up to 5 s. A failed send marks the device `feed_broken` at once (no retries; rescan clears it).
- Curves that need a feed start as placement "pending": nothing is written until the device echoes the
  values (`feed_ok`), then they move onto the device; no confirmation within `PENDING_SECONDS` → software.
  Octo / farbwerk 360 layouts are unverified.
- Software placement neutralises the device's min/max (0 / 100 %) because scaling is done in software;
  device placement writes the output's min/max/fallback/flags.
- Unmanaged outputs (no controller) must never be written.
- Safety nets for untested hardware behaviour: `MAX_REWRITES` (a device that doesn't keep our settings
  → `settings_stuck`, software control), `_verify_device_outputs` (reported power far from the curve for
  `CURVE_CHECK_SECONDS` → `curve_suspect`, software control), pumps always hold minimum power.
- `Engine(control=False)` is read-only (used by `aquactl` without the service) — no writes at all,
  including on shutdown.
- Flag bits (hold min = bit 1, start boost = bit 2) follow the out-of-tree driver.
- The GUI shares the EZP2019Linux / FirmwareLab / ReolinkLinux design system (`theme.py`, `icons.py`,
  `widgets.py`); keep it visually consistent with those apps.
- Worker results come back through `worker.run(fn, done, error)`; callbacks run on the GUI thread.

## Running
```sh
python -m venv --system-site-packages .venv && . .venv/bin/activate
pip install -e ".[dev]"
make test            # QT_QPA_PLATFORM=offscreen pytest
make lint            # ruff
./aquasuitelinux.sh --demo
python -m aquasuitelinux.core.service --demo --socket /tmp/aq.sock   # service against the simulator
```
