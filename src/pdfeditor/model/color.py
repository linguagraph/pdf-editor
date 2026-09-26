"""Engine-neutral RGB(A) color with float components in [0, 1]."""

from __future__ import annotations

from dataclasses import dataclass


def _clamp(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


@dataclass(frozen=True, slots=True)
class Color:
    r: float
    g: float
    b: float
    a: float = 1.0

    def __post_init__(self) -> None:
        for name in ("r", "g", "b", "a"):
            object.__setattr__(self, name, _clamp(float(getattr(self, name))))

    @classmethod
    def from_hex(cls, value: str) -> Color:
        """Parse ``#rgb``, ``#rrggbb`` or ``#rrggbbaa``."""
        h = value.lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) not in (6, 8):
            raise ValueError(f"invalid hex color: {value!r}")
        parts = [int(h[i : i + 2], 16) / 255 for i in range(0, len(h), 2)]
        return cls(*parts)

    @classmethod
    def from_int(cls, value: int) -> Color:
        """From a packed 0xRRGGBB integer (as MuPDF reports span colors)."""
        return cls(((value >> 16) & 0xFF) / 255, ((value >> 8) & 0xFF) / 255, (value & 0xFF) / 255)

    @classmethod
    def from_components(cls, comps: tuple[float, ...] | list[float]) -> Color | None:
        """From PDF color components: 1 = gray, 3 = RGB, 4 = CMYK (naive conversion)."""
        if not comps:
            return None
        if len(comps) == 1:
            return cls(comps[0], comps[0], comps[0])
        if len(comps) == 3:
            return cls(comps[0], comps[1], comps[2])
        if len(comps) == 4:
            c, m, y, k = comps
            return cls((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
        raise ValueError(f"unsupported color component count: {len(comps)}")

    def to_hex(self, with_alpha: bool = False) -> str:
        vals = [self.r, self.g, self.b] + ([self.a] if with_alpha else [])
        return "#" + "".join(f"{round(v * 255):02x}" for v in vals)

    def rgb(self) -> tuple[float, float, float]:
        return (self.r, self.g, self.b)


BLACK = Color(0, 0, 0)
WHITE = Color(1, 1, 1)
RED = Color(1, 0, 0)
YELLOW = Color(1, 1, 0)
