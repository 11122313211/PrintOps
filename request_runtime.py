"""Request-local monotonic budgets for blocking standard-library operations.

Only read-only outbound I/O runs in bounded daemon workers. Business mutations
remain on the request thread. An OS resolver cannot be forcibly cancelled in
Python; late I/O results are discarded and still consume a bounded worker slot
until their operation exits, rather than retaining an HTTP handler forever.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
import queue
import threading
import time
from typing import Callable, TypeVar


class DeadlineExceeded(TimeoutError):
    """The total request budget, rather than one socket operation, expired."""


class OutboundBusy(DeadlineExceeded):
    """No bounded outbound worker slot was free before the deadline.

    Distinct from a socket or DNS timeout: the operation never started, so a
    caller can tell the user to retry instead of blaming the endpoint.
    """


_DEADLINE: ContextVar[float | None] = ContextVar("printops_request_deadline", default=None)
_OUTBOUND_SLOTS = threading.BoundedSemaphore(8)
# Saving settings is user-blocking and repairs a broken endpoint.  Letting a
# stuck capability probe own every outbound slot would block the very save
# that fixes it, so control-plane I/O gets its own small pool instead.
CONTROL_SLOTS = threading.BoundedSemaphore(2)
T = TypeVar("T")


def current_deadline() -> float | None:
    return _DEADLINE.get()


def remaining(deadline: float | None = None) -> float | None:
    deadline = current_deadline() if deadline is None else deadline
    if deadline is None:
        return None
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise DeadlineExceeded("request deadline exceeded")
    return seconds


def check_deadline() -> None:
    remaining()


def replace_deadline(deadline: float) -> None:
    """Raise the active request budget for a route that declared more work.

    Only the HTTP handler calls this, for routes whose bounded network
    budget is announced up front (capability probe). The new deadline is
    capped by the caller; an absent deadline is never invented here.
    """
    current = _DEADLINE.get()
    if current is not None and deadline > current:
        _DEADLINE.set(deadline)


def operation_deadline(seconds: float, deadline: float | None = None) -> float:
    """A local operation may shorten the request budget, never extend it."""
    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("operation timeout must be positive and finite")
    candidates = [time.monotonic() + seconds]
    if current_deadline() is not None:
        candidates.append(current_deadline())
    if deadline is not None:
        candidates.append(deadline)
    result = min(candidates)
    remaining(result)
    return result


@contextmanager
def deadline_scope(deadline: float | None):
    parent = current_deadline()
    effective = min(parent, deadline) if parent is not None and deadline is not None else parent or deadline
    token = _DEADLINE.set(effective)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


@contextmanager
def deadline_lock(lock):
    budget = remaining()
    acquired = lock.acquire() if budget is None else lock.acquire(timeout=budget)
    if not acquired:
        raise DeadlineExceeded("request deadline expired waiting for lock")
    try:
        check_deadline()
        yield
    finally:
        lock.release()


def deadline_sleep(seconds: float, deadline: float) -> None:
    time.sleep(min(seconds, remaining(deadline)))
    remaining(deadline)


def bounded_io(operation: Callable[[], T], deadline: float, slots=None) -> T:
    """Wait only until deadline, including worker capacity and DNS/header I/O.

    The worker must be read-only. Never dispatch Agent/state updates here: a
    timed-out worker can finish late, and no late result is applied by callers.
    ``slots`` selects which bounded pool the worker draws from; the default
    shares the model outbound pool, while control-plane callers pass
    ``CONTROL_SLOTS`` so a stuck model probe cannot starve them.
    """
    pool = _OUTBOUND_SLOTS if slots is None else slots
    if not pool.acquire(timeout=remaining(deadline)):
        raise OutboundBusy("outbound capacity exhausted before the deadline")
    result: queue.Queue = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            with deadline_scope(deadline):
                check_deadline()
                value = operation()
                check_deadline()
                result.put((True, value))
        except BaseException as error:
            result.put((False, error))
        finally:
            pool.release()

    try:
        worker = threading.Thread(target=run, name="printops-outbound", daemon=True)
        worker.start()
    except BaseException:
        pool.release()
        raise
    try:
        ok, value = result.get(timeout=remaining(deadline))
    except queue.Empty:
        raise DeadlineExceeded("request deadline expired during outbound I/O") from None
    remaining(deadline)
    if not ok:
        raise value
    return value
