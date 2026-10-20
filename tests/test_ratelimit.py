import threading
import time

from flypoints.ratelimit import RateLimiter


def test_min_interval_between_starts():
    lim = RateLimiter(0.1, 10)
    starts = []
    for _ in range(3):
        with lim:
            starts.append(time.monotonic())
    assert starts[1] - starts[0] >= 0.07
    assert starts[2] - starts[1] >= 0.07


def test_max_concurrent():
    lim = RateLimiter(0, 2)
    active, peak, lock = 0, 0, threading.Lock()

    def work():
        nonlocal active, peak
        with lim:
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak == 2


def test_releases_on_exception():
    lim = RateLimiter(0, 1)
    try:
        with lim:
            raise RuntimeError
    except RuntimeError:
        pass
    with lim:  # would deadlock if the slot leaked
        pass
