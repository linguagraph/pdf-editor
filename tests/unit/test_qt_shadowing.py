"""Guard against a recurring bug: widget attributes or methods that shadow QWidget methods.

Qt's own code calls methods like ``width()``, ``font()``, ``size()`` and ``style()`` on every
widget. Assigning ``self.width = QSpinBox()`` or defining ``def style(self) -> MarkStyle`` in a
widget subclass silently breaks layout and painting. This test scans the UI sources statically.
"""

from __future__ import annotations

import ast
from pathlib import Path

from PySide6.QtWidgets import QWidget

UI = Path(__file__).resolve().parents[2] / "src" / "pdfeditor" / "ui"
QT_METHODS = {
    name
    for name in dir(QWidget)
    if callable(getattr(QWidget, name, None)) and not name.startswith("_")
}
# Method names people reach for that are Qt methods Qt itself calls; never redefine these.
NEVER_DEFINE = {
    "style",
    "size",
    "font",
    "width",
    "height",
    "pos",
    "rect",
    "palette",
    "layout",
    "window",
    "cursor",
    "geometry",
}


def _widget_classes(tree: ast.Module) -> list[ast.ClassDef]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [getattr(b, "id", getattr(b, "attr", "")) for b in node.bases]
            if any(
                b.startswith("Q") or b.endswith(("Panel", "Dialog", "Widget", "View", "Tab"))
                for b in bases
            ):
                out.append(node)
    return out


def test_no_widget_member_shadows_a_qt_method() -> None:
    problems: list[str] = []
    for path in UI.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in _widget_classes(tree):
            for node in ast.walk(cls):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, ast.Store)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                    and node.attr in QT_METHODS
                ):
                    problems.append(f"{path.name}:{node.lineno} {cls.name}: self.{node.attr} = ...")
                if isinstance(node, ast.FunctionDef) and node.name in NEVER_DEFINE:
                    problems.append(f"{path.name}:{node.lineno} {cls.name}: def {node.name}()")
    assert not problems, "members shadowing QWidget methods:\n" + "\n".join(problems)


def test_detector_catches_known_bad_patterns(tmp_path: Path, monkeypatch) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "class P(QDialog):\n"
        "    def __init__(self):\n"
        "        self.width = 1\n"
        "        self.size = 2\n"
        "    def style(self):\n"
        "        return 0\n",
        encoding="utf-8",
    )
    import tests.unit.test_qt_shadowing as module

    monkeypatch.setattr(module, "UI", tmp_path)
    try:
        module.test_no_widget_member_shadows_a_qt_method()
    except AssertionError as exc:
        text = str(exc)
        assert "self.width" in text and "self.size" in text and "def style()" in text
    else:
        raise AssertionError("the detector missed obvious shadowing")
