"""A small evaluator for GitHub Actions expressions (``${{ ... }}``).

It covers the syntax real workflows use in ``if:`` conditions and ``run:``
interpolation: literals, context lookups (``github.event.action``,
``matrix['java']``), comparisons, ``!``/``&&``/``||``, and the built-in
functions. Lookups that cannot be answered locally (``secrets.*``) are
recorded so the caller can tell the user why a step may behave differently.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

INTERP_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


class ExpressionError(Exception):
    pass


@dataclass
class Context:
    """Values an expression can read, plus a log of what it read."""

    values: dict[str, Any] = field(default_factory=dict)
    status: str = "success"  # success | failure | cancelled
    hash_files: Callable[[list[str]], str] | None = None
    reads: set[str] = field(default_factory=set)

    def lookup(self, name: str) -> Any:
        self.reads.add(name)
        return self.values.get(name)


# ---------------------------------------------------------------- tokenizer

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>-?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))
  | (?P<str>'(?:[^']|'')*')
  | (?P<op>&&|\|\||==|!=|<=|>=|[<>!()\[\].,*])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_-]*)
    """,
    re.VERBOSE,
)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ExpressionError(f"unexpected character {text[pos]!r} in {text!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        tokens.append((kind, m.group()))
    return tokens


# ---------------------------------------------------------------- coercion


def to_bool(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return not (v == 0 or (isinstance(v, float) and math.isnan(v)))
    if isinstance(v, str):
        return v != ""
    return True  # objects and arrays


def to_number(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return 0.0
        try:
            return float(int(s, 16)) if s.lower().startswith("0x") else float(s)
        except ValueError:
            return math.nan
    return math.nan


def to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if type(a) is type(b) and not isinstance(a, (int, float)):
        return a == b
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    na, nb = to_number(a), to_number(b)
    return not (math.isnan(na) or math.isnan(nb)) and na == nb


def _compare(a: Any, b: Any) -> float | None:
    if isinstance(a, str) and isinstance(b, str):
        la, lb = a.lower(), b.lower()
        return (la > lb) - (la < lb)
    na, nb = to_number(a), to_number(b)
    if math.isnan(na) or math.isnan(nb):
        return None
    return (na > nb) - (na < nb)


# ---------------------------------------------------------------- parser


class _Parser:
    def __init__(self, text: str, ctx: Context):
        self.text = text
        self.tokens = _tokenize(text)
        self.i = 0
        self.ctx = ctx

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        j = self.i + offset
        return self.tokens[j] if j < len(self.tokens) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None or (value is not None and tok[1] != value):
            raise ExpressionError(f"expected {value!r} in {self.text!r}")
        self.i += 1
        return tok

    def accept(self, value: str) -> bool:
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] == value:
            self.i += 1
            return True
        return False

    def parse(self) -> Any:
        v = self.or_expr()
        if self.peek() is not None:
            raise ExpressionError(f"unexpected {self.peek()[1]!r} in {self.text!r}")
        return v

    def or_expr(self) -> Any:
        left = self.and_expr()
        while self.accept("||"):
            right = self.and_expr()
            left = left if to_bool(left) else right
        return left

    def and_expr(self) -> Any:
        left = self.cmp_expr()
        while self.accept("&&"):
            right = self.cmp_expr()
            left = right if to_bool(left) else left
        return left

    def cmp_expr(self) -> Any:
        left = self.unary()
        while True:
            tok = self.peek()
            if not tok or tok[0] != "op" or tok[1] not in ("==", "!=", "<", "<=", ">", ">="):
                return left
            self.i += 1
            right = self.unary()
            op = tok[1]
            if op == "==":
                left = _equal(left, right)
            elif op == "!=":
                left = not _equal(left, right)
            else:
                c = _compare(left, right)
                left = c is not None and {
                    "<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0
                }[op]

    def unary(self) -> Any:
        if self.accept("!"):
            return not to_bool(self.unary())
        return self.postfix()

    def postfix(self) -> Any:
        value, path = self.primary()
        while True:
            if self.accept("."):
                tok = self.take()
                key = tok[1]
                if key == "*":
                    value = _star(value)
                    path = None
                else:
                    value, path = self._prop(value, path, key)
            elif self.accept("["):
                key = self.or_expr()
                self.take("]")
                value, path = self._prop(value, path, to_str(key))
            else:
                return value

    def _prop(self, value: Any, path: str | None, key: str) -> tuple[Any, str | None]:
        if path is not None:
            full = f"{path}.{key}"
            found = self.ctx.lookup(full)
            if found is not None:
                return found, full
            if isinstance(value, dict):
                return _get_ci(value, key), full
            return None, full
        if isinstance(value, list):
            return [_get_ci(v, key) if isinstance(v, dict) else None for v in value], None
        if isinstance(value, dict):
            return _get_ci(value, key), None
        return None, None

    def primary(self) -> tuple[Any, str | None]:
        tok = self.take()
        kind, text = tok
        if kind == "num":
            return to_number(text), None
        if kind == "str":
            return text[1:-1].replace("''", "'"), None
        if kind == "op" and text == "(":
            v = self.or_expr()
            self.take(")")
            return v, None
        if kind == "ident":
            low = text.lower()
            if low == "true":
                return True, None
            if low == "false":
                return False, None
            if low == "null":
                return None, None
            if self.accept("("):
                args = []
                if not self.accept(")"):
                    args.append(self.or_expr())
                    while self.accept(","):
                        args.append(self.or_expr())
                    self.take(")")
                return self.call(low, args), None
            return self.ctx.lookup(text), text
        raise ExpressionError(f"unexpected {text!r} in {self.text!r}")

    def call(self, name: str, args: list[Any]) -> Any:
        status = self.ctx.status
        if name == "success":
            return status == "success"
        if name == "failure":
            return status == "failure"
        if name == "always":
            return True
        if name == "cancelled":
            return status == "cancelled"
        if name == "contains":
            hay, needle = args
            if isinstance(hay, list):
                return any(_equal(h, needle) for h in hay)
            return to_str(needle).lower() in to_str(hay).lower()
        if name == "startswith":
            return to_str(args[0]).lower().startswith(to_str(args[1]).lower())
        if name == "endswith":
            return to_str(args[0]).lower().endswith(to_str(args[1]).lower())
        if name == "format":
            fmt = to_str(args[0])
            return re.sub(r"\{(\d+)\}", lambda m: to_str(args[int(m.group(1)) + 1]), fmt)
        if name == "join":
            sep = to_str(args[1]) if len(args) > 1 else ","
            val = args[0]
            return sep.join(to_str(v) for v in val) if isinstance(val, list) else to_str(val)
        if name == "tojson":
            return json.dumps(args[0], indent=2)
        if name == "fromjson":
            return json.loads(to_str(args[0]))
        if name == "hashfiles":
            if self.ctx.hash_files:
                return self.ctx.hash_files([to_str(a) for a in args])
            return "local"
        raise ExpressionError(f"unknown function {name}()")


def _get_ci(d: dict, key: str) -> Any:
    if key in d:
        return d[key]
    low = key.lower()
    for k, v in d.items():
        if isinstance(k, str) and k.lower() == low:
            return v
    return None


def _star(value: Any) -> list:
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return value
    return []


# ---------------------------------------------------------------- public API


def evaluate(text: str, ctx: Context) -> Any:
    """Evaluate one expression (the part inside ``${{ }}``)."""
    return _Parser(text.strip(), ctx).parse()


def interpolate(text: str, ctx: Context) -> str:
    """Replace every ``${{ expr }}`` in ``text`` with its string value."""
    return INTERP_RE.sub(lambda m: to_str(evaluate(m.group(1), ctx)), text)


_STATUS_FN_RE = re.compile(r"\b(success|failure|always|cancelled)\s*\(", re.IGNORECASE)


def evaluate_condition(cond: Any, ctx: Context) -> bool:
    """Evaluate an ``if:`` value the way the Actions runner does.

    A missing condition means ``success()``. A condition that does not call a
    status function is implicitly ``success() && (cond)``.
    """
    if cond is None:
        return ctx.status == "success"
    if isinstance(cond, bool):
        return cond and ctx.status == "success"
    text = str(cond).strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    if not _STATUS_FN_RE.search(text):
        if ctx.status != "success":
            return False
    return to_bool(evaluate(text, ctx))
