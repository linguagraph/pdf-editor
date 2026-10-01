"""Whether UI animations should run, following the system's "show animations" setting.

Windows exposes it as SPI_GETCLIENTAREAANIMATION (Settings > Accessibility > Visual effects >
Animation effects). Elsewhere animations are on. Tests force a value with
:func:`set_animations_enabled` so they don't depend on the machine they run on.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

DURATION_MS = 150  # zoom and page-jump animations
FADE_MS = 200  # the floating page/zoom pill

SPI_GETCLIENTAREAANIMATION = 0x1042

_override: bool | None = None


def set_animations_enabled(value: bool | None) -> None:
    """Force animations on or off (tests); ``None`` follows the system again."""
    global _override
    _override = value


def system_animations_enabled() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        value = wintypes.BOOL(True)
        ok = ctypes.windll.user32.SystemParametersInfoW(  # type: ignore[attr-defined,unused-ignore]
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0
        )
        return bool(value.value) if ok else True
    except Exception:  # very old Windows or a sandbox without user32: keep the default
        log.debug("can't read the animation setting", exc_info=True)
        return True


def animations_enabled() -> bool:
    """Checked when an animation starts, so a settings change applies without a restart."""
    return _override if _override is not None else system_animations_enabled()
