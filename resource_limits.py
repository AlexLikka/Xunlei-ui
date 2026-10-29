"""Small shared budgets for the 2 CPU / 2 GiB deployment."""
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import threading

PLAN_SECONDS = 900
POLL_WORKERS = 2
PREVIEW_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix='preview')
REMOTE_SLOTS = threading.BoundedSemaphore(4)


def remote_io(function):
    @wraps(function)
    def limited(*args, **kwargs):
        if not REMOTE_SLOTS.acquire(timeout=60):
            raise TimeoutError('远端连接繁忙，请稍后重试')
        try:
            return function(*args, **kwargs)
        finally:
            REMOTE_SLOTS.release()
    return limited
