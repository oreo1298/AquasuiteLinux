"""Bring settings over from aquasuite (Windows): import, plus our own settings backups.

aquasuite stores a device's fan configuration *on the device*: curves, curve inputs, minimum,
maximum and fallback power, start boost, temperature offsets and flow calibration all live
in the device's settings report. So the most reliable import is to read that report back
from the device (``plan_import`` on the bytes from ``Engine.device_settings``).

aquasuite's own files (``C:\\ProgramData\\aquasuite-data``, profile exports) use a private,
undocumented format. ``find_settings`` searches any file — binary, XML, JSON, zip, gzip,
text with hex or base64 blocks — for embedded device settings reports and accepts only
blocks whose CRC-16 checks out, so a match is exact rather than a guess.

What the device can't know is what fed its *software sensors*: an aquasuite Delta T or CPU
temperature arrives over USB. Curves that use a software sensor are imported with that
slot noted, so you can point it at the matching Linux sensor (for example a Delta T
virtual sensor) and AquasuiteLinux keeps feeding the same slot.

aquasuite's device backups (``<DeviceBackup>`` XML: *Backup* on the device page) also hold the
device's name table (feature report 0x08) with the names given in aquasuite — "Water Temp",
"Ambient", a software sensor called "Delta T". Those names label the imported sensors and let the
import set up the matching Linux sensors by itself (``suggest_sources``).
"""

from __future__ import annotations

import base64
import binascii
import gzip
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field

from . import control
from .config import Config, ControllerConfig, FeedConfig, OutputConfig, VirtualSensorConfig, new_id
from .crc import is_sealed
from .devices import ALL_SPECS, BY_KIND, DeviceSpec
from .errors import AquaError

BACKUP_FORMAT = "aquasuitelinux-settings"
MAX_SCAN_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------- backups
def make_backup(spec: DeviceSpec, serial: str, firmware: int, data: bytes) -> dict:
    return {"format": BACKUP_FORMAT, "version": 1, "kind": spec.kind, "model": spec.name, "serial": serial,
            "firmware": firmware, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "settings": base64.b64encode(bytes(data)).decode()}


def backup_bytes(backup: dict, spec: DeviceSpec) -> bytes:
    if backup.get("format") != BACKUP_FORMAT:
        raise AquaError("this is not an AquasuiteLinux settings backup")
    if backup.get("kind") != spec.kind:
        raise AquaError(f"the backup is for a {backup.get('model') or backup.get('kind')}, not a {spec.name}")
    data = base64.b64decode(backup.get("settings", ""))
    if len(data) != spec.ctrl_length or data[0] != spec.ctrl_id:
        raise AquaError("the backup does not contain a complete settings report")
    return data


# ---------------------------------------------------------------------- finding settings in files
# ---------------------------------------------------------------------- names (feature report 0x08)
NAME_REPORT_ID = 0x08
NAME_REPORT_LENGTH = 1013           # QUADRO and OCTO (from their HID report descriptors)
NAME_SIZE = 24                      # NUL-padded Latin-1, after a 3-byte header; CRC-16 at the end
# First slot and count of each group. QUADRO: from an aquasuite backup of a real QUADRO
# (fans 0-3, LED controllers 8-15, flow 16, sensors 17-20, software sensors 24-39). OCTO: from
# aqdctl's captures (fans 0x003, sensors 0x1E3, flow 0x243, software sensors 0x273).
NAME_LAYOUTS: dict[str, dict[str, tuple[int, int]]] = {
    "quadro": {"fan": (0, 4), "flow": (16, 1), "temp": (17, 4), "virt": (24, 16)},
    "octo": {"fan": (0, 8), "temp": (20, 4), "flow": (24, 1), "virt": (26, 16)},
}
_DEFAULT_NAME = re.compile(r"^(fan|sensor|soft\.? ?sensor|software sensor|flow|temperature|temp)\s*\d*$", re.I)


def parse_names(kind: str, data: bytes) -> dict[str, str]:
    """Sensor key → name from a device's name report (``{"temp1": "Water Temp", "virt1": "Delta T"}``)."""
    layout = NAME_LAYOUTS.get(kind)
    if not layout or not data or data[0] != NAME_REPORT_ID or not is_sealed(data):
        return {}
    out: dict[str, str] = {}
    for group, (first, count) in layout.items():
        for i in range(count):
            off = 3 + (first + i) * NAME_SIZE
            name = data[off:off + NAME_SIZE].split(b"\0")[0].decode("latin-1", "replace").strip()
            if name:
                out[group if group == "flow" else f"{group}{i + 1}"] = name
    return out


def custom_name(name: str) -> bool:
    """A name someone chose, not the device's default ("Fan 1", "Sensor 3", "Soft. Sensor 6")."""
    return bool(name) and not _DEFAULT_NAME.match(name.strip())


@dataclass
class FoundSettings:
    spec: DeviceSpec
    data: bytes
    origin: str
    serial: str = ""
    names: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        rep = control.ControlReport(self.spec, self.data)
        modes = []
        for fan in rep.to_dict()["fans"]:
            modes.append(describe_mode(self.spec, fan))
        return f"{self.spec.name} — " + "; ".join(modes)


_CANDIDATE_SPECS = [s for s in ALL_SPECS if s.family == "standard" and s.ctrl_id is not None and s.ctrl_crc]


def _plausible(spec: DeviceSpec, blob: bytes, pos: int) -> bool:
    """Cheap structural checks before computing a CRC at ``pos``."""
    for f in spec.fans:
        if f.ctrl is None:
            continue
        o = pos + f.ctrl
        if blob[o] > 12:
            return False
        if int.from_bytes(blob[o + 1:o + 3], "big") > 10000:
            return False
        if f.setup_min is not None:
            for k in (0, 2, 4):
                if int.from_bytes(blob[pos + f.setup_min + k:pos + f.setup_min + k + 2], "big") > 10000:
                    return False
    return True


def scan_binary(blob: bytes, origin: str = "") -> list[FoundSettings]:
    found: list[FoundSettings] = []
    for spec in _CANDIDATE_SPECS:
        n = spec.ctrl_length
        start = 0
        while True:
            pos = blob.find(bytes([spec.ctrl_id]), start)
            if pos < 0 or pos + n > len(blob):
                break
            start = pos + 1
            if not _plausible(spec, blob, pos):
                continue
            chunk = blob[pos:pos + n]
            if is_sealed(chunk):
                where = f"{origin} at byte {pos}" if origin else f"byte {pos}"
                found.append(FoundSettings(spec, bytes(chunk), where))
                start = pos + n
    return found


_HEX_RE = re.compile(rb"(?:[0-9A-Fa-f]{2}[\s,:;-]?){200,}")
_B64_RE = re.compile(rb"[A-Za-z0-9+/\r\n]{300,}={0,2}")


def _text_blocks(blob: bytes) -> list[tuple[str, bytes]]:
    out = []
    for m in _HEX_RE.finditer(blob):
        digits = re.sub(rb"[^0-9A-Fa-f]", b"", m.group(0))
        if len(digits) % 2:
            digits = digits[:-1]
        try:
            out.append(("hex text", binascii.unhexlify(digits)))
        except binascii.Error:
            pass
    for m in _B64_RE.finditer(blob):
        text = re.sub(rb"\s", b"", m.group(0))
        text = text[: len(text) - len(text) % 4]
        try:
            out.append(("base64 text", base64.b64decode(text, validate=True)))
        except (binascii.Error, ValueError):
            pass
    return out


_XML_ITEM_RE = re.compile(rb"<DeviceDataItem>\s*<Name>([^<]*)</Name>\s*<Data>([^<]*)</Data>", re.S)


def _xml_field(blob: bytes, tag: str) -> str:
    m = re.search(rb"<" + tag.encode() + rb">([^<]*)</" + tag.encode() + rb">", blob)
    return m.group(1).decode("utf-8", "replace").strip() if m else ""


def device_backup(blob: bytes, label: str = "file") -> list[FoundSettings]:
    """An aquasuite device backup (``<DeviceBackup>`` XML): the settings report plus the names."""
    if b"<DeviceBackup" not in blob[:4096]:
        return []
    items: dict[str, bytes] = {}
    for m in _XML_ITEM_RE.finditer(blob):
        try:
            items[m.group(1).decode("utf-8", "replace").strip()] = base64.b64decode(re.sub(rb"\s", b"", m.group(2)))
        except (binascii.Error, ValueError):
            continue
    serial = _xml_field(blob, "DeviceSerial")
    kind = _xml_field(blob, "DeviceType").lower()
    found: list[FoundSettings] = []
    for f in scan_binary(items.get("settings", b"")):
        if kind and kind in BY_KIND and f.spec.kind != kind:
            continue
        names = parse_names(f.spec.kind, items.get("flash", b""))
        when = _xml_field(blob, "Time")[:10]
        origin = f"{label} (aquasuite backup of {f.spec.name} {serial}{', ' + when if when else ''})"
        found.append(FoundSettings(f.spec, f.data, origin, serial, names))
    return found


def find_settings(blob: bytes, name: str = "", depth: int = 0) -> list[FoundSettings]:
    """Every device settings report inside ``blob`` (any file format), CRC-verified."""
    if len(blob) > MAX_SCAN_BYTES:
        blob = blob[:MAX_SCAN_BYTES]
    label = name or "file"
    backup = device_backup(blob, label)
    if backup:
        return backup
    # our own backup format
    stripped = blob.lstrip()
    if stripped[:1] == b"{":
        try:
            data = json.loads(blob.decode("utf-8"))
            if isinstance(data, dict) and data.get("format") == BACKUP_FORMAT and data.get("kind") in BY_KIND:
                spec = BY_KIND[data["kind"]]
                raw = backup_bytes(data, spec)
                return [FoundSettings(spec, raw, f"{label} (AquasuiteLinux backup)", data.get("serial", ""))]
        except (ValueError, UnicodeDecodeError, AquaError):
            pass
    found: list[FoundSettings] = []
    if depth < 2:
        if blob[:2] == b"\x1f\x8b":
            try:
                found += find_settings(gzip.decompress(blob), f"{label} (gzip)", depth + 1)
            except (OSError, EOFError):
                pass
        if blob[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                    for info in zf.infolist()[:500]:
                        if info.file_size <= MAX_SCAN_BYTES:
                            found += find_settings(zf.read(info), f"{label}:{info.filename}", depth + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError):
                pass
        for kind, data in _text_blocks(blob):
            found += [FoundSettings(f.spec, f.data, f"{label} ({kind})") for f in scan_binary(data)]
    found += scan_binary(blob, label)       # after the containers, so their more precise origin wins
    unique: dict[bytes, FoundSettings] = {}
    for f in found:
        unique.setdefault(f.data, f)
    return list(unique.values())


# ---------------------------------------------------------------------- turning settings into a config
MODE_NAMES = {0: "Manual", 1: "Target temperature (PID)", 2: "Curve"}


def describe_mode(spec: DeviceSpec, fan: dict, names: dict[str, str] | None = None) -> str:
    mode = fan["mode"]
    if mode == 0:
        return f"{fan['label']}: manual {fan['pwm']:.0f} %"
    if mode in (1, 2):
        src = fan["source"]
        key = control.source_key(spec, src) if src is not None else None
        name = _sensor_label(spec, key) if key else "no sensor"
        given = (names or {}).get(key or "", "")
        if custom_name(given):
            name += f" “{given}”"
        return f"{fan['label']}: {'target temperature' if mode == 1 else 'curve'} on {name}"
    other = mode - control.MODE_FOLLOW
    label = spec.fans[other].label if 0 <= other < len(spec.fans) else f"fan {other + 1}"
    return f"{fan['label']}: follows {label}"


def _sensor_label(spec: DeviceSpec, key: str) -> str:
    for t in spec.temps:
        if t.key == key:
            return t.label
    if key.startswith("virt"):
        return f"software sensor {key[4:]}"
    return key


@dataclass
class ImportItem:
    output_id: str
    label: str
    description: str
    controller: ControllerConfig | None
    output: OutputConfig
    soft_slot: int | None = None       # software sensor slot the curve reads
    selected: bool = True


NEW_DELTA_T = "+deltat"         # slot_map value: create a Delta T from the device's coolant and air sensors
_DELTA_RE = re.compile(r"delta|Δ|\bd ?t\b|diff", re.I)
_COOLANT_RE = re.compile(r"water|coolant|liquid|loop|fluid|wasser|kühl|kuehl|in ?let|out ?let", re.I)
_AIR_RE = re.compile(r"ambient|air|room|luft|umgebung|raum", re.I)
# preferred PC sensors for software sensors named after the CPU or GPU, best first
_CPU_IDS = ("system/k10temp/tctl", "system/k10temp/tdie", "system/zenpower/tdie", "system/zenpower/tctl",
            "system/coretemp/package_id_0")
_GPU_IDS = {"hot": ("system/amdgpu/junction", "system/nvidia0/gpu", "system/amdgpu/edge"),
            "core": ("system/amdgpu/edge", "system/nvidia0/gpu", "system/amdgpu/junction")}


@dataclass
class ImportPlan:
    device_key: str
    spec: DeviceSpec
    items: list[ImportItem] = field(default_factory=list)
    temp_offsets: list[float] = field(default_factory=list)
    flow_pulses: int | None = None
    notes: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)          # sensor key → name given in aquasuite
    delta_t: tuple[str, str] | None = None                       # (coolant, air) sensor keys, from the names

    @property
    def soft_slots(self) -> list[int]:
        return sorted({i.soft_slot for i in self.items if i.soft_slot is not None and i.selected})

    def slot_name(self, slot: int) -> str:
        name = self.names.get(f"virt{slot}", "")
        return name if custom_name(name) else ""

    def sensor_names(self) -> dict[str, str]:
        """Names given in aquasuite to the device's own sensors (only the ones someone chose)."""
        keys = {t.key for t in self.spec.temps} | {"flow"}
        return {k: v for k, v in self.names.items() if k in keys and custom_name(v)}

    def describe_source(self, source: str) -> str:
        if source == NEW_DELTA_T and self.delta_t:
            c, a = self.delta_t
            return f"new Delta T: {self.names.get(c, c)} − {self.names.get(a, a)}"
        return source or "nothing (the curve runs at fallback power)"


def plan_import(spec: DeviceSpec, data: bytes, device_key: str, names: dict[str, str] | None = None) -> ImportPlan:
    """What an aquasuite-configured settings report means in AquasuiteLinux terms."""
    rep = control.ControlReport(spec, data)
    plan = ImportPlan(device_key, spec, temp_offsets=rep.temp_offsets(), flow_pulses=rep.flow_pulses(),
                      names=dict(names or {}))
    temp_names = {t.key: plan.names.get(t.key, "") for t in spec.temps}
    coolant = [k for k, n in temp_names.items() if _COOLANT_RE.search(n)]
    air = [k for k, n in temp_names.items() if _AIR_RE.search(n)]
    if coolant and air and coolant[0] != air[0]:
        plan.delta_t = (coolant[0], air[0])
    if not rep.valid:
        plan.notes.append("The settings checksum does not match; values may be damaged.")
    curves: dict[tuple, ControllerConfig] = {}
    for i, f in enumerate(spec.fans):
        if f.ctrl is None:
            continue
        fc = rep.fan(i)
        st = rep.setup(i)
        oid = f"{device_key}/{f.key}"
        given = plan.names.get(f.key, "")
        oc = OutputConfig(name=given if custom_name(given) else f.label)
        if st is not None:
            oc.min_power, oc.max_power = st.min_power / 100, st.max_power / 100
            oc.fallback, oc.hold_min, oc.start_boost = st.fallback / 100, st.hold_min, st.start_boost
        oc.placement = "auto"
        fan_dict = {"label": f.label, "mode": fc.mode, "pwm": fc.pwm / 100,
                    "source": None if fc.source == control.SOURCE_NONE else fc.source}
        desc = describe_mode(spec, fan_dict, plan.names)
        ctrl: ControllerConfig | None = None
        slot = None
        src_key = control.source_key(spec, fc.source)
        input_id = f"{device_key}/{src_key}" if src_key else ""
        if src_key and src_key.startswith("virt") and fc.mode in (control.MODE_CURVE, control.MODE_PID):
            slot = int(src_key[4:])       # a follow or manual output keeps a stale input field: not read
        if fc.mode == control.MODE_CURVE:
            pts = _dedupe_points([[t / 100, p / 100] for t, p in zip(fc.curve_temps, fc.curve_powers)])
            sig = ("curve", input_id, tuple(map(tuple, pts)))
            ctrl = curves.get(sig)
            if ctrl is None:
                given = plan.names.get(src_key or "", "")
                label = given if custom_name(given) else _sensor_label(spec, src_key) if src_key else "Curve"
                name = f"{label[:1].upper()}{label[1:]} curve"
                ctrl = ControllerConfig(id=new_id(), name=name, kind="curve", input=input_id, points=pts)
                curves[sig] = ctrl
        elif fc.mode == control.MODE_PID:
            sig = ("target", input_id, fc.pid[0])
            ctrl = curves.get(sig)
            if ctrl is None:
                ctrl = ControllerConfig(id=new_id(), name=f"Target {fc.pid[0] / 100:g} °C", kind="target",
                                        input=input_id, target=fc.pid[0] / 100)
                curves[sig] = ctrl
                plan.notes.append(f"{f.label}: target temperature {fc.pid[0] / 100:g} °C imported; the P/I/D "
                                  "values use AquasuiteLinux defaults (aquasuite's scaling is not known).")
        elif fc.mode >= control.MODE_FOLLOW:
            other = fc.mode - control.MODE_FOLLOW
            if 0 <= other < len(spec.fans):
                ctrl = ControllerConfig(id=new_id(), name=f"Follow {spec.fans[other].label}", kind="follow",
                                        follow=f"{device_key}/{spec.fans[other].key}")
        else:
            ctrl = ControllerConfig(id=new_id(), name=f"{f.label} {fc.pwm / 100:g} %", kind="fixed",
                                    power=fc.pwm / 100)
        item = ImportItem(oid, f.label, desc, ctrl, oc, slot, selected=fc.mode != control.MODE_MANUAL)
        if fc.mode == control.MODE_MANUAL:
            item.description += " (aquasuite may have been setting this from the PC)"
        plan.items.append(item)
    if plan.soft_slots:
        slots = ", ".join(str(s) + (f" (“{plan.slot_name(s)}”)" if plan.slot_name(s) else "")
                          for s in plan.soft_slots)
        plan.notes.append(f"Curves read software sensor {slots}. aquasuite filled it from the PC; choose which "
                          "Linux sensor should feed it (for example a Delta T).")
    for item in plan.items:
        c = item.controller
        if (item.selected and c is not None and c.kind == "curve" and item.soft_slot is not None and c.points
                and _DELTA_RE.search(plan.slot_name(item.soft_slot)) and min(p[0] for p in c.points) >= 15):
            x0, y0 = min(c.points)
            plan.notes.append(f"{item.label}: its curve starts at {x0:g}, which looks like a coolant temperature "
                              f"rather than a Delta T (a few kelvin), so on the Delta T it would stay at {y0:g} %. "
                              "Check the curve after importing.")
    return plan


def suggest_sources(plan: ImportPlan, cfg: Config, readings: dict[str, object] | None = None) -> dict[int, str]:
    """Linux sensors for the software sensors the imported curves read, from their aquasuite names.

    A "Delta T" becomes an existing difference sensor over the same two sensors, or ``NEW_DELTA_T``
    (``apply_import`` creates it); a "CPU …" or "GPU …" sensor becomes the PC's matching sensor.
    """
    readings = readings or {}
    out: dict[int, str] = {}
    for slot in plan.soft_slots:
        name = plan.slot_name(slot)
        if not name:
            continue
        if _DELTA_RE.search(name) and plan.delta_t:
            inputs = [f"{plan.device_key}/{k}" for k in plan.delta_t]
            same = next((v for v in cfg.virtual_sensors if v.kind == "difference" and v.inputs == inputs), None)
            out[slot] = f"virtual/{same.id}" if same else NEW_DELTA_T
        elif re.search(r"cpu", name, re.I):
            sid = next((i for i in _CPU_IDS if i in readings), "")
            if sid:
                out[slot] = sid
        elif re.search(r"gpu", name, re.I):
            prefs = _GPU_IDS["hot" if re.search(r"hot|junction|package", name, re.I) else "core"]
            sid = next((i for i in prefs if i in readings), "")
            if sid:
                out[slot] = sid
    return out


def _dedupe_points(points: list[list[float]]) -> list[list[float]]:
    """16 device points often repeat at the ends; keep the shape with fewer points."""
    pts = sorted(points)
    out: list[list[float]] = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 1e-9:
            out[-1] = p
            continue
        out.append(p)
    # drop points that lie on the straight line between their neighbours
    i = 1
    while i < len(out) - 1:
        (x0, y0), (x1, y1), (x2, y2) = out[i - 1], out[i], out[i + 1]
        if x2 != x0 and abs(y0 + (y2 - y0) * (x1 - x0) / (x2 - x0) - y1) < 0.05:
            out.pop(i)
        else:
            i += 1
    return [[round(x, 2), round(y, 2)] for x, y in out]


def apply_import(cfg: Config, plan: ImportPlan, slot_map: dict[int, str] | None = None) -> Config:
    """Add the selected items to a copy of ``cfg``. ``slot_map`` maps software sensor slots to sensors."""
    cfg = cfg.copy()
    slot_map = dict(slot_map or {})
    for slot, source in list(slot_map.items()):
        if source == NEW_DELTA_T and slot_map[slot] == NEW_DELTA_T:
            if not plan.delta_t:
                raise AquaError("no coolant and air sensor known for a new Delta T; pick a sensor instead")
            vs = VirtualSensorConfig(id=new_id(), name=plan.slot_name(slot) or "Delta T", kind="difference",
                                     inputs=[f"{plan.device_key}/{k}" for k in plan.delta_t], smoothing=5.0)
            cfg.virtual_sensors.append(vs)
            for other in [s for s, src in slot_map.items() if src == NEW_DELTA_T]:
                slot_map[other] = f"virtual/{vs.id}"          # one Delta T for every slot that asks for it
    if plan.sensor_names():
        names = cfg.device(plan.device_key).sensor_names
        for key, name in plan.sensor_names().items():
            names.setdefault(key, name)                          # never rename what the user named here
    added: dict[str, str] = {}
    for item in plan.items:
        if not item.selected:
            continue
        ctrl = item.controller
        if ctrl is not None:
            if ctrl.id not in added:
                c = ControllerConfig(**{**ctrl.__dict__, "points": [list(p) for p in ctrl.points],
                                        "sources": list(ctrl.sources)})
                if item.soft_slot is not None and slot_map.get(item.soft_slot):
                    c.input = slot_map[item.soft_slot]
                cfg.controllers.append(c)
                added[ctrl.id] = c.id
        oc = OutputConfig(**item.output.__dict__)
        oc.controller = added.get(ctrl.id, "") if ctrl else ""
        existing = cfg.outputs.get(item.output_id)
        if existing and existing.name and existing.name != item.label:
            oc.name = existing.name
        cfg.outputs[item.output_id] = oc
    for slot, source in slot_map.items():
        if source and slot in plan.soft_slots:
            cfg.feeds = [f for f in cfg.feeds if not (f.device == plan.device_key and f.slot == slot)]
            cfg.feeds.append(FeedConfig(plan.device_key, slot, source))
    return cfg
