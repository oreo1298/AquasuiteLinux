"""``aquactl``: AquasuiteLinux from the command line (scripts, SSH, status bars).

It talks to the background service when it runs. Without it, read-only commands open the
devices directly (without taking over any fan), and configuration commands edit your
configuration file (``~/.config/aquasuitelinux/config.json``).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

from . import __app_name__, __version__
from .core import aquasuite
from .core import config as config_mod
from .core.config import Config, VirtualSensorConfig, new_id
from .core.devices import BY_KIND, VENDOR_ID
from .core.errors import AquaError


# ---------------------------------------------------------------------- connection
class Session:
    """The service if it runs; otherwise a read-only local engine plus the user's config file."""

    def __init__(self, args):
        from .core import session
        self.args = args
        self.api = None
        self.local = False
        if args.demo:
            self.api = session.local_engine(demo=True, control=True)
            self.local = True
        elif not args.standalone and session.service_running():
            self.api = session.connect(prefer_service=True)
        else:
            self.api = session.local_engine(control=False, config_path=Path(args.config) if args.config else None)
            self.local = True

    @property
    def mode(self) -> str:
        return self.api.mode

    def snapshot(self) -> dict:
        if self.local:
            # give freshly opened devices a moment to send their first reports
            time.sleep(0.3)
            return self.api.engine.tick()
        return self.api.snapshot()

    def config(self) -> Config:
        if self.local and not self.args.demo:
            return config_mod.load(self.config_path())
        return Config.from_dict(self.api.get_config())

    def config_path(self) -> Path:
        return Path(self.args.config) if self.args.config else config_mod.user_config_path()

    def save_config(self, cfg: Config) -> str:
        if self.local and not self.args.demo:
            config_mod.save(cfg, self.config_path(), 0o600)
            return f"saved to {self.config_path()} (applies when the app or the service runs)"
        self.api.set_config(cfg.to_dict())
        return "applied"

    def close(self) -> None:
        try:
            self.api.close()
        except Exception:  # noqa: BLE001 - closing on exit
            pass


def _fmt(value, unit: str = "") -> str:
    if value is None:
        return "—"
    digits = {"rpm": 0, "%": 0, "L/h": 0, "V": 2, "A": 3}.get(unit, 1)
    return f"{value:.{digits}f} {unit}".strip()


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _find(items: list[dict], key: str, what: str, field: str = "id") -> dict:
    wanted = key.lower()
    for it in items:
        if it[field].lower() == wanted:
            return it
    matches = [it for it in items if wanted in it[field].lower() or wanted in str(it.get("name", "")).lower()
               or wanted in str(it.get("label", "")).lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"No {what} matches {key!r}")
    raise SystemExit(f"{key!r} matches several {what}s: " + ", ".join(m[field] for m in matches))


# ---------------------------------------------------------------------- commands
def cmd_status(s: Session, args) -> int:
    snap = s.snapshot()
    if args.json:
        _print_json(snap)
        return 0
    print(f"{__app_name__} {__version__} — {s.mode}" + (f", profile {snap['profile']}" if snap.get("profile") else ""))
    if not snap["devices"]:
        print("No Aquacomputer devices found.")
    readings = {r["id"]: r for r in snap["readings"]}
    for d in snap["devices"]:
        print(f"\n{d['name']}  ({d['key']}, {d['backend']}{'' if d['online'] else ', OFFLINE'})")
        for r in snap["readings"]:
            if r["source"] == d["key"] and r["value"] is not None and r["group"] in ("Temperatures", "Flow",
                                                                                        "Coolant", "Pressure"):
                print(f"  {r['label']:<28} {_fmt(r['value'], r['unit'])}")
    virt = [r for r in snap["readings"] if r["source"] == "virtual"]
    if virt:
        print("\nVirtual sensors")
        for r in virt:
            print(f"  {r['label']:<28} {_fmt(r['value'], r['unit'])}")
    if snap["outputs"]:
        print("\nFans and pumps")
        for o in snap["outputs"]:
            power = o["reported"] if o["reported"] is not None else o["target"]
            ctrl = o["controller_name"] or "not managed"
            print(f"  {o['name']:<22} {_fmt(o['rpm'], 'rpm'):>9}  {_fmt(power, '%'):>5}  {o['placement']:<9} {ctrl}")
    active = [a for a in snap["alarms"] if a["active"]]
    for a in active:
        print(f"\nALARM: {a['name']} (value {a['value']})")
    for p in snap.get("problems", []):
        print(f"\nnote: {p}")
    del readings
    return 1 if active else 0


def cmd_sensors(s: Session, args) -> int:
    snap = s.snapshot()
    rows = [r for r in snap["readings"] if args.all or r["value"] is not None]
    if args.json:
        _print_json(rows)
        return 0
    for r in rows:
        print(f"{r['id']:<44} {_fmt(r['value'], r['unit']):>12}  {r['label']}")
    return 0


def cmd_get(s: Session, args) -> int:
    snap = s.snapshot()
    r = _find(snap["readings"], args.sensor, "sensor")
    print(r["value"] if args.raw else _fmt(r["value"], r["unit"]))
    return 0 if r["value"] is not None else 1


def cmd_devices(s: Session, args) -> int:
    snap = s.snapshot()
    if args.json:
        _print_json(snap["devices"])
        return 0
    for d in snap["devices"]:
        print(f"{d['key']:<28} {d['model']:<22} fw {d['firmware']:<6} {d['backend']:<9} "
              f"{', '.join(d['capabilities'])}")
    for p in snap.get("problems", []):
        print(f"note: {p}")
    return 0


def cmd_outputs(s: Session, args) -> int:
    snap = s.snapshot()
    if args.json:
        _print_json(snap["outputs"])
        return 0
    for o in snap["outputs"]:
        power = o["reported"] if o["reported"] is not None else o["target"]
        print(f"{o['id']:<30} {o['name']:<20} {_fmt(o['rpm'], 'rpm'):>9} {_fmt(power, '%'):>5}  "
              f"{o['placement']:<9} {o['controller_name'] or '—'}")
        if o.get("reason") and args.verbose:
            print(f"    {o['reason']}")
    return 0


def cmd_controllers(s: Session, args) -> int:
    cfg = s.config()
    for c in cfg.controllers:
        detail = {"curve": lambda: " → ".join(f"{x:g}:{y:g}%" for x, y in c.points),
                  "target": lambda: f"target {c.target:g}",
                  "two_point": lambda: f"on ≥ {c.on_above:g}, off ≤ {c.off_below:g}",
                  "fixed": lambda: f"{c.power:g} %",
                  "follow": lambda: c.follow,
                  "mix": lambda: f"{c.mix} of {', '.join(c.sources)}"}.get(c.kind, lambda: "")()
        print(f"{c.id:<10} {c.name:<28} {c.kind:<10} {c.input:<36} {detail}")
    return 0


def cmd_assign(s: Session, args) -> int:
    cfg = s.config()
    ctrl = None
    if args.controller.lower() not in ("none", "-", ""):
        ctrl = next((c for c in cfg.controllers if c.id == args.controller or
                     c.name.lower() == args.controller.lower()), None)
        if ctrl is None:
            raise SystemExit(f"No controller {args.controller!r}. See: aquactl controllers")
    cfg.output(args.output).controller = ctrl.id if ctrl else ""
    if args.placement:
        cfg.output(args.output).placement = args.placement
    print(f"{args.output}: {ctrl.name if ctrl else 'not managed'} — {s.save_config(cfg)}")
    return 0


def cmd_set(s: Session, args) -> int:
    if s.local and not args.demo:
        raise SystemExit("Setting a power needs the running app or the background service.")
    s.api.override(args.output, None if args.release else args.power, args.seconds)
    print("released" if args.release else f"{args.output} at {args.power:g} % for {args.seconds:g} s")
    return 0


def cmd_profile(s: Session, args) -> int:
    cfg = s.config()
    if args.name is None:
        active = cfg.profile(cfg.active_profile) if cfg.active_profile else None
        for name in ["Default", *[p.name for p in cfg.profiles]]:
            mark = "*" if (active.name if active else "Default") == name else " "
            print(f"{mark} {name}")
        return 0
    name = "" if args.name.lower() == "default" else args.name
    if s.local and not args.demo:
        prof = cfg.profile(name) if name else None
        if name and not prof:
            raise SystemExit(f"No profile {name!r}")
        cfg.active_profile = prof.id if prof else ""
        print(s.save_config(cfg))
    else:
        s.api.set_profile(name)
        print(f"profile: {name or 'Default'}")
    return 0


def cmd_deltat(s: Session, args) -> int:
    cfg = s.config()
    vs = VirtualSensorConfig(id=new_id(), name=args.name, kind="difference", inputs=[args.coolant, args.ambient],
                             smoothing=args.smoothing)
    cfg.virtual_sensors.append(vs)
    print(f"virtual/{vs.id} “{vs.name}” = {args.coolant} − {args.ambient} — {s.save_config(cfg)}")
    return 0


def _slot_map(values: list[str]) -> dict[int, str]:
    out = {}
    for v in values or []:
        slot, _, sensor = v.partition("=")
        out[int(slot)] = sensor
    return out


def _print_plan(plan: aquasuite.ImportPlan) -> None:
    print(f"{plan.spec.name} → {plan.device_key}")
    for item in plan.items:
        print(f"  [{'x' if item.selected else ' '}] {item.label:<10} {item.description}")
    for note in plan.notes:
        print(f"  note: {note}")


def cmd_import(s: Session, args) -> int:
    if args.file:
        found = aquasuite.find_settings(Path(args.file).read_bytes(), Path(args.file).name)
        if not found:
            raise SystemExit("No device settings found in that file.")
        f = found[args.index] if args.index < len(found) else found[0]
        key = args.device or (f"{f.spec.kind}-{f.serial}" if f.serial else f.spec.kind)
        plan = aquasuite.plan_import(f.spec, f.data, key)
    else:
        if not args.device:
            raise SystemExit("Name the device: aquactl import DEVICE (see aquactl devices)")
        s.snapshot()
        data = s.api.device_settings(args.device)
        plan = aquasuite.plan_import(BY_KIND[data["kind"]], base64.b64decode(data["raw"]), args.device)
    if args.all:
        for item in plan.items:
            item.selected = True
    _print_plan(plan)
    if args.dry_run:
        return 0
    cfg = aquasuite.apply_import(s.config(), plan, _slot_map(args.map))
    print(s.save_config(cfg))
    return 0


def cmd_backup(s: Session, args) -> int:
    s.snapshot()
    b = s.api.backup(args.device)
    text = json.dumps(b, indent=2)
    if args.output in (None, "-"):
        print(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"saved {args.output}")
    return 0


def cmd_restore(s: Session, args) -> int:
    s.snapshot()
    backup = json.loads(Path(args.file).read_text(encoding="utf-8"))
    s.api.restore(args.device, backup)
    print("restored")
    return 0


def cmd_settings(s: Session, args) -> int:
    s.snapshot()
    data = s.api.device_settings(args.device)
    data.pop("raw", None)
    changes = {}
    if args.offset:
        offsets = [None] * len(data["temp_offsets"])
        for item in args.offset:
            idx, _, val = item.partition("=")
            offsets[int(idx.replace("temp", "")) - 1] = float(val)
        changes["temp_offsets"] = offsets
    if args.flow_pulses:
        changes["flow_pulses"] = args.flow_pulses
    if changes:
        s.api.apply_device_settings(args.device, changes)
        print("saved on the device")
        return 0
    if args.json:
        _print_json(data)
        return 0
    print(f"{args.device}: flow pulses {data['flow_pulses']}, offsets {data['temp_offsets']}")
    for f in data["fans"]:
        mode = {0: "manual", 1: "PID", 2: "curve"}.get(f["mode"], f"follow fan {f['mode'] - 2}")
        st = f["setup"] or {}
        print(f"  {f['label']:<8} {mode:<8} source {f['source']!s:<5} min {st.get('min', '—')} % "
              f"max {st.get('max', '—')} % fallback {st.get('fallback', '—')} %")
    return 0


def cmd_config(s: Session, args) -> int:
    if args.load:
        cfg = Config.from_dict(json.loads(Path(args.load).read_text(encoding="utf-8")))
        print(s.save_config(cfg))
        return 0
    _print_json(s.config().to_dict())
    return 0


def cmd_monitor(s: Session, args) -> int:
    try:
        while True:
            snap = s.snapshot() if s.local else s.api.snapshot()
            parts = []
            for sid in args.sensors:
                r = next((x for x in snap["readings"] if x["id"] == sid or x["label"] == sid), None)
                parts.append(f"{(r or {}).get('label', sid)} {_fmt((r or {}).get('value'), (r or {}).get('unit', ''))}")
            if not args.sensors:
                for o in snap["outputs"]:
                    parts.append(f"{o['name']} {_fmt(o['rpm'], 'rpm')}")
            print(time.strftime("%H:%M:%S"), " | ".join(parts), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_probe(_s, args) -> int:
    """List Aquacomputer HID interfaces and their reports (for bug reports about new devices)."""
    from .core.transport import HidrawTransport, enumerate_nodes
    nodes = enumerate_nodes(VENDOR_ID)
    if not nodes:
        print("No Aquacomputer hidraw nodes found.")
        return 1
    for n in nodes:
        print(f"{n.path}  {n.vendor:04x}:{n.product:04x}  {n.name!r}  interface {n.interface}")
        for r in n.reports:
            print(f"    report {r.report_id:#04x} {r.kind:<7} {r.length} bytes")
        if args.dump:
            try:
                t = HidrawTransport(n.path)
                for rep in t.read_input(2.0)[:1]:
                    print("    status:", rep.hex(" "))
                t.close()
            except AquaError as exc:
                print(f"    ({exc})")
    return 0


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aquactl", description=f"{__app_name__} command-line tool")
    p.add_argument("--version", action="version", version=f"aquactl {__version__}")
    p.add_argument("--demo", action="store_true", help="use simulated devices")
    p.add_argument("--standalone", action="store_true", help="don't use the background service")
    p.add_argument("--config", help="configuration file for standalone use")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name, fn, help_text, json_opt=False):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(fn=fn)
        if json_opt:
            sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    add("status", cmd_status, "devices, key readings, fans and alarms", True)
    sp = add("sensors", cmd_sensors, "every sensor reading", True)
    sp.add_argument("-a", "--all", action="store_true", help="include unavailable sensors")
    sp = add("get", cmd_get, "print one sensor's value")
    sp.add_argument("sensor", help="sensor id or name, e.g. virtual/deltat or 'Coolant'")
    sp.add_argument("--raw", action="store_true", help="just the number")
    add("devices", cmd_devices, "connected devices", True)
    sp = add("outputs", cmd_outputs, "fans and pumps", True)
    sp.add_argument("-v", "--verbose", action="store_true", help="explain where control runs")
    add("controllers", cmd_controllers, "configured controllers")
    sp = add("assign", cmd_assign, "choose the controller of an output")
    sp.add_argument("output", help="output id, e.g. quadro-12345-67890/fan1")
    sp.add_argument("controller", help="controller id or name, or 'none'")
    sp.add_argument("--placement", choices=("auto", "device", "software"))
    sp = add("set", cmd_set, "run an output at a fixed power for a while (test)")
    sp.add_argument("output")
    sp.add_argument("power", type=float, nargs="?", default=100.0)
    sp.add_argument("--seconds", type=float, default=30.0)
    sp.add_argument("--release", action="store_true", help="end a running override")
    sp = add("profile", cmd_profile, "show or switch the profile")
    sp.add_argument("name", nargs="?")
    sp = add("deltat", cmd_deltat, "create a Delta T virtual sensor")
    sp.add_argument("coolant", help="coolant temperature sensor id")
    sp.add_argument("ambient", help="air temperature sensor id")
    sp.add_argument("--name", default="Coolant ΔT")
    sp.add_argument("--smoothing", type=float, default=5.0)
    sp = add("import", cmd_import, "import aquasuite settings from a device or a file")
    sp.add_argument("device", nargs="?", help="device key (read its stored settings)")
    sp.add_argument("--file", help="aquasuite file or backup to search instead")
    sp.add_argument("--index", type=int, default=0, help="which settings block of the file")
    sp.add_argument("--map", action="append", metavar="SLOT=SENSOR",
                    help="feed a software sensor slot from a sensor, e.g. 1=virtual/deltat")
    sp.add_argument("--all", action="store_true", help="also import outputs in manual mode")
    sp.add_argument("--dry-run", action="store_true", help="only show what would be imported")
    sp = add("backup", cmd_backup, "save a device's settings to a file")
    sp.add_argument("device")
    sp.add_argument("-o", "--output")
    sp = add("restore", cmd_restore, "write a settings backup to a device")
    sp.add_argument("device")
    sp.add_argument("file")
    sp = add("settings", cmd_settings, "show or change settings stored on a device", True)
    sp.add_argument("device")
    sp.add_argument("--offset", action="append", metavar="tempN=K", help="temperature offset, e.g. temp2=-0.5")
    sp.add_argument("--flow-pulses", type=int, help="flow sensor pulses per litre")
    sp = add("config", cmd_config, "print (or load) the whole configuration")
    sp.add_argument("--load", metavar="FILE", help="replace the configuration with a JSON file")
    sp = add("monitor", cmd_monitor, "print readings continuously")
    sp.add_argument("sensors", nargs="*")
    sp.add_argument("-n", "--interval", type=float, default=2.0)
    sp = add("probe", cmd_probe, "list Aquacomputer HID interfaces (for bug reports)")
    sp.add_argument("--dump", action="store_true", help="also print one status report")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        args.fn = cmd_status
        args.json = False
    if args.fn is cmd_probe:
        return cmd_probe(None, args)
    s = None
    try:
        s = Session(args)
        return args.fn(s, args) or 0
    except BrokenPipeError:
        # output piped into head/grep that exited early
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except (AquaError, PermissionError, OSError, ValueError) as exc:
        print(f"aquactl: {exc}", file=sys.stderr)
        return 2
    finally:
        if s is not None:
            s.close()


if __name__ == "__main__":
    sys.exit(main())
