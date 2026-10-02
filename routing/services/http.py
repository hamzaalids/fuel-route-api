"""One shared ``requests.Session`` for all outbound calls.

Retries are deliberately not configured at the adapter level: each service
decides what is safe to retry (OSRM retries once on timeout/5xx only).
"""

from __future__ import annotations

import threading

import requests

_session: requests.Session | None = None
_lock = threading.Lock()


def get_session() -> requests.Session:
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                session = requests.Session()
                adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                _session = session
    return _session
