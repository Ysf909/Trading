"""Static checks for SMC_ICT_Pro.pine that a grammar parser doesn't catch but
TradingView's compiler rejects:

* a function / method called above its declaration
  ("Could not find function or function reference")
* a global variable used inside a function declared above that variable
* a global variable used before its declaration
* a global variable modified inside a function ("Cannot modify global variable")
* a name declared twice in the same scope
* identifiers that are neither declared nor known Pine built-ins (typos)

It is line based and relies on this script's style (one statement per line,
4-space indentation). Run:  python tradingview/check_pine.py [file ...]
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

KEYWORDS = {
    "if", "else", "for", "to", "by", "in", "while", "switch", "and", "or", "not", "true", "false", "na", "var",
    "varip", "type", "method", "import", "export", "const", "simple", "series", "break", "continue", "enum",
    "int", "float", "bool", "string", "color", "line", "label", "box", "table", "array", "matrix", "map",
    "linefill", "polyline", "chart",
}
BUILTINS = {
    # series and bar state
    "open", "high", "low", "close", "volume", "time", "time_close", "bar_index", "last_bar_index", "hl2", "hlc3",
    "ohlc4", "barstate", "syminfo", "timeframe", "timenow",
    # namespaces
    "ta", "math", "str", "request", "strategy", "input", "extend", "size", "position", "text", "shape", "location",
    "xloc", "yloc", "display", "format", "barmerge", "dayofweek", "session", "currency", "font", "alert", "plot",
    "order", "scale", "ticker", "runtime", "log", "adjustment", "earnings", "dividends", "splits", "hline",
    # functions
    "indicator", "plotshape", "plotchar", "bgcolor", "barcolor", "fill", "alertcondition", "nz", "fixnan",
    "timestamp", "hour", "minute", "second", "month", "year", "dayofmonth", "weekofyear", "max_bars_back",
}


@dataclass
class Func:
    name: str
    start: int
    end: int
    is_method: bool
    params: set[str] = field(default_factory=set)
    locals: dict[str, int] = field(default_factory=dict)  # name -> first line


DECL_TYPED = re.compile(r"^(?:var\s+|varip\s+)?(?:const\s+|simple\s+|series\s+)?"
                        r"(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?(?:<[^>]*>)?)\s+([A-Za-z_]\w*)\s*=(?!=)")
DECL_UNTYPED = re.compile(r"^(?:var\s+|varip\s+)?([A-Za-z_]\w*)\s*=(?![=>])")
DECL_TUPLE = re.compile(r"^\[([^\]]+)\]\s*=(?!=)")
FOR_LOOP = re.compile(r"^for\s+(?:\[([^\]]+)\]|([A-Za-z_]\w*))\s*(?:=|in\b)")
FUNC = re.compile(r"^(method\s+)?([A-Za-z_]\w*)\s*\((.*)\)\s*=>")
TYPE = re.compile(r"^type\s+([A-Za-z_]\w*)")
REASSIGN = re.compile(r"^([A-Za-z_]\w*)\s*(?::=|\+=|-=|\*=|/=|%=)")
IDENT = re.compile(r"(?<![\w.#])([A-Za-z_]\w*)")


def strip(line: str) -> str:
    """Remove comments and string contents (keeps the quotes)."""
    out, i, quote = [], 0, ""
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                out.append(ch)
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
        elif line.startswith("//", i):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).rstrip()


def declared_names(code: str) -> list[str]:
    m = DECL_TUPLE.match(code)
    if m:
        return [n.strip() for n in m.group(1).split(",")]
    m = FOR_LOOP.match(code)
    if m:
        return [n.strip() for n in (m.group(1) or m.group(2)).split(",")]
    m = DECL_TYPED.match(code)
    if m and code.split()[0] not in ("if", "else", "for", "while", "switch", "return"):
        return [m.group(1)]
    m = DECL_UNTYPED.match(code)
    if m and m.group(1) not in KEYWORDS:
        return [m.group(1)]
    return []


def duplicate_locals(lines: list[str]) -> list[str]:
    """A name declared twice in the same block ("already declared")."""
    errors: list[str] = []
    stack: list[tuple[int, dict[str, int]]] = [(0, {})]  # (indent of the block's statements, names)
    in_type = False
    for n, code in enumerate(lines, 1):
        body = code.strip()
        if not body:
            continue
        indent = len(code) - len(code.lstrip(" "))
        if indent == 0:
            in_type = bool(TYPE.match(body))
        if in_type:
            continue
        while len(stack) > 1 and indent < stack[-1][0]:
            stack.pop()
        if indent > stack[-1][0]:
            stack.append((indent, {}))
        names = stack[-1][1]
        fm = FUNC.match(body) if indent == 0 else None
        if fm:
            continue  # a function's parameters live in its own block
        for name in declared_names(body):
            if FOR_LOOP.match(body):
                continue  # loop variables belong to the loop body
            if name in names and not (indent == 0):
                errors.append(f"line {n}: '{name}' declared twice in the same block (also line {names[name]})")
            names.setdefault(name, n)
    return errors


def check(path: Path) -> list[str]:
    raw = path.read_text().split("\n")
    lines = [strip(line) for line in raw]
    errors: list[str] = []

    types: dict[str, int] = {}
    funcs: dict[str, Func] = {}
    globals_: dict[str, int] = {}
    in_type = False
    cur: Func | None = None
    for n, code in enumerate(lines, 1):
        if not code.strip():
            continue
        indent = len(code) - len(code.lstrip(" "))
        body = code.strip()
        if indent == 0:
            in_type = False
            if cur is not None:
                cur = None
            m = TYPE.match(body)
            if m:
                types[m.group(1)] = n
                in_type = True
                continue
            m = FUNC.match(body)
            if m:
                name = m.group(2)
                if name in funcs and not m.group(1):
                    errors.append(f"line {n}: function {name} declared twice (also line {funcs[name].start})")
                params = set()
                for p in re.split(r",(?![^<]*>)", m.group(3)):
                    p = p.split("=")[0].strip()
                    if p:
                        params.add(p.split()[-1])
                cur = Func(name, n, n, bool(m.group(1)), params)
                funcs[name] = cur
                continue
            for name in declared_names(body):
                if name in globals_ and not body.startswith("["):
                    errors.append(f"line {n}: '{name}' declared twice at global scope (also line {globals_[name]})")
                globals_.setdefault(name, n)
        elif in_type:
            continue
        elif cur is not None:
            cur.end = n
            for name in declared_names(body):
                cur.locals.setdefault(name, n)

    # declarations outside functions at any depth (locals of global if/for blocks)
    block_decl: dict[str, int] = {}
    fspans = sorted((f.start, f.end) for f in funcs.values())

    def owner(n: int) -> Func | None:
        for f in funcs.values():
            if f.start <= n <= f.end:
                return f
        return None

    in_type = False
    for n, code in enumerate(lines, 1):
        body = code.strip()
        if not body:
            continue
        if code == body:
            in_type = bool(TYPE.match(body))
        if in_type or owner(n) is not None:
            continue
        for name in declared_names(body):
            block_decl.setdefault(name, n)

    methods = {name for name, f in funcs.items() if f.is_method}
    unknown: dict[str, int] = {}
    in_type = False
    for n, code in enumerate(lines, 1):
        body = code.strip()
        if not body:
            continue
        if code == body:
            in_type = bool(TYPE.match(body))
        if in_type:
            continue
        f = owner(n)
        if f is not None and n == f.start:
            # default values of parameters may use globals; the signature itself declares
            code_part = body.split("=>", 1)[1] if "=>" in body else ""
        else:
            code_part = body
        # method calls: obj.name(
        for m in re.finditer(r"\.([A-Za-z_]\w*)\s*\(", code_part):
            name = m.group(1)
            if name in methods:
                decl = funcs[name].start
                limit = f.start if f is not None else n
                if decl >= limit:
                    errors.append(f"line {n}: method {name} used before its declaration (line {decl})")
        if f is not None:
            m = REASSIGN.match(code_part)
            if m:
                name = m.group(1)
                if name not in f.params and name not in f.locals and name in globals_:
                    errors.append(f"line {n}: function {f.name} modifies global variable '{name}'")
                if name in f.params and name not in f.locals:
                    errors.append(f"line {n}: function {f.name} reassigns its parameter '{name}'")
        for m in IDENT.finditer(code_part):
            name = m.group(1)
            rest = code_part[m.end():]
            if name in KEYWORDS or name in BUILTINS:
                continue
            if re.match(r"\s*=(?![=>])", rest):  # declaration target or named argument
                continue
            if name in funcs:
                if funcs[name].is_method and m.start() > 0 and code_part[m.start() - 1] == ".":
                    continue
                decl = funcs[name].start
                limit = f.start if f is not None else n
                if decl >= limit and not (f is not None and f.name == name):
                    where = f" (inside {f.name})" if f is not None else ""
                    errors.append(f"line {n}: function {name} used{where} before its declaration (line {decl})")
                continue
            if name in types:
                if types[name] >= n:
                    errors.append(f"line {n}: type {name} used before its declaration (line {types[name]})")
                continue
            if f is not None:
                if name in f.params:
                    continue
                if name in f.locals and f.locals[name] <= n:
                    continue
                if name in globals_:
                    if globals_[name] >= f.start:
                        errors.append(f"line {n}: function {f.name} (line {f.start}) uses global '{name}' "
                                      f"declared later (line {globals_[name]})")
                    continue
                if name in f.locals:
                    continue
            else:
                first = min(globals_.get(name, 10**9), block_decl.get(name, 10**9))
                if first < 10**9:
                    if first > n:
                        errors.append(f"line {n}: '{name}' used before its declaration (line {first})")
                    continue
            unknown.setdefault(name, n)
    errors += duplicate_locals(lines)
    for name, n in sorted(unknown.items(), key=lambda kv: kv[1]):
        errors.append(f"line {n}: unknown identifier '{name}' (typo, or a built-in missing from the checker)")
    del fspans
    return errors


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or [Path(__file__).parent / "SMC_ICT_Pro.pine",
                                        Path(__file__).parent / "SMC_ICT_Pro_Strategy.pine"]
    bad = 0
    for path in files:
        errs = check(path)
        for e in errs:
            print(f"{path.name}: {e}")
        bad += len(errs)
        if not errs:
            print(f"{path.name}: ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
