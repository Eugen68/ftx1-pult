"""Cancellable waits for read-only network calls.

urllib cannot interrupt a request waiting for its response. Its worker must not
keep the application alive after Exit. File writes intentionally continue to use
asyncio.to_thread, whose executor is joined at shutdown.
"""
import asyncio
import threading

_slots = threading.BoundedSemaphore(8)


async def network_call(function, *args, **kwargs):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    while not _slots.acquire(blocking=False):
        await asyncio.sleep(0.05)

    def deliver(result, error):
        if future.done():
            return
        if error is None:
            future.set_result(result)
        else:
            future.set_exception(error)

    def worker():
        result, error = None, None
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            error = exc
        finally:
            _slots.release()
        try:
            loop.call_soon_threadsafe(deliver, result, error)
        except RuntimeError:  # the application has already closed its loop
            pass

    try:
        threading.Thread(target=worker, name='ftx-network', daemon=True).start()
    except BaseException:
        _slots.release()
        raise
    return await future
