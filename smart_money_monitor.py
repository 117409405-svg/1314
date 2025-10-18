"""Utilities for tracking "smart money" wallets on the BSC network.

This module scrapes the gmgn.ai "Smart Degen" page to discover the current
leaderboard addresses, inspects their recent on-chain activity, applies simple
heuristics to infer related ("affiliate") addresses, and notifies a Telegram
chat whenever fresh activity is detected.

The implementation favours readability and debuggability so that it can be
used as a starting point for more production-ready tooling.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
import time
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set

import requests
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError
from web3 import Web3
from web3.types import TxData


LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class MonitorConfig:
    """Runtime configuration for :class:`SmartMoneyMonitor`.

    The configuration can be constructed directly or derived from environment
    variables using :func:`MonitorConfig.from_env`.
    """

    gmgn_url: str = (
        "https://gmgn.ai/discover/6dTHoJE8?chain=bsc&tab=smart_degen"
    )
    """Leaderboard page that surfaces the desired wallets."""

    top_n_wallets: int = 10
    """How many wallet addresses to pull from the leaderboard."""

    bsc_rpc_url: str = "https://bsc-dataseed.binance.org/"
    """HTTP RPC endpoint for the BSC network."""

    poll_interval_seconds: int = 30
    """How frequently a monitoring loop iteration should run."""

    affiliate_value_wei: int = 10**17
    """Minimum value (in Wei) a transaction must move to qualify as affiliate."""

    telegram_token: Optional[str] = None
    """Bot token for Telegram notifications."""

    telegram_chat_id: Optional[str] = None
    """Chat identifier for Telegram notifications."""

    request_timeout: int = 20
    """HTTP timeout for scraping operations."""

    @classmethod
    def from_env(cls) -> "MonitorConfig":
        """Load configuration from environment variables.

        The following variables are recognised:

        ``GMGN_URL``
            Override for :attr:`gmgn_url`.
        ``TOP_N_WALLETS``
            Numeric override for :attr:`top_n_wallets`.
        ``BSC_RPC_URL``
            Override for :attr:`bsc_rpc_url`.
        ``POLL_INTERVAL_SECONDS``
            Numeric override for :attr:`poll_interval_seconds`.
        ``AFFILIATE_VALUE_WEI``
            Numeric override for :attr:`affiliate_value_wei`.
        ``TELEGRAM_BOT_TOKEN``
            Sets :attr:`telegram_token`.
        ``TELEGRAM_CHAT_ID``
            Sets :attr:`telegram_chat_id`.
        ``REQUEST_TIMEOUT``
            Numeric override for :attr:`request_timeout`.
        """

        def maybe_int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, "").strip() or default)
            except ValueError:
                LOGGER.warning("Environment variable %s is not an integer", name)
                return default

        return cls(
            gmgn_url=os.getenv("GMGN_URL", cls.gmgn_url),
            top_n_wallets=maybe_int("TOP_N_WALLETS", cls.top_n_wallets),
            bsc_rpc_url=os.getenv("BSC_RPC_URL", cls.bsc_rpc_url),
            poll_interval_seconds=maybe_int(
                "POLL_INTERVAL_SECONDS", cls.poll_interval_seconds
            ),
            affiliate_value_wei=maybe_int(
                "AFFILIATE_VALUE_WEI", cls.affiliate_value_wei
            ),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            request_timeout=maybe_int("REQUEST_TIMEOUT", cls.request_timeout),
        )


class GmgnScraper:
    """Scrape the gmgn.ai leaderboard for the top smart-money wallets."""

    ADDRESS_PATTERN = re.compile(r"0x[a-fA-F0-9]{40}")

    def __init__(self, url: str, timeout: int = 20) -> None:
        self.url = url
        self.timeout = timeout

    def fetch_wallets(self, limit: int = 10) -> List[str]:
        """Return up to ``limit`` wallet addresses from the leaderboard.

        The gmgn.ai site renders the leaderboard dynamically, but the initial
        HTML still contains anchor tags with wallet addresses. We parse the HTML
        and pick the first ``limit`` unique addresses that resemble an EVM
        address.
        """
        LOGGER.debug("Fetching gmgn leaderboard from %s", self.url)
        response = requests.get(self.url, timeout=self.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        wallets: List[str] = []
        seen: Set[str] = set()
        for anchor in soup.find_all("a", href=True):
            match = self.ADDRESS_PATTERN.search(anchor.get("href", ""))
            if match:
                addr = Web3.to_checksum_address(match.group(0))
                if addr not in seen:
                    wallets.append(addr)
                    seen.add(addr)
            if len(wallets) >= limit:
                break

        LOGGER.info("Discovered %d leaderboard wallets", len(wallets))
        return wallets


class AffiliateHeuristics:
    """Simple heuristics for flagging addresses related to a leader wallet."""

    def __init__(self, value_threshold_wei: int) -> None:
        self.value_threshold_wei = value_threshold_wei

    def identify(self, main: str, txs: Sequence[TxData]) -> Set[str]:
        """Return a set of addresses suspected to be affiliates.

        The current heuristics are intentionally straightforward and are meant to
        be evolved over time. We consider an address to be related if:

        * It performs a transaction interacting with the same contract before
          ``main`` does.
        * It consistently follows ``main`` into transactions within a few blocks.
        * It transfers at least :attr:`value_threshold_wei`.
        """
        affiliates: Set[str] = set()
        first_seen_block: Dict[str, int] = {}
        main_block: Optional[int] = None

        for tx in txs:
            block_number = tx.get("blockNumber")
            sender = tx.get("from")
            receiver = tx.get("to")
            value = int(tx.get("value", 0))

            if sender is None or block_number is None:
                continue

            if sender.lower() == main.lower() or (receiver and receiver.lower() == main.lower()):
                # Record when the main address participated.
                if main_block is None or block_number < main_block:
                    main_block = block_number
                continue

            if value < self.value_threshold_wei:
                continue

            if sender.lower() == main.lower():
                continue

            if sender not in first_seen_block:
                first_seen_block[sender] = block_number

            if main_block is not None and block_number <= main_block:
                affiliates.add(sender)

            if receiver and receiver.lower() == main.lower():
                affiliates.add(sender)

        return affiliates


class TelegramNotifier:
    """Send activity updates to Telegram."""

    def __init__(self, token: Optional[str], chat_id: Optional[str]) -> None:
        if token and chat_id:
            self._bot: Optional[Bot] = Bot(token=token)
            self._chat_id = chat_id
        else:
            self._bot = None
            self._chat_id = None
            LOGGER.warning(
                "Telegram token or chat id not provided. Notifications are disabled."
            )

    def send(self, message: str) -> None:
        if not self._bot or not self._chat_id:
            LOGGER.debug("Skipping Telegram notification: %s", message)
            return

        try:
            self._bot.send_message(chat_id=self._chat_id, text=message)
            LOGGER.info("Sent Telegram notification")
        except TelegramError as exc:
            LOGGER.error("Failed to send Telegram message: %s", exc)


class SmartMoneyMonitor:
    """Monitor smart-money wallets and alert on affiliate activity."""

    def __init__(self, config: MonitorConfig) -> None:
        self.config = config
        self.scraper = GmgnScraper(config.gmgn_url, timeout=config.request_timeout)
        self.heuristics = AffiliateHeuristics(config.affiliate_value_wei)
        self.notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id)
        self.web3 = Web3(Web3.HTTPProvider(config.bsc_rpc_url, request_kwargs={"timeout": 30}))
        if not self.web3.is_connected():
            raise RuntimeError("Failed to connect to BSC RPC endpoint")

        self._processed_hashes: Set[str] = set()

    def _recent_transactions(self, addresses: Sequence[str], blocks: int = 20) -> Dict[str, List[TxData]]:
        latest_block = self.web3.eth.block_number
        start_block = max(0, latest_block - blocks)
        LOGGER.debug("Fetching blocks %s-%s for transaction analysis", start_block, latest_block)

        address_lookup = {addr.lower(): addr for addr in addresses}
        address_set = set(address_lookup.keys())
        transactions: Dict[str, List[TxData]] = defaultdict(list)

        for block_number in range(start_block, latest_block + 1):
            block = self.web3.eth.get_block(block_number, full_transactions=True)
            for tx in block.get("transactions", []):
                tx_hash = tx.get("hash")
                if tx_hash is None:
                    continue
                tx_hash_hex = tx_hash.hex()
                if tx_hash_hex in self._processed_hashes:
                    continue

                sender = (tx.get("from") or "").lower()
                receiver = (tx.get("to") or "").lower()

                key = None
                if sender in address_set:
                    key = address_lookup[sender]
                elif receiver in address_set:
                    key = address_lookup[receiver]

                if key is not None:
                    transactions[key].append(tx)
                    self._processed_hashes.add(tx_hash_hex)

        return transactions

    def _format_affiliate_message(
        self, leader: str, affiliates: Iterable[str], txs: Sequence[TxData]
    ) -> str:
        first_tx = txs[0] if txs else {}
        block = first_tx.get("blockNumber", "unknown")
        value = first_tx.get("value")
        value_bnb = None
        if value is not None:
            try:
                value_bnb = self.web3.from_wei(int(value), "ether")
            except (TypeError, ValueError):
                value_bnb = None

        affiliate_list = ", ".join(sorted(affiliates))
        value_str = f" (~{value_bnb} BNB)" if value_bnb is not None else ""
        return (
            f"Affiliate activity detected for leader {leader}: {affiliate_list}"
            f" in block {block}{value_str}"
        )

    def run_once(self) -> None:
        """Run a single monitoring iteration."""
        leaders = self.scraper.fetch_wallets(limit=self.config.top_n_wallets)
        if not leaders:
            LOGGER.warning("No leader wallets discovered; skipping iteration")
            return

        tx_map = self._recent_transactions(leaders)
        LOGGER.debug("Collected transactions for %d leader wallets", len(tx_map))

        for leader, txs in tx_map.items():
            affiliates = self.heuristics.identify(leader, txs)
            if not affiliates:
                continue
            message = self._format_affiliate_message(leader, affiliates, txs)
            LOGGER.info(message)
            self.notifier.send(message)

    def run_forever(self) -> None:
        """Run the monitoring loop indefinitely."""
        LOGGER.info("Starting smart-money monitoring loop")
        while True:
            start_time = time.time()
            try:
                self.run_once()
            except Exception:  # pragma: no cover - top-level safety net
                LOGGER.exception("Monitoring iteration failed")
            elapsed = time.time() - start_time
            sleep_for = max(0, self.config.poll_interval_seconds - elapsed)
            LOGGER.debug("Sleeping for %.2f seconds", sleep_for)
            time.sleep(sleep_for)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> None:
    """Entry point for the monitoring script."""
    configure_logging()
    config = MonitorConfig.from_env()
    LOGGER.info("Loaded configuration: %s", config)
    monitor = SmartMoneyMonitor(config)
    monitor.run_forever()


if __name__ == "__main__":  # pragma: no cover - script entry point
    main()
