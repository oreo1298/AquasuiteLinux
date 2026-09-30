"""Virtual sensors: values computed from other sensors (Delta T, averages, formulas, heat load).

A virtual sensor's id is ``virtual/<id>``; it can use device sensors, system sensors and
other virtual sensors as inputs. Formulas are evaluated by a small whitelist-based
interpreter (numbers, + - * / ** %, comparisons, ``a if c else b`` and a few functions),
never by ``eval``.
"""

from __future__ import annotations

import ast
import math
import operator

from .config import VirtualSensorConfig
from .errors import ConfigError
from .model import Reading

WATER_CP = 4186.0          # J/(kg·K)
WATER_DENSITY = 1.0        # kg/L (close enough between 20 and 50 °C)

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}
_CMPOPS = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
           ast.Eq: operator.eq, ast.NotEq: operator.ne}
_FUNCS = {
    "min": min, "max": max, "abs": abs, "round": round, "sqrt": math.sqrt, "log": math.log, "exp": math.exp,
    "clamp": lambda v, lo, hi: max(lo, min(hi, v)),
}
VARIABLES = "abcdefgh"


def compile_expression(text: str) -> ast.Expression:
    try:
        tree = ast.parse(text.strip() or "0", mode="eval")
    except SyntaxError as exc:
        raise ConfigError(f"formula: {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp, ast.BoolOp,
                             ast.Load, ast.And, ast.Or, ast.USub, ast.UAdd, ast.Not, *_BINOPS, *_CMPOPS)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            continue
        if isinstance(node, ast.Name) and (node.id in VARIABLES or node.id in ("pi",)):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS \
                and not node.keywords:
            continue
        if isinstance(node, ast.Name) and node.id in _FUNCS:
            continue
        raise ConfigError(f"formula: '{ast.unparse(node) if hasattr(ast, 'unparse') else node}' is not allowed")
    return tree


def _eval(node, env: dict[str, float]):
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "pi":
            return math.pi
        return env[node.id]
    if isinstance(node, ast.BinOp):
        return _BINOPS[type(node.op)](_eval(node.left, env), _eval(node.right, env))
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        return -v if isinstance(node.op, ast.USub) else (not v) if isinstance(node.op, ast.Not) else +v
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, env)
            if not _CMPOPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, env) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.IfExp):
        return _eval(node.body, env) if _eval(node.test, env) else _eval(node.orelse, env)
    if isinstance(node, ast.Call):
        return _FUNCS[node.func.id](*[_eval(a, env) for a in node.args])
    raise ConfigError("formula: unsupported element")


def evaluate_expression(text_or_tree, values: list[float]) -> float:
    tree = compile_expression(text_or_tree) if isinstance(text_or_tree, str) else text_or_tree
    env = {VARIABLES[i]: v for i, v in enumerate(values[:len(VARIABLES)])}
    return float(_eval(tree, env))


def unit_kind(unit: str) -> str:
    return {"°C": "temperature", "K": "delta", "W": "power", "%": "percent", "L/h": "flow", "rpm": "rpm",
            "V": "voltage", "A": "current", "mbar": "pressure"}.get(unit, "number")


def derived_unit(cfg: VirtualSensorConfig, inputs: list[Reading | None]) -> str:
    if cfg.unit:
        return cfg.unit
    units = [r.unit for r in inputs if r is not None]
    first = units[0] if units else ""
    if cfg.kind == "difference":
        return "K" if first in ("°C", "K") else first
    if cfg.kind == "heat_load":
        return "W"
    if cfg.kind in ("average", "min", "max", "sum", "scale"):
        return first
    return ""


def dependencies(cfg: VirtualSensorConfig) -> list[str]:
    return [i[len("virtual/"):] for i in cfg.inputs if i.startswith("virtual/")]


def order(sensors: list[VirtualSensorConfig]) -> list[VirtualSensorConfig]:
    """Topological order; raises ConfigError on a cycle."""
    by_id = {s.id: s for s in sensors}
    out: list[VirtualSensorConfig] = []
    mark: dict[str, int] = {}

    def visit(s: VirtualSensorConfig, trail: list[str]) -> None:
        m = mark.get(s.id, 0)
        if m == 2:
            return
        if m == 1:
            names = " → ".join(by_id[t].name for t in [*trail, s.id] if t in by_id)
            raise ConfigError(f"virtual sensors depend on each other in a loop: {names}")
        mark[s.id] = 1
        for dep in dependencies(s):
            if dep in by_id:
                visit(by_id[dep], [*trail, s.id])
        mark[s.id] = 2
        out.append(s)

    for s in sensors:
        visit(s, [])
    return out


def validate(sensors: list[VirtualSensorConfig]) -> None:
    order(sensors)
    for s in sensors:
        if s.kind == "expression":
            compile_expression(s.expression)


class VirtualSensors:
    """Evaluates the configured virtual sensors every engine tick (keeps smoothing state)."""

    def __init__(self) -> None:
        self._ema: dict[str, float] = {}
        self._compiled: dict[str, tuple[str, ast.Expression]] = {}

    def _expr(self, cfg: VirtualSensorConfig) -> ast.Expression:
        cached = self._compiled.get(cfg.id)
        if cached and cached[0] == cfg.expression:
            return cached[1]
        tree = compile_expression(cfg.expression)
        self._compiled[cfg.id] = (cfg.expression, tree)
        return tree

    def compute(self, cfg: VirtualSensorConfig, inputs: list[Reading | None]) -> float | None:
        vals = [r.value if r is not None else None for r in inputs]
        k = cfg.kind
        if k == "constant":
            return float(cfg.value)
        present = [v for v in vals if v is not None]
        if k == "difference":
            if len(vals) < 2 or vals[0] is None or vals[1] is None:
                return None
            return vals[0] - vals[1]
        if k in ("average", "min", "max", "sum"):
            if not present:
                return None
            return {"average": sum(present) / len(present), "min": min(present), "max": max(present),
                    "sum": sum(present)}[k]
        if k == "scale":
            if not vals or vals[0] is None:
                return None
            return vals[0] * cfg.factor + cfg.offset
        if k == "heat_load":
            if len(vals) < 3 or None in vals[:3]:
                return None
            flow, hot, cold = vals[:3]
            return flow / 3600.0 * WATER_DENSITY * WATER_CP * cfg.coolant_factor * (hot - cold)
        if k == "expression":
            if any(v is None for v in vals):
                return None
            try:
                return evaluate_expression(self._expr(cfg), vals)
            except (ArithmeticError, ValueError, TypeError, ConfigError):
                return None
        return None

    def evaluate(self, sensors: list[VirtualSensorConfig], readings: dict[str, Reading], dt: float) -> list[Reading]:
        out: list[Reading] = []
        try:
            ordered = order(sensors)
        except ConfigError:
            ordered = []
        for cfg in ordered:
            inputs = [readings.get(i) for i in cfg.inputs]
            value = self.compute(cfg, inputs)
            if value is not None and cfg.smoothing > 0 and dt > 0:
                prev = self._ema.get(cfg.id)
                if prev is not None:
                    alpha = 1.0 - math.exp(-dt / cfg.smoothing)
                    value = prev + (value - prev) * alpha
                self._ema[cfg.id] = value
            elif value is None:
                self._ema.pop(cfg.id, None)
            unit = derived_unit(cfg, inputs)
            r = Reading(f"virtual/{cfg.id}", cfg.name, unit_kind(unit),
                        None if value is None else round(value, 3), "Virtual sensors", "virtual", unit)
            readings[r.id] = r
            out.append(r)
        return out
