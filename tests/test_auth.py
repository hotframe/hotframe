"""Tests for hotframe.auth."""

from hotframe.auth.auth import hash_password, verify_password
from hotframe.auth.crypto import generate_key
from hotframe.auth.permissions import has_permission
from hotframe.auth.rate_limit import PINRateLimiter


class TestPasswordHashing:
    def test_hash_and_verify(self):
        hashed = hash_password("secret123")
        assert hashed != "secret123"
        assert verify_password("secret123", hashed) is True
        assert verify_password("wrong", hashed) is False


class TestPermissions:
    def test_wildcard(self):
        assert has_permission(["*"], "sales.view_dashboard") is True

    def test_exact_match(self):
        assert has_permission(["sales.view_dashboard"], "sales.view_dashboard") is True

    def test_no_match(self):
        assert has_permission(["sales.view_dashboard"], "inventory.view_stock") is False

    def test_pattern_match(self):
        assert has_permission(["sales.*"], "sales.view_dashboard") is True
        assert has_permission(["sales.*"], "inventory.view_stock") is False

    def test_empty_permissions(self):
        assert has_permission([], "anything") is False


class TestPINRateLimiter:
    def test_allows_first_attempt(self):
        limiter = PINRateLimiter()
        result = limiter.check_rate_limit("user1")
        assert result.allowed is True

    def test_blocks_after_max(self):
        limiter = PINRateLimiter()
        for _ in range(10):
            limiter.record_failed_attempt("user2")
        result = limiter.check_rate_limit("user2")
        assert result.allowed is False


class TestCrypto:
    def test_generate_key(self):
        key = generate_key()
        assert isinstance(key, str)
        assert len(key) > 20
