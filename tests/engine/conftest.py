from __future__ import annotations

import pytest

from pdfeditor.engine.base import Engine
from pdfeditor.engine.registry import available_engines, get_engine


@pytest.fixture(params=available_engines())
def engine(request: pytest.FixtureRequest) -> Engine:
    """Contract tests run once per registered engine backend."""
    return get_engine(request.param)
