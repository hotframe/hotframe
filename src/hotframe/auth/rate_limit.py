"""
PIN-specific rate limiting.

In-memory rate limiter to prevent brute-force PIN attacks.
Uses escalating lockout thresholds with an optional permanent lock.

Thresholds:
- 5 failed attempts  -> 5 min lockout
- 10 failed attempts -> 30 min lockout
- 20 failed attempts -> permanent lock (requires admin unlock)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock

# Escalating thresholds: (attempt_count, lockout_seconds | None for permanent)
THRESHOLDS: list[tuple[int, int | None]] = [
    (5, 5 * 60),  # 5 attempts  -> 5 min
    (10, 30 * 60),  # 10 attempts -> 30 min
    (20, None),  # 20 attempts -> permanent
]


@dataclass(slots=True)
class _AttemptRecord:
    """Track failed attempts for a single device/IP."""

    attempts: int = 0
    locked_until: float | None = None  # Unix timestamp, None = permanent lock
    permanently_locked: bool = False


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    retry_after: int | None = None  # Seconds until unlock, None if permanent


class PINRateLimiter:
    """
    In-memory rate limiter for PIN authentication attempts.

    Keyed by ``device_token`` (primary) with ``ip`` as fallback.
    Thread-safe via a simple lock (low contention expected).
    """

    def __init__(self) -> None:
        self._records: dict[str, _AttemptRecord] = {}
        self._lock = Lock()

    def _get_key(self, device_token: str | None, ip: str | None) -> str:
        """Build a lookup key from device token or IP."""
        if device_token:
            return f"dev:{device_token}"
        if ip:
            return f"ip:{ip}"
        return "unknown"

    def check_rate_limit(
        self, device_token: str | None = None, ip: str | None = None
    ) -> RateLimitResult:
        """
        Check if an authentication attempt is allowed.

        Args:
            device_token: Unique device identifier (preferred).
            ip: Client IP address (fallback).

        Returns:
            RateLimitResult indicating whether the attempt is allowed.
        """
        key = self._get_key(device_token, ip)

        with self._lock:
            record = self._records.get(key)
            if record is None:
                return RateLimitResult(allowed=True)

            if record.permanently_locked:
                return RateLimitResult(allowed=False, retry_after=None)

            if record.locked_until is not None:
                now = time.monotonic()
                if now < record.locked_until:
                    remaining = int(record.locked_until - now) + 1
                    return RateLimitResult(allowed=False, retry_after=remaining)
                # Lock expired — allow but keep attempt count
                record.locked_until = None

        return RateLimitResult(allowed=True)

    def record_failed_attempt(
        self, device_token: str | None = None, ip: str | None = None
    ) -> RateLimitResult:
        """
        Record a failed authentication attempt and apply lockout if threshold reached.

        Args:
            device_token: Unique device identifier.
            ip: Client IP address.

        Returns:
            Current rate limit status after recording the attempt.
        """
        key = self._get_key(device_token, ip)

        with self._lock:
            record = self._records.get(key)
            if record is None:
                record = _AttemptRecord()
                self._records[key] = record

            record.attempts += 1

            # Check thresholds (highest first for escalation)
            for threshold_count, lockout_seconds in reversed(THRESHOLDS):
                if record.attempts >= threshold_count:
                    if lockout_seconds is None:
                        record.permanently_locked = True
                        record.locked_until = None
                        return RateLimitResult(allowed=False, retry_after=None)
                    else:
                        record.locked_until = time.monotonic() + lockout_seconds
                        return RateLimitResult(allowed=False, retry_after=lockout_seconds)

        return RateLimitResult(allowed=True)

    def record_success(self, device_token: str | None = None, ip: str | None = None) -> None:
        """
        Reset the attempt counter after a successful authentication.

        Args:
            device_token: Unique device identifier.
            ip: Client IP address.
        """
        key = self._get_key(device_token, ip)
        with self._lock:
            self._records.pop(key, None)

    def unlock_device(self, device_token: str | None = None, ip: str | None = None) -> None:
        """
        Admin action: unlock a locked device/IP.

        Completely removes the record, resetting all counters.

        Args:
            device_token: Unique device identifier.
            ip: Client IP address.
        """
        key = self._get_key(device_token, ip)
        with self._lock:
            self._records.pop(key, None)

    def get_status(self, device_token: str | None = None, ip: str | None = None) -> dict:
        """
        Get current rate limit status for diagnostics.

        Returns dict with attempts, locked, permanently_locked, retry_after.
        """
        key = self._get_key(device_token, ip)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return {
                    "attempts": 0,
                    "locked": False,
                    "permanently_locked": False,
                    "retry_after": None,
                }

            retry_after: int | None = None
            locked = False

            if record.permanently_locked:
                locked = True
            elif record.locked_until is not None:
                now = time.monotonic()
                if now < record.locked_until:
                    locked = True
                    retry_after = int(record.locked_until - now) + 1

            return {
                "attempts": record.attempts,
                "locked": locked,
                "permanently_locked": record.permanently_locked,
                "retry_after": retry_after,
            }

    def clear(self) -> None:
        """Remove all records. Intended for testing."""
        with self._lock:
            self._records.clear()
