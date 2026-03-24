"""
client_manager.py
-----------------
Initialises and owns the Polymarket ClobClient singleton.

Other modules should call get_client() rather than creating their own client.
This centralises credential management and ensures a single authenticated
session is shared across the bot.
"""

import logging
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from config import Config

logger = logging.getLogger(__name__)

# Polymarket CLOB host and Polygon chain ID (never changes)
CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

_client: Optional[ClobClient] = None


def init_client(cfg: Config) -> ClobClient:
    """
    Create and authenticate the ClobClient based on the bot's configuration.

    Supports all three Polymarket login / signature types:
        0 — Raw EOA (direct wallet, no proxy)
        1 — Email / Magic.link wallet
        2 — Browser wallet (MetaMask, Coinbase Wallet, etc.)

    After initialisation, L2 API credentials are automatically derived or
    retrieved and set on the client so authenticated endpoints are available.

    Args:
        cfg: Populated Config object.

    Returns:
        Authenticated ClobClient.

    Raises:
        RuntimeError: If the client cannot be initialised or authenticated.
    """
    global _client

    logger.info(
        "Initialising ClobClient | sig_type=%d | proxy=%s",
        cfg.SIGNATURE_TYPE,
        cfg.POLYMARKET_PROXY_ADDRESS or "none (EOA)",
    )

    try:
        if cfg.SIGNATURE_TYPE == 0:
            # ── EOA: trades directly from the key-derived address ───────────
            client = ClobClient(
                CLOB_HOST,
                key=cfg.PRIVATE_KEY,
                chain_id=POLYGON_CHAIN_ID,
            )
            logger.info("ClobClient created for EOA wallet (sig_type=0).")
            _warn_eoa_allowance()

        elif cfg.SIGNATURE_TYPE == 1:
            # ── Email / Magic wallet ──────────────────────────────────────
            client = ClobClient(
                CLOB_HOST,
                key=cfg.PRIVATE_KEY,
                chain_id=POLYGON_CHAIN_ID,
                signature_type=1,
                funder=cfg.POLYMARKET_PROXY_ADDRESS,
            )
            logger.info(
                "ClobClient created for Magic/email wallet (sig_type=1) | funder=%s",
                cfg.POLYMARKET_PROXY_ADDRESS,
            )

        elif cfg.SIGNATURE_TYPE == 2:
            # ── Browser wallet (MetaMask, etc.) ────────────────────────────
            client = ClobClient(
                CLOB_HOST,
                key=cfg.PRIVATE_KEY,
                chain_id=POLYGON_CHAIN_ID,
                signature_type=2,
                funder=cfg.POLYMARKET_PROXY_ADDRESS,
            )
            logger.info(
                "ClobClient created for browser wallet (sig_type=2) | funder=%s",
                cfg.POLYMARKET_PROXY_ADDRESS,
            )

        else:
            raise ValueError(f"Unsupported SIGNATURE_TYPE: {cfg.SIGNATURE_TYPE}")

        # ── Derive L2 API credentials ──────────────────────────────────────
        # create_or_derive_api_creds() either creates new creds (first run)
        # or re-derives deterministic creds from the private key.
        print("  […] 🔑 Deriving L2 API credentials...", flush=True)
        logger.info("Deriving L2 API credentials…")
        api_creds: ApiCreds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)
        logger.info("L2 API credentials set successfully.")

        # ── Sanity-check connectivity ──────────────────────────────────────
        print("  […] 🔑 Testing CLOB connectivity...", flush=True)
        server_time = client.get_server_time()
        print("  […] ✅ Connected to Polymarket CLOB", flush=True)
        logger.info("CLOB server time: %s", server_time)

        _client = client
        return _client

    except Exception as exc:
        logger.error("Failed to initialise ClobClient: %s", exc, exc_info=True)
        raise RuntimeError(f"ClobClient initialisation failed: {exc}") from exc


def get_client() -> ClobClient:
    """
    Return the singleton ClobClient.  init_client() must be called first.

    Raises:
        RuntimeError: If init_client() has not been called.
    """
    if _client is None:
        raise RuntimeError(
            "ClobClient has not been initialised. Call init_client(cfg) first."
        )
    return _client


def _warn_eoa_allowance() -> None:
    """
    Emit a reminder for EOA users to set token allowances.

    EOA wallets must grant the Polymarket exchange contract permission to
    spend their USDC and conditional tokens before trading can proceed.
    This is a one-time on-chain transaction.  See:
    https://github.com/Polymarket/py-clob-client/blob/master/examples/set_allowance.py
    """
    logger.warning(
        "EOA wallet detected.  Make sure you have approved the Polymarket "
        "exchange contract to spend your USDC and conditional tokens.  "
        "Run the allowance script once if you haven't already:\n"
        "  https://github.com/Polymarket/py-clob-client/blob/master/examples/set_allowance.py"
    )


def get_api_credentials(client: Optional[ClobClient] = None) -> dict:
    """
    Return the current L2 API credentials as a plain dict (for WebSocket auth).

    Args:
        client: ClobClient to extract creds from.  Defaults to singleton.

    Returns:
        Dict with keys: apiKey, secret, passphrase.
    """
    c = client or get_client()
    creds = c.creds
    if creds is None:
        raise RuntimeError("ClobClient has no API credentials set.")
    return {
        "apiKey": creds.api_key,
        "secret": creds.api_secret,
        "passphrase": creds.api_passphrase,
    }
