# fastebaysearch

`fastebaysearch` is a command-line Python tool for monitoring eBay Browse API search results. It searches configured eBay marketplaces, stores seen item IDs in a local SQLite database and sends notifications only for items that have not been reported before.

Notifications can be sent by email and/or Telegram. Exchange rates are cached in SQLite so they do not need to be fetched on every run.

`fastebaysearch` is based on the original **ebaysearch** script version 0.4.0 by Kalevi Kolttonen. The original project provided a configurable Python tool for automated eBay searching, SQLite-based tracking of seen items, and email notifications. `fastebaysearch` continues that idea with a refactored codebase, significantly faster asynchronous searches, eBay Browse API support, Telegram notifications, improved SQLite handling, notification retry state, exchange-rate caching, stronger configuration validation and an expanded test suite.

Original project:
https://kolttonen.fi/computers_and_logic/programming/ebaysearch/ebaysearch.html

## Requirements

- Python 3.11 or newer
- An eBay Developer account and application credentials. The default eBay Browse API limit is typically 10,000 calls per day.
- An eBay developer application with a `Client ID` and `Client Secret`
- SMTP credentials if email notifications are enabled.
- A Telegram bot token and chat id if Telegram notifications are enabled
- Server like Raspberry PI should work fine. 

## Quick installation guide (Raspberry PI, Debian, Ubuntu)

```bash
  sudo apt update
  sudo apt install python3 python3-venv python3-pip
  cd /home/pi/fastebaysearch
  python3 -m venv .venv
  . .venv/bin/activate
  pip install -r requirements.txt
  cp ebaysearch.example.json ebaysearch.json
  nano ebaysearch.json
  python fastebaysearch.py ebaysearch.json
```

## Detailed installation instructions

Create a virtual environment and install the runtime dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuration

Copy the example configuration:

```bash
cp ebaysearch.example.json ebaysearch.json
```

On Windows PowerShell:

```powershell
Copy-Item ebaysearch.example.json ebaysearch.json
```

Edit `ebaysearch.json` for your own searches, eBay API credentials, and notification settings.

Set these eBay API credential fields:

```json
"ebay_client_id": "your-client-id",
"ebay_client_secret": "your-client-secret"
```

On the first run the script requests an OAuth token and stores it in `oauth_token.json`.

Important settings:

- `ebay_sites`: eBay marketplaces to search, for example `EBAY_US`, `EBAY_GB`, `EBAY_DE`.
- `ebay_client_id`: eBay Developer application client id.
- `ebay_client_secret`: eBay Developer application client secret.
- `ebay_search_keywords.base_terms`: main search terms.
- `ebay_search_keywords.required_terms`: required terms added to the generated search queries.
- `exclude_terms`: terms excluded from generated search queries.
- `ebay_urls_dbfile`: SQLite database file name. The file is created under the project directory.
- `use_email`: enables or disables email notifications.
- `email_max_per_run`: maximum number of pending email notification items handled in one run.
- `use_telegram`: enables or disables Telegram notifications.
- `telegram_max_per_run`: maximum number of Telegram messages sent in one run.
- `telegram_send_mode`: one of `auto`, `photo`, `photo_only`, or `text`.
- `api_concurrency`: number of eBay API searches allowed to run concurrently.
- `exchange_rate_cache_ttl_hours`: exchange-rate cache lifetime in hours. Use `0` to disable the cache.
- `log_to_console`: also writes logs to stdout/stderr when enabled.

Database, token, and log paths must stay under the project directory. The script rejects configured paths that escape the project directory.

## Command-Line Usage

Run from the project directory:

```bash
python fastebaysearch.py ebaysearch.json
```

If you want to run without activating the virtual environment, call the virtual environment's Python directly:

```bash
/path/to/fastebaysearch/.venv/bin/python /path/to/fastebaysearch/fastebaysearch.py /path/to/fastebaysearch/ebaysearch.json
```

The script writes logs to `fastebaysearch.log`. If `log_to_console` is enabled, logs are also printed to the console.

Exit codes:

- `0`: run completed successfully.
- `1`: configuration, database, token, notification, or other runtime error.
- `2`: eBay API rate limit was hit.

## Scheduled Runs With Cron

Use absolute paths in cron. eBay API credentials are read from `ebaysearch.json`.

Example: run every 30 minutes.

```cron
*/30 * * * * cd /home/user/fastebaysearch && /home/user/fastebaysearch/.venv/bin/python /home/user/fastebaysearch/fastebaysearch.py /home/user/fastebaysearch/ebaysearch.json >> /home/user/fastebaysearch/cron.log 2>&1
```

## Tests

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run the test suite:

```bash
python -m pytest -q
```
