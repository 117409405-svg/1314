# Smart Money Monitor

A lightweight Python script that keeps an eye on the gmgn.ai "Smart Degen"
leaderboard, monitors the top wallets on the Binance Smart Chain (BSC), applies
simple heuristics to infer affiliate addresses, and notifies a Telegram chat when
new activity is detected.

## Features

- Scrapes the gmgn.ai leaderboard and extracts the top smart-money wallets.
- Connects to the BSC network via Web3 and inspects recent transactions for each
  leader wallet.
- Applies configurable heuristics to flag potential affiliate addresses that
  trade alongside the leaders.
- Pushes notifications to Telegram so that you can act on fresh alpha quickly.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

The monitor is driven by environment variables. The defaults are sensible for a
quick test, but you will usually want to configure at least the Telegram values.

| Variable | Description |
| --- | --- |
| `GMGN_URL` | gmgn.ai leaderboard URL to scrape. |
| `TOP_N_WALLETS` | Number of wallets to consider (default: `10`). |
| `BSC_RPC_URL` | BSC RPC endpoint (default: `https://bsc-dataseed.binance.org/`). |
| `POLL_INTERVAL_SECONDS` | Seconds between monitoring iterations (default: `30`). |
| `AFFILIATE_VALUE_WEI` | Minimum Wei moved to qualify as affiliate activity (default: `1e17`). |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token used for notifications. |
| `TELEGRAM_CHAT_ID` | Telegram chat ID that receives notifications. |
| `REQUEST_TIMEOUT` | HTTP timeout (seconds) when scraping gmgn.ai (default: `20`). |
| `PROCESSED_HASH_RETENTION` | Number of processed transaction hashes to keep (default: `2000`). |

## Usage

Export the environment variables you need and launch the monitor:

```bash
export TELEGRAM_BOT_TOKEN="123:abc"
export TELEGRAM_CHAT_ID="987654321"
python smart_money_monitor.py
```

The script will run indefinitely, polling every `POLL_INTERVAL_SECONDS`
seconds. Logs provide visibility into scraping, transaction inspection, and
notifications.

## Caveats

- Public RPC nodes often enforce rate limits. For production use, point the
  monitor at a dedicated or paid RPC endpoint.
- The heuristics for identifying affiliate addresses are intentionally simple.
  You are encouraged to iterate on them to better match your definition of
  "smart money" behaviour.
- The gmgn.ai frontend may evolve without warning. If scraping stops working,
  adjust the selectors in `GmgnScraper`. Supplying a desktop-like User-Agent
  header (as the script now does by default) often helps avoid being served
  minimal or bot-filtered markup.
