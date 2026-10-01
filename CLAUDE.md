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
  (import + backups), `config.py` (dataclasses ↔ JSON), `demo.py`, `doctor.py` (`aquactl doctor`).
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
- Feeds (software sensors, Leakshield) only run with `settings.device_feeds` (default **off**: the bulk path
  is unconfirmed on real hardware; the demo turns it on). Off → curves on remote inputs run in software.
- Curves that need a feed start as placement "pending": nothing is written until the device echoes the
  values (`feed_ok`), then they move onto the device; no confirmation within `PENDING_SECONDS` → software.
  A device that goes quiet while fed, or disconnects within `FEED_TRUST_SECONDS` of the first send, is
  marked `feed_broken` and its bulk interface released (`stop_feed`). Octo / farbwerk 360 layouts are unverified.
- Every tick step runs through `Engine._step`: an exception is reported once per `ERROR_REPEAT_SECONDS`
  and the other steps (and the snapshot) still run. Keep new steps inside it.
- Two configs: the service's (`/etc/aquasuitelinux/config.json`) and the app's without it
  (`~/.config/aquasuitelinux/config.json`). Enabling the service by hand leaves it empty; the GUI offers to
  move the user's setup into an empty service (`MainWindow.offer_settings_move`), `set_config` re-keys it to
  the connected devices (`_adopt`), and `aquactl doctor` flags it.
- `aquactl doctor` (`core/doctor.py`) is the first thing to ask users for; `--feed-test` is the only part
  that writes (one unused software sensor slot, cleared afterwards) and refuses while the service runs.
- Software placement neutralises the device's min/max (0 / 100 %) because scaling is done in software;
  device placement writes the output's min/max/fallback/flags.
- Unmanaged outputs (no controller) must never be written.
- ARCTIC Fan Controller (`core/arctic.py`, spec `ARCTIC_FAN`, only in `BY_KIND`: its PID 0xF001 is the
  aquaero's): only through the Linux 7.2+ `arctic_fan` hwmon driver (no hidraw node). Every `pwmN` write
  sends all 10 channels from the driver's cache (0 after load/resume), so unassigned channels are set to the
  device's 40 % default before the first write — the one exception to "never write unmanaged outputs".
  Writes run on a background thread (each blocks ≤ 1 s for the device's ACK); resume is detected from the
  cleared cache and everything is re-sent. Software placement only.
- PC sensors (`system.py`) never read network hardware — Ethernet/Wi-Fi adapters and Ethernet PHYs
  (`is_network_hardware`): polling a NIC/PHY temperature every second broke a user's Ethernet.
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
