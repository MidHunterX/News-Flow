"""Pytest fixtures for scraper integration tests.

Resets the singleton HttpClient between tests so each async test
gets a fresh client bound to its own event loop.
"""

import pytest

from app.client import HttpClient


@pytest.fixture(autouse=True)
def _reset_http_client():
    """Ensure every test starts with a fresh HTTP client."""
    HttpClient._instance = None
    yield
    HttpClient._instance = None
