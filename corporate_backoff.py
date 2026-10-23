"""Shared conservative corporate-action circuit; never retries within a request."""
import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

log = logging.getLogger(__name__)
_lock = threading.Lock()
_until = 0.0
_failures = 0


class CorporateCircuitOpen(RuntimeError):
    pass


def retry_seconds(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def corporate_get(session, url, params):
    global _until, _failures
    # Both collectors share one circuit, including the single recovery probe.
    with _lock:
        now = time.monotonic()
        if now < _until:
            raise CorporateCircuitOpen(f"Corporate actions cooling down for {int(_until - now) + 1}s")
        try:
            response = session.get(url, params=params, timeout=(4, 20))
            response.raise_for_status()
            data = response.json() if response.text.strip() else {}
        except Exception as exc:
            _failures = min(_failures + 1, 8)
            response = getattr(exc, 'response', None)
            delay = max(900, min(7200, 900 * 2 ** (_failures - 1)))
            if response is not None:
                delay = max(delay, retry_seconds(response.headers.get('Retry-After')))
            _until = time.monotonic() + delay
            log.warning('ALPACA_CORPORATE circuit=OPEN status=%s retry_in_sec=%s',
                        getattr(response, 'status_code', type(exc).__name__), int(delay))
            raise
        _until = 0.0
        _failures = 0
        return data
