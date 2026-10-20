from threading import Barrier, Lock

from food_record_digitizer.batch import run_batch


def test_bounded_concurrency_and_unique_records():
    barrier = Barrier(3)
    lock = Lock()
    active = peak = 0
    seen = []

    def worker(record_id):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            seen.append(record_id)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
        return {"record_id": record_id, "status": "succeeded"}

    results = list(run_batch(['a', 'b', 'c', 'a'], worker, 3))
    assert peak == 3
    assert sorted(seen) == ['a', 'b', 'c']
    assert len(results) == 3


def test_rate_limit_stops_new_dispatch():
    seen = []

    def worker(record_id):
        seen.append(record_id)
        return {"status": "failed", "error": "api_http_429"}

    assert len(list(run_batch(['a', 'b', 'c'], worker, 1))) == 1
    assert seen == ['a']
