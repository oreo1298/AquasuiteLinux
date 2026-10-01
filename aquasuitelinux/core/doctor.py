"""``aquactl doctor``: everything needed to understand a problem, in one report to paste.

Only reads, with one exception: ``--feed-test`` sends software sensor values (the report aquasuite
sends) to one unused software sensor slot of each device for a few seconds, to find out whether
that works on this device, and then marks the slot unavailable again.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import __version__
from . import control
from .devices import BY_PRODUCT, SOFT_TEMPERATURE, VENDOR_ID, DeviceSpec
from .errors import AquaError
from .status import parse_status
from .transport import DEV_USB, SYSFS_HIDRAW, HidNode, HidrawTransport, enumerate_nodes, feed_channel, usb_layout

UDEV_RULE = "70-aquasuitelinux.rules"
UDEV_DIRS = (Path("/etc/udev/rules.d"), Path("/usr/lib/udev/rules.d"), Path("/lib/udev/rules.d"))
MODES = {control.MODE_MANUAL: "fixed power", control.MODE_PID: "target temperature (PID)",
         control.MODE_CURVE: "curve"}
TEST_VALUE = 21.37          # °C: unlikely to be mistaken for anything else in the slot
TEST_SENDS = 6


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _value(v, unit: str = "") -> str:
    if v is None:
        return "—"
    digits = 0 if unit in ("rpm", "%") else 2
    return f"{v:.{digits}f}{(' ' + unit) if unit else ''}" if isinstance(v, (int, float)) else f"{v}{unit}"


class Doctor:
    def __init__(self, out=print, hidraw_root: Path = SYSFS_HIDRAW, dev_usb: Path = DEV_USB,
                 udev_dirs=UDEV_DIRS, open_transport=HidrawTransport, channel_for=None, api="auto"):
        self.out = out
        self.hidraw_root = hidraw_root
        self.dev_usb = dev_usb
        self.udev_dirs = udev_dirs
        self.open_transport = open_transport
        self.channel_for = channel_for or (lambda node: feed_channel(node.path, self.hidraw_root, self.dev_usb))
        self._api = api
        self.findings: list[str] = []

    # ------------------------------------------------------------------ output helpers
    def head(self, text: str) -> None:
        self.out("")
        self.out(f"== {text} ==")

    def line(self, text: str, indent: int = 1) -> None:
        self.out("  " * indent + text)

    def finding(self, text: str) -> None:
        self.findings.append(text)
        self.line(f"! {text}")

    # ------------------------------------------------------------------ the report
    def run(self, feed_test: bool = False) -> int:
        self.out(f"AquasuiteLinux doctor — {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.system()
        api = self.service()
        self.configuration(api)
        self.udev()
        nodes = self.hardware()
        for node, spec in nodes:
            self.live(node, spec, api)
        if api is not None:
            self.service_state(api)
        if feed_test:
            self.feed_test(nodes, api)
        self.head("Summary")
        if self.findings:
            for f in self.findings:
                self.line(f"! {f}")
        else:
            self.line("nothing obviously wrong")
        if api is not None:
            try:
                api.close()
            except Exception:  # noqa: BLE001 - closing on exit
                pass
        return 1 if self.findings else 0

    def system(self) -> None:
        self.head("System")
        os_name = ""
        for line in _read(Path("/etc/os-release")).splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip('"')
        self.line(f"AquasuiteLinux {__version__}, Python {platform.python_version()}, "
                  f"kernel {platform.release()}{', ' + os_name if os_name else ''}")
        try:
            import grp
            import pwd
            pw = pwd.getpwuid(os.getuid())
            user = pw.pw_name
            groups = sorted({grp.getgrgid(g).gr_name for g in os.getgrouplist(user, pw.pw_gid) if _has_group(g)})
        except (ImportError, KeyError, OSError):
            user, groups = str(os.getuid()), []
        self.line(f"user {user} (uid {os.getuid()}), groups: {', '.join(groups) or '—'}")
        mod = Path("/sys/module/aquacomputer_d5next")
        if mod.exists():
            version = _read(mod / "version")
            self.line(f"kernel driver aquacomputer_d5next loaded{' (version ' + version + ')' if version else ''}")
        else:
            self.line("kernel driver aquacomputer_d5next not loaded (not needed)")

    def service(self):
        self.head("Background service")
        from .ipc import Client, socket_path
        path = socket_path()
        if shutil.which("systemctl"):
            states = []
            for verb in ("is-active", "is-enabled"):
                try:
                    r = subprocess.run(["systemctl", verb, "aquasuited.service"], capture_output=True, text=True,
                                       timeout=5)
                    states.append((r.stdout.strip() or r.stderr.strip() or "?").splitlines()[0])
                except (OSError, subprocess.SubprocessError) as exc:
                    states.append(f"? ({exc})")
            self.line(f"aquasuited.service: {states[0]}, {states[1]}")
        api = None
        if self._api == "auto":
            client = Client(path, timeout=5.0)
            if client.available():
                from .api import ServiceAPI
                api = ServiceAPI(client)
            else:
                client.close()
        elif self._api:
            api = self._api
        if api is None:
            self.line(f"not reachable at {path}: the app and aquactl use the devices directly")
            return None
        try:
            hello = api.hello()
            self.line(f"running: version {hello.get('version')}, mode {hello.get('mode')}, pid {hello.get('pid')}")
            if hello.get("version") != __version__:
                self.finding(f"the service runs version {hello.get('version')} but this is {__version__}: "
                             "restart it (sudo systemctl restart aquasuited)")
        except AquaError as exc:
            self.finding(f"the service doesn't answer: {exc}")
            return None
        return api

    def configuration(self, api) -> None:
        """Where the fan setup lives: the service has its own, separate from the one the app uses alone."""
        from . import config as config_mod
        self.head("Fan setup")
        path = _user_config_path()
        local = None
        if path.exists():
            try:
                local = config_mod.load(path)
            except AquaError as exc:
                self.finding(str(exc))
        self.line(f"app without the service ({path}): {local.setup_summary() if local else 'no file'}")
        if api is None:
            if local is None or not local.has_setup():
                self.line("nothing is set up yet: no fan is controlled")
            return
        try:
            remote = config_mod.Config.from_dict(api.get_config())
        except AquaError as exc:
            self.finding(f"the service doesn't answer: {exc}")
            return
        self.line(f"background service: {remote.setup_summary()}")
        if remote.has_setup():
            return
        if local is not None and local.has_setup():
            self.finding(f"the service has no fan setup, so it controls none of your fans. Your setup "
                         f"({local.setup_summary()}) is in {path}, which only the app uses without the service. "
                         f"Move it: open the app (it offers to), or run: aquactl config --load {path}")
        else:
            self.finding("no fan is set up, so the service controls none of your fans")

    def udev(self) -> None:
        self.head("Permissions (udev rule)")
        found = [d / UDEV_RULE for d in self.udev_dirs if (d / UDEV_RULE).exists()]
        if not found:
            self.line(f"{UDEV_RULE} not installed: only root (and the service) can open the devices")
            return
        for f in found:
            text = _read(f)
            usb = 'SUBSYSTEM=="usb"' in text
            self.line(f"{f}{'' if usb else ' (old version: no rule for the USB device node)'}")

    def hardware(self) -> list[tuple[HidNode, DeviceSpec]]:
        self.head("Devices")
        nodes = enumerate_nodes(VENDOR_ID, self.hidraw_root)
        if not nodes:
            self.finding("no Aquacomputer device found (is the USB cable plugged into a USB header?)")
            return []
        known: list[tuple[HidNode, DeviceSpec]] = []
        seen_usb: set[str] = set()
        for n in nodes:
            spec = BY_PRODUCT.get(n.product)
            access = "read/write" if os.access(n.path, os.R_OK | os.W_OK) else "NO ACCESS"
            self.line(f"{n.path}  {n.vendor:04x}:{n.product:04x}  {spec.name if spec else 'unknown device'}  "
                      f"interface {n.interface}  [{access}]")
            if n.reports:
                self.line("reports: " + ", ".join(f"{r.report_id:#04x} {r.kind} {r.length}" for r in n.reports), 2)
            if spec is None:
                continue
            if access == "NO ACCESS":
                self.finding(f"no permission for {n.path}: install the udev rule and re-plug, or use the service")
            known.append((n, spec))
            usb_dir, hid_if, endpoints = usb_layout(n.path, self.hidraw_root)
            if usb_dir is None or str(usb_dir) in seen_usb:
                continue
            seen_usb.add(str(usb_dir))
            bus, dev = _read(usb_dir / "busnum"), _read(usb_dir / "devnum")
            devnode = self.dev_usb / f"{int(bus or 0):03d}" / f"{int(dev or 0):03d}"
            node_access = "read/write" if os.access(devnode, os.R_OK | os.W_OK) else "NO ACCESS"
            self.line(f"USB {usb_dir.name}: {devnode} [{node_access}], HID interface {hid_if}", 2)
            for intf in sorted(usb_dir.glob(f"{usb_dir.name}:*")):
                num = _read(intf / "bInterfaceNumber")
                driver = os.path.basename(os.path.realpath(intf / "driver")) if (intf / "driver").exists() else "none"
                eps = ", ".join(f"{e.address:#04x} {e.kind} {e.direction}" for e in endpoints
                                if f"{e.interface:02x}" == num.lower().zfill(2))
                self.line(f"interface {int(num or '0', 16)} class {_read(intf / 'bInterfaceClass') or '?'}, "
                          f"driver {driver}: {eps or 'no endpoints'}", 3)
            if spec.soft_sensors or spec.leakshield_feed:
                mode, channel = self.channel_for(n)
                self.line(f"software sensor data would go to: {channel.describe() if channel else mode}", 2)
        return known

    def live(self, node: HidNode, spec: DeviceSpec, api) -> None:
        self.head(f"{spec.name} at {node.path}: live data")
        if spec.status_via_feature and api is not None:
            self.line("read by the service (see below)")
            return
        try:
            t = self.open_transport(node.path)
        except AquaError as exc:
            self.finding(str(exc))
            return
        try:
            self._live(node, spec, api, t)
        except AquaError as exc:
            if spec.multi_interface:
                self.line(f"(not the sensor interface: {exc})")
            else:
                self.finding(f"{spec.name}: {exc}")
        finally:
            t.close()

    def _live(self, node: HidNode, spec: DeviceSpec, api, t) -> None:
        from .device import HidDevice
        dev = HidDevice(spec, t, node.path)
        count, began = 0, time.monotonic()
        if not spec.status_via_feature:
            while time.monotonic() - began < 2.0:
                count += sum(1 for r in t.read_input(0.5) if r and r[0] == spec.status_id)
        self.line(f"serial {dev.serial}, firmware {dev.firmware}, power-on count {dev.cycles}")
        if not spec.status_via_feature:
            self.line(f"status reports: {count} in 2 s")
            if count == 0:
                self.finding(f"{spec.name} sends no status reports")
        groups: dict[str, list[str]] = {}
        outputs: dict[str, dict[str, float | None]] = {}
        values: dict[str, float | None] = {}
        for r in dev.poll():
            key = r.id.split("/", 1)[1]
            values[key] = r.value
            if "." in key:                                     # fan1.rpm, fan1.percent, …
                name, _, what = key.partition(".")
                outputs.setdefault(name, {})[what] = r.value
                continue
            groups.setdefault(r.group or "other", []).append(f"{key} {_value(r.value, r.unit)}")
        for g, items in groups.items():
            self.line(f"{g}: " + ", ".join(items))
        if outputs:
            self.line("outputs: " + ", ".join(
                f"{name} {_value(v.get('percent'), '%')} {_value(v.get('rpm'), 'rpm')}" for name, v in outputs.items()))
        raw = dev.raw_status() or b""
        if spec.virtual and spec.virtual_types is not None and len(raw) >= spec.virtual_types + spec.virtual[1]:
            types = raw[spec.virtual_types:spec.virtual_types + spec.virtual[1]]
            self.line("software sensor types: " + " ".join(f"{b:02x}" for b in types))
        settings = None
        if "settings" in dev.capabilities():
            key = dev.key
            try:
                if api is not None:
                    settings = api.device_settings(key)      # the service reads it, paced with its writes
                else:
                    from .engine import Engine
                    settings = control.ControlReport(spec, dev.read_control(fresh=True)).to_dict()
                    settings["names"] = Engine._device_names(dev)
            except AquaError as exc:
                self.finding(f"{spec.name}: reading the settings report failed: {exc}")
        if settings:
            self.settings(spec, settings, values)

    def settings(self, spec: DeviceSpec, data: dict, values: dict | None = None) -> None:
        valid = data.get("valid", True) or not spec.ctrl_crc
        self.line(f"settings report: {'checksum OK' if valid else 'CHECKSUM WRONG'}" if spec.ctrl_crc
                  else "settings report: read")
        if not valid:
            self.finding(f"{spec.name}: the settings report's checksum doesn't match (unknown firmware layout?)")
        from .aquasuite import custom_name
        named = {k: v for k, v in (data.get("names") or {}).items() if custom_name(v)}
        if named:
            self.line("names given in aquasuite: " + ", ".join(f"{k} “{v}”" for k, v in named.items()))
        for f in data.get("fans", []):
            mode = f["mode"]
            name = MODES.get(mode) or (f"follow fan {mode - control.MODE_FOLLOW + 1}"
                                       if mode >= control.MODE_FOLLOW else f"mode {mode}")
            src = f.get("source")
            src_text = "—" if src is None else (control.source_key(spec, src) or str(src))
            text = f"{f['key']}: {name}, input {src_text}, fixed power {f['pwm']:.1f} %"
            st = f.get("setup")
            if st:
                text += (f", min {st['min']:.0f} % max {st['max']:.0f} % fallback {st['fallback']:.0f} %"
                         f"{', hold min' if st['hold_min'] else ''}{', start boost' if st['start_boost'] else ''}")
            if mode == control.MODE_CURVE and f.get("curve"):
                c = f["curve"]
                text += f", curve {c[0][0]:g}→{c[0][1]:g} % … {c[-1][0]:g}→{c[-1][1]:g} %"
            self.line(text, 2)
            if (mode == control.MODE_CURVE and src_text.startswith("virt") and values is not None
                    and values.get(src_text) is None):
                self.finding(f"{spec.name} {f['key']} runs a curve stored on the device that reads software sensor "
                             f"{src_text[4:]}, but nothing sends that sensor a value, so the curve can't work")

    def service_state(self, api) -> None:
        self.head("What the service does")
        try:
            snap = api.snapshot()
            cfg = api.get_config()
        except AquaError as exc:
            self.finding(f"the service doesn't answer: {exc}")
            return
        s = cfg.get("settings", {})
        self.line(f"mode {snap.get('mode')}, running for {snap.get('uptime')} s, "
                  f"sending sensor values to devices: {'on' if s.get('device_feeds') else 'off'}")
        for d in snap.get("devices", []):
            self.line(f"{d['key']} ({d['backend']}): {'online' if d.get('online') else 'OFFLINE'}"
                      f"{', ' + d['error'] if d.get('error') else ''}; {d.get('writes', 0)} settings writes; "
                      f"software sensor data: {d.get('feed') or 'not used'}"
                      f"{' (' + d['feed_path'] + ')' if d.get('feed_path') else ''}")
            if not d.get("online"):
                self.finding(f"{d['key']} is offline in the service: {d.get('error') or 'no data'}")
            if d.get("feed") == "broken":
                self.finding(f"{d['key']} doesn't take software sensor data (see the events below)")
        self.line("outputs:")
        for o in snap.get("outputs", []):
            self.line(f"{o['id']} ({o['name']}): {o['placement']}, {o['controller_name'] or 'no controller'}, "
                      f"target {_value(o.get('target'), '%')}, reported {_value(o.get('reported'), '%')}, "
                      f"{_value(o.get('rpm'), 'rpm')}", 2)
            if o.get("reason"):
                self.line(f"→ {o['reason']}", 3)
        self.line("controllers:")
        for c in snap.get("controllers", []):
            self.line(f"{c['name']} ({c['kind']}): input {c['input'] or '—'} = {_value(c.get('input_value'))}, "
                      f"output {_value(c.get('output'), '%')}", 2)
        for f in snap.get("feeds", []):
            self.line(f"feed: {f['device']} slot {f['slot']} ← {f['source']} = {_value(f.get('value'))}"
                      f"{' (auto)' if f.get('auto') else ''}")
        for p in snap.get("problems", []):
            self.line(f"problem: {p}")
        events = snap.get("events", [])[-40:]
        if events:
            self.line("recent events:")
            for t, level, text in events:
                self.line(f"{time.strftime('%H:%M:%S', time.localtime(t))} {level:<7} {text}", 2)
            errors = [e for e in events if e[1] == "error"]
            if errors:
                self.finding(f"{len(errors)} error event(s) in the service, the last: {errors[-1][2]}")

    # ------------------------------------------------------------------ feed test
    def feed_test(self, nodes: list[tuple[HidNode, DeviceSpec]], api) -> None:
        self.head("Software sensor test")
        if api is not None:
            self.line("The background service is running and would interfere. Stop it, run the test, start it again:")
            self.line("sudo systemctl stop aquasuited && aquactl doctor --feed-test; sudo systemctl start aquasuited",
                      2)
            return
        self.line("(if the AquasuiteLinux window runs without the service, close it first)")
        tested = False
        for node, spec in nodes:
            if spec.soft_sensors and not spec.multi_interface:
                tested = True
                self._feed_test_one(node, spec)
        if not tested:
            self.line("no device with software sensors found")

    def _feed_test_one(self, node: HidNode, spec: DeviceSpec) -> None:
        from .device import HidDevice
        soft = spec.soft_sensors
        self.line(f"{spec.name} at {node.path}:")
        mode, channel = self.channel_for(node)
        if channel is None and mode != "hid":
            self.finding(f"{spec.name}: no USB endpoint for software sensor data ({mode})")
            return
        try:
            t = self.open_transport(node.path)
        except AquaError as exc:
            self.finding(f"{spec.name}: {exc}")
            return
        try:
            dev = HidDevice(spec, t, node.path)
        except AquaError as exc:
            t.close()
            self.finding(f"{spec.name}: {exc}")
            return
        sent_any = attempted = False
        try:
            values = {k: v for k, _l, _kind, v, _g in parse_status(spec, dev.raw_status() or b"")}
            used: set[int] = set()
            if "settings" in dev.capabilities():
                try:
                    rep = control.ControlReport(spec, dev.read_control(fresh=True))
                    for i, f in enumerate(spec.fans):
                        if f.ctrl is not None:
                            key = control.source_key(spec, rep.fan(i).source)
                            if key and key.startswith("virt"):
                                used.add(int(key[4:]))
                except AquaError as exc:
                    self.finding(f"{spec.name}: reading the settings report failed: {exc}")
                    return
            free = [s for s in range(soft.slots, 0, -1) if s not in used and values.get(f"virt{s}") is None]
            if not free:
                self.line("every software sensor slot is in use or has a value; nothing tested", 2)
                return
            slot = free[0]
            data = [None] * soft.slots
            data[slot - 1] = (TEST_VALUE, SOFT_TEMPERATURE)
            report = control.soft_sensor_report(soft, data)
            where = channel.describe() if channel else "HID output report"
            self.line(f"sending software sensor {slot} = {TEST_VALUE} °C ({len(report)} bytes) to {where}", 2)
            echoed_after = None
            reports_seen = 0
            began = time.monotonic()
            for attempt in range(1, TEST_SENDS + 1):
                t0 = time.monotonic()
                attempted = True
                try:
                    if channel is not None:
                        channel.write(report)
                    else:
                        t.write_output(report)
                    sent_any = True
                except AquaError as exc:
                    self.line(f"send {attempt}: FAILED after {(time.monotonic() - t0) * 1000:.0f} ms: {exc}", 2)
                    self.finding(f"{spec.name}: sending software sensor data failed: {exc}")
                    break
                took = (time.monotonic() - t0) * 1000
                got, value = self._watch(t, spec, slot, 1.0)
                reports_seen += got
                self.line(f"send {attempt}: ok ({took:.0f} ms); {got} status report(s) in the next second, "
                          f"software sensor {slot} = {_value(value)}", 2)
                if got == 0:
                    self.finding(f"{spec.name} stopped sending status reports after receiving software sensor data")
                    break
                if value is not None and abs(value - TEST_VALUE) < 0.06:
                    echoed_after = time.monotonic() - began
                    break
            if echoed_after is not None:
                self.line(f"RESULT: the {spec.name} reports the value back after {echoed_after:.1f} s — software "
                          "sensors work. You can turn on “Send sensor values to devices” in Settings.", 2)
            elif sent_any and reports_seen:
                self.line(f"RESULT: the data was accepted by USB, but the {spec.name} doesn't show it — software "
                          "sensors don't work this way yet. Keep “Send sensor values to devices” off.", 2)
                self.findings.append(f"{spec.name}: software sensor data is accepted but not shown by the device")
        finally:
            if sent_any:
                try:
                    clear = control.soft_sensor_report(soft, [None] * soft.slots)
                    if channel is not None:
                        channel.write(clear)
                    else:
                        t.write_output(clear)
                except AquaError:
                    pass
            if channel is not None:
                channel.close()
            if attempted:
                self._after_test(t, spec, dev)
            t.close()

    def _after_test(self, t, spec: DeviceSpec, dev) -> None:
        try:
            got, _v = self._watch(t, spec, 1, 1.5)
            ok = "still answers" if got else "SENDS NO STATUS REPORTS"
            if "settings" in dev.capabilities():
                dev.read_control(fresh=True)
                ok += ", settings report readable"
            self.line(f"afterwards the {spec.name} {ok}", 2)
            if not got:
                self.findings.append(f"{spec.name} sent no status reports after the test")
        except AquaError as exc:
            self.line(f"afterwards: {exc}", 2)
            self.findings.append(f"{spec.name} after the test: {exc}")

    @staticmethod
    def _watch(t, spec: DeviceSpec, slot: int, seconds: float) -> tuple[int, float | None]:
        """Status reports seen within ``seconds`` and the last value of software sensor ``slot``."""
        got, value = 0, None
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            for rep in t.read_input(min(left, 0.5)):
                if rep and rep[0] == spec.status_id:
                    got += 1
                    vals = {k: v for k, _l, _kind, v, _g in parse_status(spec, rep)}
                    value = vals.get(f"virt{slot}")
        return got, value


def _user_config_path() -> Path:
    """The app's own configuration, for the user who ran ``sudo aquactl doctor`` too."""
    from . import config as config_mod
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user:
        try:
            import pwd
            return Path(pwd.getpwnam(sudo_user).pw_dir) / ".config" / "aquasuitelinux" / "config.json"
        except (ImportError, KeyError):
            pass
    return config_mod.user_config_path()


def _has_group(gid: int) -> bool:
    try:
        import grp
        grp.getgrgid(gid)
        return True
    except (ImportError, KeyError):
        return False


def main(feed_test: bool = False) -> int:
    return Doctor(out=lambda s: print(s, flush=True)).run(feed_test=feed_test)


if __name__ == "__main__":
    sys.exit(main("--feed-test" in sys.argv))
