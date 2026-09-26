from __future__ import annotations

import threading

import pytest

from pdfeditor.ui.jobs import Job

pytestmark = pytest.mark.gui


def test_instant_job_delivers_result(qtbot) -> None:
    """Regression: a job that finishes immediately must not lose its finished signal."""
    for _ in range(20):
        job = Job(lambda j: 42)
        with qtbot.waitSignal(job.finished, timeout=5000) as blocker:
            job.start()
        assert blocker.args == [42]


def test_partial_progress_and_failure(qtbot) -> None:
    def work(job: Job) -> str:
        job.progress(1, 2)
        job.report("half")
        job.progress(2, 2)
        return "done"

    job = Job(work)
    partials: list[object] = []
    job.partial.connect(partials.append)
    with qtbot.waitSignal(job.finished, timeout=5000):
        job.start()
    assert partials == ["half"]

    def boom(_job: Job) -> None:
        raise RuntimeError("bad input")

    failing = Job(boom)
    with qtbot.waitSignal(failing.failed, timeout=5000) as blocker:
        failing.start()
    assert blocker.args == ["bad input"]


def test_cancel(qtbot) -> None:
    started = threading.Event()

    def slow(job: Job) -> None:
        started.set()
        while True:
            job.token.check()

    job = Job(slow)
    with qtbot.waitSignal(job.cancelled, timeout=5000):
        job.start()
        started.wait(5)
        job.cancel()
    assert job.done
