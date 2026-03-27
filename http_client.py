"""
http_client.py
--------------
Shared HTTP session pool for all modules.

Provides a single requests.Session with connection pooling, retry logic,
and standardised headers.  All modules should import get_session() rather
than creating their own requests.Session().

Benefits:
  - Connection reuse across the bot (TCP keep-alive)
  - Single place to configure timeouts, retries, headers
  - Lower memory footprint on a 4 GB VPS
"""

import logging
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

# Module-level singleton
_session: Optional[requests.Session] = None


def get_session() -> requests.Session:
    """
    Return the shared requests.Session singleton.

    The session is created on first call with:
      - Connection pool (10 per host, 20 total)
      - Retry on transient failures (3 retries with backoff)
      - Standard JSON + XML accept headers
    """
    global _session
    if _session is None:
        _session = _create_session()
    return _session


def _create_session() -> requests.Session:
    """Build a configured requests.Session."""
    session = requests.Session()

    session.headers.update({
        "Accept": "application/json, application/xml, text/xml",
        "User-Agent": "PolymarketBot/2.0",
    })

    # Connection pooling: max 10 connections per host, 20 total
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=3,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    logger.debug("Shared HTTP session created (pool=10/20, retries=3).")
    return session


def close_session() -> None:
    """Close the shared session (call on bot shutdown)."""
    global _session
    if _session is not None:
        _session.close()
        _session = None
        logger.debug("Shared HTTP session closed.")
