"""Bounded extraction scheduling with independent provider state per record."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

STOP_ERRORS = {"api_http_401", "api_http_403", "api_http_404", "api_http_429"}


def run_batch(record_ids, worker, workers=1):
    """Yield completed results; stop scheduling on account/configuration errors."""
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 8:
        raise ValueError("workers must be an integer from 1 to 8")
    remaining = iter(dict.fromkeys(record_ids))
    stopped = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = set()
        while True:
            while not stopped and len(pending) < workers:
                record_id = next(remaining, None)
                if record_id is None:
                    break
                pending.add(pool.submit(worker, record_id))
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            results = [future.result() for future in done]
            stopped |= any(result.get("error") in STOP_ERRORS for result in results)
            yield from results
