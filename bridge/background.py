"""Work handed to the event loop from a thread that is not it.

Every callback from the SIP side arrives on a plain worker thread and
has something for the loop to do. This is the one way across, and it
exists rather than calling asyncio directly because of what asyncio
does with the result.
"""
import asyncio
import traceback


def run_logged(coro, loop, label: str):
    """asyncio.run_coroutine_threadsafe() returns a concurrent.futures.Future
    whose exception is silently dropped unless something calls .result() on
    it - unlike a plain asyncio Task, it does NOT log on garbage collection.
    Every sip_call.CallManager callback in this module schedules its async
    work this way from a plain worker thread, so without this wrapper any
    exception anywhere in that coroutine (offer/answer negotiation, codec
    setup, ...) simply vanishes with zero trace, no matter how bad."""
    future = asyncio.run_coroutine_threadsafe(coro, loop)

    def _log_if_failed(f):
        exc = f.exception()
        if exc is not None:
            print(f"[talk] ERROR in {label}: {exc!r}")
            traceback.print_exception(type(exc), exc, exc.__traceback__)

    future.add_done_callback(_log_if_failed)
    return future
