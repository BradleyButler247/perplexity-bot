"""
redeemer.py
-----------
Automatically redeems resolved Polymarket positions to recover USDC.e.

When a market resolves, winning tokens are worth $1.00 and losing tokens
are worth $0, but both need to be explicitly redeemed on-chain via the
CTF contract's redeemPositions() function.

This module:
  1. Queries the Data API for redeemable positions.
  2. Calls redeemPositions() on the Conditional Tokens contract.
  3. Logs the transaction hash and result.

The redeemer runs once per cycle in the main bot loop, checking for
resolved positions that haven't been redeemed yet.

Contract addresses (Polygon mainnet):
  CTF:    0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
  USDC.e: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
"""

import logging
import os
import time
from typing import Dict, List, Optional

from web3 import Web3

from constants import DATA_API
from http_client import get_session
try:
    # web3 v7+
    from web3.middleware import ExtraDataToPOAMiddleware as poa_middleware
    WEB3_V7 = True
except ImportError:
    # web3 v6
    from web3.middleware import geth_poa_middleware as poa_middleware
    WEB3_V7 = False
try:
    from web3.middleware import SignAndSendRawMiddlewareBuilder
except ImportError:
    SignAndSendRawMiddlewareBuilder = None

from config import Config

logger = logging.getLogger("bot.redeemer")

# ── Contract addresses ────────────────────────────────────────────────────────
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_RPC = "https://polygon-rpc.com"

# ── ABI for redeemPositions ───────────────────────────────────────────────────
REDEEM_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


# Cooldown between redemption attempts for the same condition (seconds)
REDEEM_COOLDOWN = 600  # 10 minutes


class Redeemer:
    """
    Automatically redeems resolved Polymarket positions.

    Usage:
        redeemer = Redeemer(cfg)
        redeemer.redeem_all()  # Call once per cycle
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._session = get_session()

        # Track redeemed conditions to avoid repeated attempts
        self._redeemed: Dict[str, float] = {}  # condition_id -> timestamp

        # Web3 setup
        self._w3: Optional[Web3] = None
        self._ctf_contract = None
        self._wallet_address: str = ""
        self._init_web3()

    def _init_web3(self) -> None:
        """Initialize Web3 connection to Polygon."""
        try:
            rpc_url = os.getenv("POLYGON_RPC_URL", POLYGON_RPC)
            self._w3 = Web3(Web3.HTTPProvider(rpc_url))
            # POA middleware for Polygon
            try:
                self._w3.middleware_onion.inject(poa_middleware, layer=0)
            except Exception:
                pass  # Some web3 versions handle this differently

            # Set up signing with private key
            pk = self.cfg.PRIVATE_KEY
            account = self._w3.eth.account.from_key(pk)
            if SignAndSendRawMiddlewareBuilder:
                self._w3.middleware_onion.inject(
                    SignAndSendRawMiddlewareBuilder.build(account),
                    layer=0,
                )
            else:
                from web3.middleware import construct_sign_and_send_raw_middleware
                self._w3.middleware_onion.add(
                    construct_sign_and_send_raw_middleware(pk)
                )
            account = self._w3.eth.account.from_key(pk)
            self._w3.eth.default_account = account.address
            self._wallet_address = account.address

            # Initialize CTF contract
            ctf_addr = self._w3.to_checksum_address(CTF_ADDRESS)
            self._ctf_contract = self._w3.eth.contract(
                address=ctf_addr, abi=REDEEM_ABI
            )

            logger.info(
                "Redeemer initialized | wallet=%s | rpc=%s",
                self._wallet_address[:10], rpc_url,
            )
        except Exception as exc:
            logger.error("Failed to initialize Redeemer: %s", exc)
            self._w3 = None

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def redeem_all(self) -> int:
        """
        Check for redeemable positions and redeem them.

        Returns:
            Number of positions successfully redeemed.
        """
        if not self._w3 or not self._ctf_contract:
            return 0

        # Get the proxy wallet address for the Data API query
        proxy_wallet = self.cfg.POLYMARKET_PROXY_ADDRESS
        if not proxy_wallet:
            proxy_wallet = self._wallet_address

        redeemable = self._fetch_redeemable_positions(proxy_wallet)
        if not redeemable:
            return 0

        redeemed_count = 0
        for position in redeemable:
            condition_id = position.get("conditionId", "")
            if not condition_id:
                continue

            # Check cooldown
            last_attempt = self._redeemed.get(condition_id, 0)
            if time.time() - last_attempt < REDEEM_COOLDOWN:
                continue

            title = position.get("title", "Unknown market")
            size = float(position.get("size", 0) or 0)
            outcome = position.get("outcome", "")

            try:
                success = self._redeem_position(condition_id)
                self._redeemed[condition_id] = time.time()

                if success:
                    redeemed_count += 1
                    logger.info(
                        "Redeemed: %s | %s | %.1f shares | %s",
                        condition_id[:16], outcome, size, title[:50],
                    )
                    print(
                        f"  [\u2026] \ud83d\udcb0 Redeemed {outcome} {size:.1f} shares | {title[:50]}",
                        flush=True,
                    )
            except Exception as exc:
                logger.warning(
                    "Redemption failed for %s: %s", condition_id[:16], exc,
                )
                self._redeemed[condition_id] = time.time()

        return redeemed_count

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_redeemable_positions(self, wallet: str) -> List[dict]:
        """
        Fetch positions that can be redeemed from the Data API.

        Looks for positions where:
          - redeemable=true (market has resolved)
          - size > 0 (still holding tokens)
        """
        try:
            url = f"{DATA_API}/positions"
            params = {
                "user": wallet,
                "redeemable": "true",
                "sizeThreshold": 0.01,
                "limit": 100,
            }
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            positions = resp.json() or []

            redeemable = [
                p for p in positions
                if p.get("redeemable") is True
                and float(p.get("size", 0) or 0) > 0
            ]

            if redeemable:
                logger.info(
                    "Found %d redeemable position(s).", len(redeemable),
                )

            return redeemable
        except Exception as exc:
            logger.debug("Failed to fetch redeemable positions: %s", exc)
            return []

    def _redeem_position(self, condition_id: str) -> bool:
        """
        Execute the on-chain redeemPositions() call for a condition.

        Args:
            condition_id: The market's condition ID (0x-prefixed hex string).

        Returns:
            True if the transaction was successful.
        """
        if not self._w3 or not self._ctf_contract:
            return False

        usdc_addr = self._w3.to_checksum_address(USDC_ADDRESS)
        parent_collection = bytes(32)  # bytes32(0)

        # Ensure condition_id is properly formatted bytes32
        if not condition_id.startswith("0x"):
            condition_id = "0x" + condition_id

        logger.info("Submitting redeem tx for condition %s...", condition_id[:16])

        try:
            txn_hash_bytes = self._ctf_contract.functions.redeemPositions(
                usdc_addr,
                parent_collection,
                condition_id,
                [1, 2],  # Index sets for binary markets (both outcomes)
            ).transact()

            txn_hash = self._w3.to_hex(txn_hash_bytes)
            logger.info("Redeem tx submitted: %s", txn_hash)

            # Wait for confirmation
            receipt = self._w3.eth.wait_for_transaction_receipt(
                txn_hash, timeout=60,
            )

            if receipt["status"] == 1:
                logger.info("Redeem tx confirmed: %s", txn_hash)
                return True
            else:
                logger.warning("Redeem tx failed: %s", txn_hash)
                return False

        except Exception as exc:
            logger.error("Redeem transaction error: %s", exc)
            return False
