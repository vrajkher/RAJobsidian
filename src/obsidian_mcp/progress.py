"""Progress reporting and cancellation for synchronous tools.

The SDK runs sync tools in worker threads. These helpers bridge back to the event loop:
``reporter(ctx)`` sends ``notifications/progress`` (only when the client asked for progress
with a progressToken), and ``cancelled()`` tells long loops whether the request was cancelled
so they can stop, clean up, and raise.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio.from_thread

Progress = Callable[[float, float | None, str | None], None]


def no_progress(progress: float, total: float | None = None, message: str | None = None) -> None:
    return None


def reporter(ctx: Any) -> Progress:
    def report(progress: float, total: float | None = None, message: str | None = None) -> None:
        try:
            anyio.from_thread.run(ctx.report_progress, progress, total, message)
        except Exception:  # progress is best-effort: never fail the tool because of it
            pass

    return report


def cancel_checker() -> Callable[[], bool]:
    """Return a function telling a worker-thread loop whether its request was cancelled.

    Call it from the tool body (the worker thread). Outside a worker thread there is no
    request to cancel, so the returned function always answers False. Inside one, losing
    the event loop later also counts as cancellation: nobody is waiting for the result.
    """
    try:
        anyio.from_thread.check_cancelled()
    except anyio.NoEventLoopError:
        return lambda: False
    except anyio.get_cancelled_exc_class():
        return lambda: True

    def cancelled() -> bool:
        try:
            anyio.from_thread.check_cancelled()
        except (anyio.get_cancelled_exc_class(), anyio.NoEventLoopError):
            return True
        return False

    return cancelled
