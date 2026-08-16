"""Resilient HTTP calls, shared across every module that talks to a
real network endpoint (`ingest.py`'s SEC downloads, `mapping.py`'s
OpenFIGI batches, `universe.py`'s one-time S&P history fetch).

Both functions here started as independent, near-identical copies (one in
`ingest.py`, one in `mapping.py`) written after two separate real
mid-run failures (`mapping.py`'s OpenFIGI POST killed by a real
"No route to host" ~49 minutes into an unattended build; `ingest.py`'s own
downloads have the same risk profile -- dozens of sequential calls, a long
unattended run). Consolidated here once a third module (`universe.py`)
needed the same resilience, rather than writing a third copy.
"""

import time

import requests


def get_with_retry(url: str, *, timeout: int, headers: dict | None = None, max_attempts: int = 5) -> requests.Response:
    """GET with exponential-backoff retry on transient failures --
    connection errors/timeouts and 429/5xx. Fixed 5s/10s/20s/40s schedule
    (no rate-limit headers available to compute a precise backoff from).
    Prints on every retry; any failure that survives every attempt
    propagates as a normal exception -- never silently swallowed, always
    visible with which URL and how many attempts it took.
    """
    delay = 5
    for attempt in range(max_attempts):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == max_attempts - 1:
                raise
            print(f"  {exc.__class__.__name__} fetching {url}, retrying in {delay}s "
                  f"(attempt {attempt + 1}/{max_attempts})...")
        except requests.exceptions.HTTPError as exc:
            # A genuine client error (404, ...) is permanent, not transient
            # -- only retry a status this specific (429/5xx), and only if
            # the status is actually determinable (a response we can't
            # inspect gets re-raised immediately, same as any other
            # unretryable failure).
            status = getattr(exc.response, "status_code", None)
            if attempt == max_attempts - 1 or status is None or (status != 429 and status < 500):
                raise
            print(f"  HTTP {status} fetching {url}, retrying in {delay}s "
                  f"(attempt {attempt + 1}/{max_attempts})...")
        else:
            return resp
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")  # pragma: no cover


def post_with_retry(
    url: str, *, headers: dict, json, timeout: int = 30, max_attempts: int = 5
) -> requests.Response:
    """POST with the same retry policy as get_with_retry (connection
    errors/timeouts, 429/5xx, fixed 5s/10s/20s/40s backoff)."""
    delay = 5
    for attempt in range(max_attempts):
        try:
            resp = requests.post(url, headers=headers, json=json, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == max_attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_attempts - 1:
                resp.raise_for_status()
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("unreachable")  # pragma: no cover
