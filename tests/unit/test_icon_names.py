"""Every icon name the UI source passes around has its SVG in ``data/icons``.

``icon()`` raises for a missing file, but only when that code runs: a menu, dialog or mini
toolbar built late would crash the frozen app on first use. This scans the source instead.
Icon names reach ``icon()`` through helpers, so the scan finds the places that take one:

- parameters named ``*icon*`` (``icon_name``) of any function or dataclass field, matched
  by position or keyword at every call with a string literal there;
- ``for ..., icon_name, ... in <literal or parameter>`` loops, matched element-wise in the
  literal, or in the tuples passed for that parameter;
- dict literals assigned to names containing ``ICON``;
- local aliases of those helpers (``a = self._action``).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from pdfeditor.bundle import data_path

UI = Path(__file__).resolve().parents[2] / "src" / "pdfeditor" / "ui"
# Helpers whose icon parameter isn't called *icon*: name -> positional index.
EXTRA = {"icon": 0, "svg_data": 0, "_svg": 0, "_button": 1, "_tinted": 0}


def _iconish(name: str) -> bool:
    return "icon" in name.lower()


def _callee(node: ast.Call) -> str | None:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _strings_at(seq: ast.AST, index: int) -> list[tuple[str, int]]:
    """Element ``index`` of each tuple in a literal sequence of tuples."""
    found = []
    if isinstance(seq, ast.Tuple | ast.List | ast.Set):
        for item in seq.elts:
            if isinstance(item, ast.Tuple | ast.List) and len(item.elts) > index:
                el = item.elts[index]
                if isinstance(el, ast.Constant) and isinstance(el.value, str):
                    found.append((el.value, el.lineno))
    return found


def _scan() -> dict[str, list[str]]:
    trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in UI.rglob("*.py")}
    positions: dict[str, set[int]] = defaultdict(set)  # callee -> icon arg index
    keywords: dict[str, set[str]] = defaultdict(set)  # callee -> icon keyword
    loops: dict[str, set[tuple[int, int]]] = defaultdict(set)  # callee -> (arg, tuple index)
    for name, index in EXTRA.items():
        positions[name].add(index)
    used: dict[str, list[str]] = defaultdict(list)

    for path, tree in trees.items():
        literals: dict[str, ast.AST] = {}  # module-level NAME = (...) for loops over them
        for node in tree.body:
            if isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and node.value is not None:
                        literals[t.id] = node.value
        owners = {  # a constructor is called by its class's name
            id(item): node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            for item in node.body
            if isinstance(item, ast.FunctionDef)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                key = owners.get(id(node), "") if node.name == "__init__" else node.name
                args = [a.arg for a in node.args.args]
                offset = 1 if args[:1] in (["self"], ["cls"]) else 0
                for i, arg in enumerate(args):
                    if _iconish(arg):
                        positions[key].add(i - offset)
                        keywords[key].add(arg)
                for loop in ast.walk(node):
                    if not (isinstance(loop, ast.For) and isinstance(loop.target, ast.Tuple)):
                        continue
                    for k, el in enumerate(loop.target.elts):
                        if isinstance(el, ast.Name) and _iconish(el.id):
                            it = loop.iter
                            if isinstance(it, ast.Name) and it.id in args:
                                loops[key].add((args.index(it.id) - offset, k))
                            elif isinstance(it, ast.Name) and it.id in literals:
                                for value, line in _strings_at(literals[it.id], k):
                                    used[value].append(f"{path.name}:{line}")
                            else:
                                for value, line in _strings_at(it, k):
                                    used[value].append(f"{path.name}:{line}")
            elif isinstance(node, ast.ClassDef):  # dataclass fields, e.g. Mode(text, icon)
                fields = [
                    s.target.id
                    for s in node.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                ]
                for i, f in enumerate(fields):
                    if _iconish(f):
                        positions[node.name].add(i)
                        keywords[node.name].add(f)
            elif isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Dict):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and "ICON" in t.id for t in targets):
                    for v in node.value.values:
                        if isinstance(v, ast.Constant) and isinstance(v.value, str):
                            used[v.value].append(f"{path.name}:{v.lineno}")

    for path, tree in trees.items():
        aliases = {  # a = self._action
            t.id: value.attr if isinstance(value, ast.Attribute) else value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(value := node.value, ast.Attribute | ast.Name)
            for t in node.targets
            if isinstance(t, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if name is None:
                continue
            if name not in positions and name not in loops:
                name = aliases.get(name, name)
            for i in positions.get(name, ()):
                if 0 <= i < len(node.args):
                    arg = node.args[i]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        used[arg.value].append(f"{path.name}:{arg.lineno}")
            for kw in node.keywords:
                if kw.arg in keywords.get(name, ()):
                    v = kw.value
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        used[v.value].append(f"{path.name}:{v.lineno}")
            for arg_index, k in loops.get(name, ()):
                if 0 <= arg_index < len(node.args):
                    for value, line in _strings_at(node.args[arg_index], k):
                        used[value].append(f"{path.name}:{line}")
    return used


def test_every_icon_name_in_the_ui_is_bundled() -> None:
    used = _scan()
    folder = data_path("icons")
    missing = {
        name: where for name, where in used.items() if not (folder / f"{name}.svg").is_file()
    }
    assert missing == {}
    # the scan itself still finds the UI's icons (it would pass vacuously otherwise)
    bundled = {p.stem for p in folder.glob("*.svg")}
    assert len(used) > 0.9 * len(bundled), sorted(bundled - set(used))
