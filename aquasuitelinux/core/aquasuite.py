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
from .config import Config, ControllerConfig, FeedConfig, OutputConfig, new_id
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
@dataclass
class FoundSettings:
    spec: DeviceSpec
    data: bytes
    origin: str
    serial: str = ""

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


def find_settings(blob: bytes, name: str = "", depth: int = 0) -> list[FoundSettings]:
    """Every device settings report inside ``blob`` (any file format), CRC-verified."""
    if len(blob) > MAX_SCAN_BYTES:
        blob = blob[:MAX_SCAN_BYTES]
    label = name or "file"
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


def describe_mode(spec: DeviceSpec, fan: dict) -> str:
    mode = fan["mode"]
    if mode == 0:
        return f"{fan['label']}: manual {fan['pwm']:.0f} %"
    if mode in (1, 2):
        src = fan["source"]
        key = control.source_key(spec, src) if src is not None else None
        name = _sensor_label(spec, key) if key else "no sensor"
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


@dataclass
class ImportPlan:
    device_key: str
    spec: DeviceSpec
    items: list[ImportItem] = field(default_factory=list)
    temp_offsets: list[float] = field(default_factory=list)
    flow_pulses: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def soft_slots(self) -> list[int]:
        return sorted({i.soft_slot for i in self.items if i.soft_slot is not None and i.selected})


def plan_import(spec: DeviceSpec, data: bytes, device_key: str) -> ImportPlan:
    """What an aquasuite-configured settings report means in AquasuiteLinux terms."""
    rep = control.ControlReport(spec, data)
    plan = ImportPlan(device_key, spec, temp_offsets=rep.temp_offsets(), flow_pulses=rep.flow_pulses())
    if not rep.valid:
        plan.notes.append("The settings checksum does not match; values may be damaged.")
    curves: dict[tuple, ControllerConfig] = {}
    for i, f in enumerate(spec.fans):
        if f.ctrl is None:
            continue
        fc = rep.fan(i)
        st = rep.setup(i)
        oid = f"{device_key}/{f.key}"
        oc = OutputConfig(name=f.label)
        if st is not None:
            oc.min_power, oc.max_power = st.min_power / 100, st.max_power / 100
            oc.fallback, oc.hold_min, oc.start_boost = st.fallback / 100, st.hold_min, st.start_boost
        oc.placement = "auto"
        fan_dict = {"label": f.label, "mode": fc.mode, "pwm": fc.pwm / 100,
                    "source": None if fc.source == control.SOURCE_NONE else fc.source}
        desc = describe_mode(spec, fan_dict)
        ctrl: ControllerConfig | None = None
        slot = None
        src_key = control.source_key(spec, fc.source)
        input_id = f"{device_key}/{src_key}" if src_key else ""
        if src_key and src_key.startswith("virt"):
            slot = int(src_key[4:])
        if fc.mode == control.MODE_CURVE:
            pts = _dedupe_points([[t / 100, p / 100] for t, p in zip(fc.curve_temps, fc.curve_powers)])
            sig = ("curve", input_id, tuple(map(tuple, pts)))
            ctrl = curves.get(sig)
            if ctrl is None:
                name = f"{_sensor_label(spec, src_key) if src_key else 'Curve'} curve".capitalize()
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
        slots = ", ".join(str(s) for s in plan.soft_slots)
        plan.notes.append(f"Curves read software sensor {slots}. aquasuite filled it from the PC; choose which "
                          "Linux sensor should feed it (for example a Delta T).")
    return plan


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
    slot_map = slot_map or {}
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
