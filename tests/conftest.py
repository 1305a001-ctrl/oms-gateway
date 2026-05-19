"""Test fixtures — set required env vars so settings load without a real .env."""
import os

import pytest

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault(
    "AICORE_DB_URL", "postgresql://test:test@localhost:5432/test"
)
# Tests should run without the live-strategy whitelist gating every call —
# the whitelist is explicitly tested in test_strategy_whitelist.py. Default
# empty = "no whitelist enforced" (back-compat path).
os.environ.setdefault("LIVE_STRATEGY_WHITELIST_CSV", "")


@pytest.fixture(autouse=True)
def _disable_strategy_whitelist_in_tests(monkeypatch):
    """Most preflight tests don't care about the whitelist — disable it.
    Tests that DO want to assert whitelist behavior monkeypatch the setting
    back to a non-empty value explicitly."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "live_strategy_whitelist_csv", "", raising=False)
