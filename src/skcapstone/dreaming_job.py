"""Entrypoint module for the ``dreaming-reflection`` skscheduler job.

The `python`-type job in ``jobs.yaml`` (``callback: skcapstone.dreaming_job:run_dreaming_job``)
is a zero-argument callable (see ``scheduler_runner.py::JobRunner._run_python``). Because
that job runs in-process (same Python process as the daemon, on a short-lived worker
thread — never a subprocess), a module-level reference cell is enough to hand the job
the live ``consciousness_loop`` instance without changing the callback signature.

``daemon.py`` calls :func:`set_consciousness_loop` once, right after the consciousness
loop is loaded (or confirmed absent under ``--no-consciousness``), during
``_load_components()``. :func:`run_dreaming_job` (added in a follow-up commit) reads it
back via :func:`get_consciousness_loop`.
"""
from __future__ import annotations

_consciousness_loop: object | None = None


def set_consciousness_loop(loop: object | None) -> None:
    """Register the in-process consciousness_loop reference for the dreaming job.

    Args:
        loop: The active ``ConsciousnessLoop`` instance, or ``None`` when
            consciousness is disabled (e.g. ``--no-consciousness``).
    """
    global _consciousness_loop
    _consciousness_loop = loop


def get_consciousness_loop() -> object | None:
    """Return whatever consciousness_loop reference was last registered.

    Returns:
        The ``ConsciousnessLoop`` instance passed to the most recent
        :func:`set_consciousness_loop` call, or ``None`` if never set or
        explicitly cleared.
    """
    return _consciousness_loop
