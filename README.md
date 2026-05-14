# fastebaysearch

`fastebaysearch` is a command-line Python tool for running large numbers of searches across multiple eBay marketplaces.

When run as a scheduled task, it provides a more powerful and flexible alternative to eBay’s own saved search alerts, especially for collectors tracking many specific items.

The tool searches configured marketplaces, stores seen item IDs in a local SQLite database, and reports only new results by email, Telegram, or local HTML report files.

**Telegram**

![Telegram](samples/Telegram.png)

**Email**

![Email](samples/email.png)

`fastebaysearch` is based on the original **ebaysearch** script version 0.4.0 by Kalevi Kolttonen.

It continues the original idea of configurable eBay searches, SQLite-based tracking, and email notifications with a refactored asynchronous codebase, eBay Browse API support, Telegram notifications, HTML reports, retry handling, exchange-rate caching, stronger validation, and expanded tests.

Original project:  
https://kolttonen.fi/computers_and_logic/programming/ebaysearch/ebaysearch.html

## Features

- Asynchronous search execution for faster bulk searches and scheduled monitoring runs.
- Ability to search all eBay sites with a single search.
- eBay Browse API search monitoring for configured marketplaces.
- Local SQLite database for tracking seen items and search history.
- Comprehensive email reports, local HTML report files, and Telegram notifications with details and links for new results.
- Telegram image notification support.
- Currency conversion support with SQLite-cached exchange rates, so they do not need to be fetched on every run.
- Configurable search terms, required and excluded terms, notification limits, parallel API request limits and more.

## Requirements

- Python 3.11 or newer
- An eBay Developer account and an application with a `Client ID` and `Client Secret`.
- SMTP credentials if email notifications are enabled.
- A Telegram bot token and chat ID if Telegram notifications are enabled. See instructions: https://www.youtube.com/watch?v=SmckRL4n1_8&t=26s
- A small server, such as a Raspberry Pi, should work fine.

## Quick installation guide (Debian, Ubuntu)

```bash
  sudo apt update
  sudo apt install -y python3 python3-venv python3-pip
  
  cd /home/<profile_name>/fastebaysearch
  
  python3 -m venv .venv
  source .venv/bin/activate
  
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  
  cp ebaysearch.example.json ebaysearch.json
```
Edit the configuration file and run the script:
```
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

Configuration file fields:

- `use_email`: enables or disables email notifications.
- `use_telegram`: enables or disables Telegram notifications.
- `use_html_report`: enables or disables local HTML report files for new results from the current run.
- `html_report_dir`: local report directory, relative to the directory where the script is run. Defaults to the current working directory when omitted.
- `html_report_max_per_run`: maximum number of new results written to one HTML report.
- `telegram_max_per_run`: maximum number of Telegram messages sent in one run.
- `telegram_token`: Telegram bot token used when Telegram notifications are enabled.
- `telegram_chat_id`: Telegram chat id where notifications are sent.
- `telegram_send_mode`: one of `auto`, `photo`, `photo_only`, or `text`.
- `telegram_disable_web_preview`: disables Telegram link previews for text messages when enabled.
- `ebay_sites`: eBay marketplaces to search, for example `EBAY_US`, `EBAY_GB`, `EBAY_DE`.
- `exclude_terms`: terms excluded from generated search queries.
- `ebay_client_id`: eBay Developer application client id.
- `ebay_client_secret`: eBay Developer application client secret.
- `ebay_urls_dbfile`: SQLite database file name. The file is created under the project directory.
- `ebay_oauth_file`: OAuth token cache file name. The file is created under the project directory.
- `ebay_search_keywords`: search keyword configuration object.
- `ebay_search_keywords.base_terms`: main search terms.
- `ebay_search_keywords.required_terms`: required terms added to the generated search queries.
- `smtp_server`: SMTP server hostname used for email notifications.
- `smtp_port`: SMTP server port.
- `smtp_starttls_encryption`: enables or disables SMTP STARTTLS.
- `smtp_authentication`: enables or disables SMTP authentication.
- `smtp_login`: SMTP username.
- `smtp_password`: SMTP password.
- `email_sender`: email sender address.
- `email_receiver`: email recipient address.
- `email_subject`: subject line for result emails.
- `email_person_name`: recipient name used in email and clean-search report text.
- `email_max_per_run`: maximum number of pending email notification items handled in one run.
- `api_concurrency`: number of eBay API searches allowed to run concurrently.
- `exchange_rate_cache_ttl_hours`: exchange-rate cache lifetime in hours. Use `0` to disable the cache.
- `log_to_console`: also writes logs to stdout/stderr when enabled.
- `log_max_size_mb`: maximum size of `fastebaysearch.log` before rotation, in MiB. Defaults to `10`.

Database, token and log paths must stay under the project directory. The script rejects configured paths that escape the project directory.

## Usage

For the first run, it is recommended to use the clean search parameter or email to receive the results. You are likely to get a fairly large number of matches in most cases. Only after that should you enable Telegram in the configuration.

Run a clean eBay search without reading from or writing to the SQLite database:

**Clean search without database reads or writes**
```bash
python fastebaysearch.py ebaysearch.json --clean-search
```

This writes the current eBay search results to a local HTML report and does not send email or Telegram notifications.

**Telegram notifications**

You can adjust the maximum number of Telegram notifications in the configuration. By default a maximum of 25 notifications is allowed. 

Run from the project directory:

```bash
python fastebaysearch.py ebaysearch.json
```

**eBay Browse API budget**

The default eBay Browse API limit is typically 10,000 calls per day. Estimate the configured eBay API usage without making any eBay, token, database, email or Telegram calls:

```bash
python fastebaysearch.py ebaysearch.json --estimate-api-budget
```

The estimate uses the number of configured `ebay_sites`, the generated search queries, and an estimated number of results per query. You can adjust these optional configuration values:

```json
"ebay_daily_api_limit": 10000,
"api_budget_safety_percent": 90,
"estimated_results_per_query": 200
```

The output shows the estimated API calls per run, possible runs per day, and a recommended cron interval.

**Log file**

The script writes logs to `fastebaysearch.log`. When the file exceeds `log_max_size_mb`, it is rotated and up to 5 backup log files are kept. If `log_to_console` is enabled, logs are also printed to the console.

Exit codes:

- `0`: run completed successfully.
- `1`: configuration, database, token, notification, or other runtime error.
- `2`: eBay API rate limit was hit.

## Scheduled runs with cron

Use absolute paths in cron. eBay API credentials are read from `ebaysearch.json`.

Example: run every 30 minutes.

```cron
*/30 * * * * cd /home/user/fastebaysearch && /home/user/fastebaysearch/.venv/bin/python /home/user/fastebaysearch/fastebaysearch.py /home/user/fastebaysearch/ebaysearch.json >> /home/user/fastebaysearch/cron.log 2>&1
```
**Running without Python's virtual environment**

If you want to run without activating the virtual environment, call the virtual environment's Python directly:

```bash
/path/to/fastebaysearch/.venv/bin/python /path/to/fastebaysearch/fastebaysearch.py /path/to/fastebaysearch/ebaysearch.json
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
