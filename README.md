# fastebaysearch

`fastebaysearch` is a command-line Python tool for running large numbers of searches across multiple eBay marketplaces.

The tool stores listing history in SQLite and reports new items, fixed-price decreases and buying-option changes by email, Telegram or local HTML files.

<details>
<summary>Notification examples</summary>

**Telegram**

![Telegram](samples/Telegram.png)

**Email**

![Email](samples/email.png)

</details>

`fastebaysearch` is based on the original **ebaysearch** script version 0.4.0 by Kalevi Kolttonen.

Original project: [ebaysearch](https://kolttonen.fi/computers_and_logic/programming/ebaysearch/ebaysearch.html).

## Features

- Concurrent searches across configured eBay marketplaces, covering fixed-price listings and auctions.
- Title-and-description search, continuation-link pagination and automatic splitting of large searches.
- SQLite history and separate delivery queues for new and changed listings.
- Telegram images, local HTML reports, currency conversion and API budget estimates.

## Requirements

- Python 3.11 or newer and Git available on your command line.
- An eBay Developer account and production application credentials (`Client ID` and `Client Secret`) with access to the Browse API. The script uses production eBay endpoints.
- SMTP credentials if email notifications are enabled.
- A Telegram bot token and chat ID if Telegram notifications are enabled. See instructions: https://www.youtube.com/watch?v=SmckRL4n1_8&t=26s

## Installation

Choose a directory where you want to keep the project. These commands create a new checkout and copy its example configuration; edit the copy for your own use.

### Debian / Ubuntu

Install Git and Python tooling, then check the Python version:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
python3 --version
```

The version must be at least 3.11. If your distribution provides an older version, install a supported Python version before continuing.

```bash
git clone https://github.com/Rakki1/fastebaysearch.git
cd fastebaysearch
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp ebaysearch.example.json ebaysearch.json
```

### Windows PowerShell

Install Git and Python if needed, then check that both are available and Python is at least 3.11:

```powershell
git --version
python --version
```

```powershell
git clone https://github.com/Rakki1/fastebaysearch.git
Set-Location fastebaysearch
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item ebaysearch.example.json ebaysearch.json
```

Run commands from the project directory. They use the virtual environment directly; activation is unnecessary.

## First HTML search

The copied example is ready for an HTML-only collector search: **The Last Ninja** and **Impossible Mission** for Commodore 64 on eBay UK, restricted to Video Games (`139973`). It searches titles, covers auctions and fixed-price listings, and leaves email and Telegram disabled. The two game titles are quoted phrases to reduce irrelevant matches.

Open `ebaysearch.json` and replace only these credential placeholders with your own production credentials:

```json
{
  "ebay_client_id": "your-production-client-id",
  "ebay_client_secret": "your-production-client-secret"
}
```

Keep the other fields in the copied configuration. No real credentials are included in the example. No SMTP or Telegram credentials are needed while those channels are disabled; their placeholders can remain. Save valid JSON without comments or trailing commas. You can change the game titles and marketplaces later; available listings vary over time.

First estimate usage without making network calls:

Debian / Ubuntu:

```bash
.venv/bin/python fastebaysearch.py ebaysearch.json --estimate-api-budget
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe fastebaysearch.py ebaysearch.json --estimate-api-budget
```

If the estimate and query validation succeed, preview the current results:

Debian / Ubuntu:

```bash
.venv/bin/python fastebaysearch.py ebaysearch.json --clean-search
echo $?
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe fastebaysearch.py ebaysearch.json --clean-search
$LASTEXITCODE
```

The second command prints the preceding command's exit code. Check it immediately: `0` means success, `1` means an error or incomplete results/report, and `2` means eBay rate limiting. See [Logs and troubleshooting](#logs-and-troubleshooting) for details.

With these settings, open the newest `reports/ebay_report_*.html` in your browser. The log is `fastebaysearch.log` in the project directory. A successful search with zero results creates no HTML file. The first actual search requests an OAuth token and caches it in `oauth_token.json`.

Clean search is a preview: it does not read or write SQLite, mark items as seen, or send email/Telegram notifications. It does write logs, a token cache when needed, and an HTML report when results exist. It always writes HTML regardless of `use_html_report`.

If `html_report_max_per_run` omits results, the report and log show the found/reported counts and the command returns `1`. Clean search has no delivery queue; increase the limit or narrow your search for a complete report.

## Start monitoring and enable notifications

After checking the preview, choose your notification channels below and run normally.

Debian / Ubuntu:

```bash
.venv/bin/python fastebaysearch.py ebaysearch.json
echo $?
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe fastebaysearch.py ebaysearch.json
$LASTEXITCODE
```

On a new database, the first normal run treats all matching items as new. **A previous clean search does not suppress this initial batch.** You can keep the first-run settings for HTML-only monitoring or enable email/Telegram before running. Check the log and output before [scheduling runs](#scheduled-runs-with-cron).

Notification delivery is tracked separately for each channel. An email delivery does not mark a Telegram notification as delivered. Enabling Telegram later can therefore send previously stored items that are still pending for Telegram, including items discovered while Telegram was disabled. Email likewise processes its own pending history. Keep a per-run limit that suits the initial batch.

HTML behaves differently: only items first discovered while HTML is enabled enter its queue. Enabling HTML later does not retrospectively queue existing history. Overflow and failed writes stay queued for later runs.

### Channel settings

| Channel | Configuration |
| --- | --- |
| HTML | `use_html_report: true`; set `html_report_dir` and `html_report_max_per_run` (default **1,000**). Overflow and failed writes stay queued. |
| Email | `use_email: true`; fill in the example's `smtp_*` and `email_*` fields. `email_max_per_run` defaults to **1,000**. SMTP supports optional STARTTLS, not implicit TLS. |
| Telegram | `use_telegram: true`; set `telegram_token`, `telegram_chat_id` and `telegram_max_per_run`: **25** in the example, **1,000** if omitted. |

Email requires non-empty SMTP login/password and identity fields even when authentication is disabled. Keep unconfigured channels disabled.

Telegram's `telegram_send_mode` accepts `auto` or `photo` (image when available, text fallback), `text`, or `photo_only` (skips items without images; no fallback). `telegram_disable_web_preview` controls text-link previews. Status warnings and fallback requests are additional to the item limit. Failed item deliveries become eligible again after six hours, up to three claimed attempts; exhausted items remain stored but are not automatically retried.

New-listing Telegram notifications include their sending date and time in the server's local time after `New listing!`, for example `New listing! 15th November 2026, 14:45`. Month names are always English, with ordinal days such as `1st`, `2nd`, `3rd`, `11th`, `21st` and `31st`. Listing notifications are sent directly, without a separate search-report header or item-count message.

### Changed listings and status alerts

Seen listings are refreshed on every observation. A separate durable event queue reports fixed purchase-price decreases and changes between AUCTION and FIXED_PRICE. Bid changes, title edits and currency-conversion changes do not trigger alerts. Price comparisons require the same currency and source marketplace. When merging duplicate items, responses with a recognized listing marketplace replace results with missing origin information, even if found through another marketplace. Among results with recognized origins, responses from the listing's own marketplace take priority; otherwise the first configured marketplace wins. Available location-country information is merged separately, preserving this price/source preference.

### Item location and links

Item links use the physical location country (`itemLocation.country`), independently of the marketplace searched or where the listing was published. For example, a German item found in a UK search links to `ebay.de`, even if its listing marketplace is `EBAY_GB`. Email/HTML reports show the country code in a separate **Location** column; Telegram shows **Location** when known. **Ebay Site** still identifies the listing marketplace.

The country is taken from search results or from a full-description response already fetched for validation; this feature makes no additional API calls. Duplicate results share available country information. On later observations, a missing country does not erase the stored country or revert its link. If the location is missing or has no supported eBay site (for example, Finland), links fall back to the listing marketplace and then to the API item URL. No country is inferred from currency or seller identity.

The next normal run automatically adds a `location_country` column to existing databases, preserving history and delivery markers. Existing rows initially have no country until observed again; pending initial notifications use refreshed data. Location/link changes alone do not create new notification events. Old generated reports and stored change-event snapshots are not rewritten. No JSON configuration change is required.

The first structured observation of an older listing establishes a baseline without a change alert. A channel whose initial listing notification is pending receives the latest details in that notification. New and changed listings share each channel's per-run limit, with the oldest pending entries selected first.

Every partial or failed search run sends one summary per enabled channel even with no pending listings. HTML writes a status-only report when necessary. Successful empty runs remain silent. Telegram sends partial-search summaries as separate status messages and truncates long diagnostic details with a pointer to the log. Channel failures do not block other channels.

## Run modes

Listing searches and budget estimates validate the configuration, including credentials for enabled channels. Disable email/Telegram for a preview when you have not configured their credentials. Category and quota lookups need only eBay credentials, `ebay_sites`, and the optional token-cache path.

| Mode | API calls | SQLite | Output |
| --- | --- | --- | --- |
| Normal run (no flag) | Developer Analytics quota check, eBay searches when allowed, OAuth as needed, exchange rates on cache miss | Reads/writes history, seen items, quota cooldown and delivery state | Enabled HTML, email and Telegram channels, including pending deliveries |
| `--clean-search` | eBay searches, OAuth as needed, live exchange rates | No access | Local HTML preview only; no email/Telegram or delivery queue |
| `--estimate-api-budget` | None | No access | Console estimate only; no token cache, log file or notifications |
| `--find-categories "keywords"` | Taxonomy lookups, OAuth as needed | No access | Category names, paths and a configuration example on the console; no listings, reports or notifications |
| `--api-quota` | Developer Analytics, OAuth as needed | No access | Browse quota, usage, remaining calls and reset time on the console; no listings or notifications |

The four command-line modes cannot be combined.

## Search settings

Settings are top-level JSON fields unless shown with a dotted path. See [ebaysearch.example.json](ebaysearch.example.json) for the full configuration.

| Setting | Purpose / default |
| --- | --- |
| `ebay_sites` | Marketplaces such as `EBAY_GB`, `EBAY_US` and `EBAY_DE`. |
| `ebay_buying_options` | `["FIXED_PRICE", "AUCTION"]` by default; either value alone restricts the listing format. |
| `ebay_search_in_description` | `true`: search titles and descriptions. Set `false` for title search. The example uses `false`; defaults to `true` when omitted. |
| `ebay_filter_recommendations` | Default `false`. With description search enabled, check full descriptions for matches contributed only by recognized recommendation blocks. |
| `ebay_validate_search_terms` | Default `false`. Require every positive search condition locally before accepting a result; skip unverifiable results and report the run as partial. |
| `ebay_description_cache_ttl_hours` | Cache verified descriptions and decisions across normal runs for **24** hours; `0` disables this persistent cache. |
| `ebay_check_api_quota` | Default `true`. Check the actual Browse quota, pause normal searches until reset when exhausted, and send one Telegram warning per quota episode. |
| `ebay_search_keywords.base_terms` | Each expression generates a query on every configured marketplace. |
| `ebay_search_keywords.required_terms` | At least one alternative must match. Use `[]` to omit the restriction. |
| `exclude_terms` | Exclusions appended to every query. |
| `ebay_category_ids` | Search only these category IDs per marketplace; default `{}` (all categories). |
| `ebay_exclude_category_ids` | Excluded category IDs per marketplace; default `{}` (no exclusions). |
| `ebay_max_search_calls_per_run` | Shared Browse request cap, including retries: **500** per run. |
| `api_concurrency` | Maximum concurrent Browse requests: **8**. |
| `exchange_rate_cache_ttl_hours` | Exchange-rate cache lifetime: **24** hours; `0` disables caching. |

### Category filters

Find category IDs directly from the command line using your configured marketplaces:

Linux:

```bash
.venv/bin/python fastebaysearch.py ebaysearch.json --find-categories "commodore 64"
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe fastebaysearch.py ebaysearch.json --find-categories "commodore 64"
```

Example output (suggestions vary by marketplace and over time):

```text
EBAY_GB
  Video Games & Consoles [1249] > Video Games [139973]
```

The command prints eBay's ranked suggestions and a JSON example containing the first suggestion for each successful marketplace. Choose the IDs before copying them into your configuration; you can also use a parent ID from the paths, such as `1249`. It does not change your configuration, access SQLite, fetch listings, or send notifications. It may update the OAuth token cache. Existing category filters and listing keywords do not restrict the lookup. Suggestions are not a complete category listing.

Normally this uses two Taxonomy calls per marketplace, plus OAuth if needed; transient errors can add retries. These are separate from Browse calls and the Browse budget estimate. Exit codes are `0` for success (including no suggestions), `1` for an error, and `2` for rate limiting. Errors appear on the console; successful marketplaces are retained in partial output. Missing marketplaces in a copied JSON example remain unrestricted.

To restrict searches to video games, add the following top-level setting:

```json
{
  "ebay_category_ids": {
    "EBAY_GB": ["139973"],
    "EBAY_US": ["139973"],
    "EBAY_DE": ["139973"],
    "EBAY_AU": ["139973"],
    "EBAY_FR": ["139973"],
    "EBAY_IT": ["139973"]
  }
}
```

`139973` is Video Games on these six marketplaces; it includes modern games too, so keep your C64/game keywords. Optionally add `"4193"` (Other Vintage Computing) to find listings outside Video Games, at the cost of broader results. Misclassified games outside the selected categories will be missed. The starter configuration uses only `EBAY_GB` and `139973`.

eBay's [category_ids parameter](https://developer.ebay.com/develop/api/buy/browse_api/item/search) accepts one category per request. Multiple configured IDs are searched separately and duplicate item IDs are merged before delivery. With 13 queries and six marketplaces, one category requires at least 78 Browse calls; two require 156. Pagination and retries add calls, within the shared per-run cap. The budget estimate includes these category searches. Logs identify each category; an invalid-category API warning fails that search instead of accepting unrestricted results. Missing marketplace entries or `[]` mean all categories.

For broader searches, you can optionally exclude vehicle parts with the following setting. The starter configuration does not need these exclusions because it searches only Video Games:

```json
{
  "ebay_exclude_category_ids": {
    "EBAY_GB": ["131090"],
    "EBAY_US": ["6028"],
    "EBAY_DE": ["131090"],
    "EBAY_AU": ["131090"],
    "EBAY_FR": ["131090"],
    "EBAY_IT": ["131090"]
  }
}
```

To find an ID, search on the relevant country's eBay site, select a category, and check the address bar. For example, this link searches for Commodore 64 in the UK Video Games category:

[https://www.ebay.co.uk/sch/i.html?_nkw=commodore+64&_sacat=139973](https://www.ebay.co.uk/sch/i.html?_nkw=commodore+64&_sacat=139973)

The number after `_sacat=` is the category ID: **139973**. The `.co.uk` site corresponds to **EBAY_GB**. To search only this category, add the following top-level setting:

```json
{
  "ebay_category_ids": {"EBAY_GB": ["139973"]}
}
```

Some URLs put the ID in the path instead: in `https://www.ebay.co.uk/sch/139973/i.html`, it is the number between `/sch/` and `/i.html`. A `bn_...` identifier is not a category ID; if no numeric category ID is visible, use eBay's [category information / Taxonomy API](https://developer.ebay.com/develop/guides/buy/marketplace-metadata-guide).

IDs can differ between marketplaces. Use `ebay_category_ids` to include categories or `ebay_exclude_category_ids` to exclude them. For example, `"EBAY_GB": ["131090", "15032"]` inside `ebay_exclude_category_ids` excludes Vehicle Parts & Accessories and Mobile Phones & Communication. Remove a marketplace entry or use `[]` to disable that setting's restriction for the marketplace.

The [excludeCategoryIds filter](https://developer.ebay.com/api-docs/buy/static/ref-buy-browse-filters.html) applies to all pages and time-range searches without extending the keyword query. Check the results with `--clean-search`; miscategorised listings can still appear. Existing database records and queued notifications are not removed. Keep personal configuration and generated HTML reports out of Git.

### Search expressions and coverage

Search terms separated by spaces retain eBay's AND semantics. For example, base term `camera` and required terms `["canon", "nikon"]` produce `("canon", "nikon") camera`. Expressions such as `(Slapshot, "Slap Shot")` and `Kong -hong` are preserved. Exclusions are appended to every query; an optional leading minus on a configured exclusion is normalized.

Every complete query, including all exclusions, must fit within eBay's 100-character limit. An overlong query is rejected with its base term, full text and length before logging, database access or API calls. This applies to normal runs, clean searches and budget estimates. Shorten the terms or split the configuration; no exclusions are silently discarded.

Results are ordered newest first and paginated using the API's continuation link. Searches reaching 10,000 results are split into listing-start time ranges and duplicate IDs are merged. All searches share the **500-call per-run cap**, including retries; OAuth and exchange-rate requests are separate. A budget stop or unsplittable interval is reported as `truncated`, with received results retained. Each run starts afresh rather than resuming unfinished ranges; complete coverage is not guaranteed for changing inventory or budget-limited searches.

Description search can find items omitted from listing titles but may return more irrelevant matches. `required_terms` and exclusions still apply. Auction-only listings show `Current bid`; missing prices display `Price unavailable`.

### Filtering recommendation matches

To keep description search while removing known recommendation-only matches, enable both settings:

```json
{
  "ebay_search_in_description": true,
  "ebay_filter_recommendations": true
}
```

Items whose titles satisfy all positive search terms need no additional call. Other items are checked using the full HTML description from Browse `getItem`, shared per REST item ID and search marketplace during the run and cached across normal runs as described below. The first supported layout is `div.wi-recommendations` with product cards linking to other eBay items. The checker removes only recognized blocks, preserving description text before and after them. It rejects a match only when the positive terms match the original text but fail after removing recommendations. It supports ordinary AND terms, quoted phrases, comma-separated OR groups and the configured required-term alternatives; negative terms and exclusions remain eBay's responsibility.

With `ebay_validate_search_terms` disabled, missing descriptions, unfamiliar layouts or query syntax, malformed HTML and failed requests retain the match and log the reason. The check is conservative rather than a guarantee against all irrelevant results. No supported block also counts as uncertain. The run summary reports accepted, rejected and uncertain checks plus description-call attempts; these counts are before final item-ID deduplication. Ordinary verification failures do not change a successful search's exit code; HTTP 429 still returns `2` and stops new API requests.

Filtering happens before saving new results, for both normal runs and `--clean-search`. Existing database records and notification queues are not rechecked or removed. For a first trial, use a separate configuration with one marketplace and one base term, disable email/Telegram and run `--clean-search`. Its local HTML report does not mark items seen or consume later notifications. Set `ebay_filter_recommendations` back to `false` to disable the feature.

### Requiring all positive search terms

Enable local validation to reject eBay results that do not satisfy the search terms, including descriptions without recommendation blocks:

```json
{
  "ebay_search_in_description": true,
  "ebay_filter_recommendations": true,
  "ebay_validate_search_terms": true
}
```

For base term `Battle Through Time` and required terms `["commodore", "c64", "c-64"]`, all three words **battle AND through AND time**, plus at least one platform alternative, must appear. Ordinary words may occur in any order and across the title and description. Quoted phrases must occur consecutively within a single field; comma-separated groups such as `(Slapshot, "Slap Shot")` remain alternatives. Matching ignores case, normalizes Unicode and whitespace, and uses word boundaries without synonyms or fuzzy matching. Negative terms and exclusions remain eBay's responsibility. Unsupported positive query syntax is a configuration error before API calls.

A title satisfying every condition is accepted without a description call. Otherwise, with description search enabled, validation uses the complete description after removing scripts, styles and, if `ebay_filter_recommendations` is enabled, verified recommendation blocks. Missing positive conditions reject the result even if no recommendation block exists. With description search disabled, only the title is checked and no description is fetched. Turning recommendation filtering off does not turn search-term validation off.

Unverifiable results are **deferred for this run**, not accepted: this includes failed or missing full descriptions, malformed HTML, ambiguous recognized recommendation blocks, and an exhausted Browse call budget. They are not saved as seen or queued for notification. The run returns `1` (`2` on HTTP 429); accepted results can still be delivered. If a later search returns the item, it can be checked again. There is no durable retry queue or guarantee of later rediscovery. A complete description containing only an unknown recommendation layout may still match; only the documented layout is removed.

The log and report show `Search term check: accepted=..., rejected=..., deferred=..., description_calls=...`. Counts are per search result before final item-ID deduplication; rejection logs name the missing word or alternative groups. A result accepted by one search can survive another search's rejection. Existing database records and pending notifications are unchanged and may still produce old false positives. Set `ebay_validate_search_terms` to `false` to restore the previous behavior.

Temporary API failures receive up to two retries; authentication errors allow one shared token refresh. Rate limiting stops outstanding searches. Results already received are retained, and incomplete runs are identified in logs and notifications.

### Caching description checks

Normal runs store confidently accepted and rejected checks, plus their full descriptions, in the existing SQLite database. The cache is separate from seen-item history: a rejected item is never marked seen. Its default lifetime is 24 hours; no JSON change is needed. Set `"ebay_description_cache_ttl_hours": 0` to disable persistence, or use another nonnegative number of hours. Cache tables are added automatically.

Fresh search summaries are still fetched on every run, keeping prices, bids, buying options and available location information current. A changed title, category, seller, condition, listing date or available revision field bypasses the cached description. Price and bid changes alone do not trigger another description call. Keys include the application, search marketplace and full REST item ID, including variations. Changed search terms or validation settings recheck the stored HTML under the new rules without a Browse call.

Browse search summaries do not currently expose a description revision or last-modified timestamp. A description-only edit can therefore remain unnoticed until the cache expires. Hits never extend the original fetch time; the next matching search after expiry fetches and checks the full description again. Failed requests and uncertain checks are retried on subsequent searches rather than saved as confident decisions. Cache read/write errors fall back to ordinary verification.

The first run fills the cache and can cost as much as before. If its per-run cap is reached, successfully checked entries remain reusable and subsequent runs can check the remaining items. `--clean-search` always bypasses persistence and fetches fresh descriptions without accessing SQLite. A run using a new temporary database also starts with an empty cache.

The log adds `Description cache: hits=..., misses=..., entries=..., validation_hits=..., ttl_hours=...`. Hits and misses count distinct marketplace/REST-item pairs; validation hits count individual search results. `description_calls` continues to report actual API attempts, including retries.

## API budget and scheduling

The offline budget calculation defaults to **5,000 Browse calls per day**. Check your application's actual quota using `--api-quota` and set `ebay_daily_api_limit` accordingly before scheduling. Changing this estimate field does not change eBay's quota or override the runtime Analytics check. A larger value explicitly configured in an existing JSON file is preserved.

These optional fields control the estimate:

```json
{
  "ebay_daily_api_limit": 5000,
  "api_budget_safety_percent": 90,
  "estimated_results_per_query": 200
}
```

Run `--estimate-api-budget` using the commands in [First HTML search](#first-html-search). The estimate uses the number of marketplaces, generated queries and expected result pages. Each marketplace/query combination makes a separate request for each page; a run is not one API call for all marketplaces.

When recommendation filtering or search-term validation is enabled, description calls and their retries share the same per-run Browse cap as search calls. They are not included in the offline page-based estimate or scheduling interval. The measured budget in Telegram quota warnings accounts for them and for the persistent cache. If no call budget remains for verification, validation defers unchecked matches; recommendation filtering alone retains them as uncertain. Valid cached checks remain usable even when the call cap is reached.

The output shows estimated calls per run, possible runs per day and a recommended interval. It also shows a retry allowance at the estimated page count and the configured per-run Browse call cap. OAuth calls are separate (0–2 per run). The recommended interval uses the normal estimate; allow headroom for retries, other applications using the same quota and higher result counts. Automatic time-range splitting can increase usage beyond the estimate. The 500-call cap is per run, not a daily quota controller.

### Live quota checks and Telegram warnings

Read the actual application quota without searching for items:

```bash
.venv/bin/python fastebaysearch.py ebaysearch.json --api-quota
```

On Windows, use `.\.venv\Scripts\python.exe` in place of `.venv/bin/python`. The command returns `0` when quota is available, `2` when exhausted, and `1` when lookup fails. It does not use SQLite or send notifications. It may refresh the OAuth token cache. Reset times are shown in UTC and, if different, the machine's local time with its UTC offset.

Normal runs query the [Developer Analytics API](https://www.developer.ebay.com/develop/api/buy/developer_analytics_api) before Browse searches and again after a Browse HTTP 429. The `buy.browse` resource covers this program's search and single-item description calls. The separate `buy.browse.item.bulk` quota belongs to `getItems` and is not combined with it. Analytics checks have a separate API quota and do not consume `ebay_max_search_calls_per_run`.

When no Browse quota remains, the database stores the pause and the API's reset time. Scheduled runs skip Browse calls and recheck Analytics after one hour or at the reported reset, whichever comes first. Searches resume at the first eligible scheduled run with available quota, even if this is before the previously reported reset: Analytics counters can lag behind a reset. A stored pause from an older release that extends more than one hour into the future is rechecked on the next run. A Telegram warning includes the available limit/usage/reset information and the next recheck time. Successful warning delivery is remembered across restarts and subsequent runs using the same database/application. A failed Telegram send remains eligible for retry; concurrent runs claim the warning before sending it. A new quota window or a new outage after a successful recovery permits a new warning. Telegram must be enabled and configured; no extra JSON setting is required for the default behavior.

If Analytics fails, ordinary searches can still proceed. A Browse 429 without a trustworthy reset time starts a 15-minute cooldown and sends a single warning explaining that the reset is unknown. The next eligible scheduled run checks again. A positive Analytics result alone does not clear an existing unknown-reset warning episode: Browse must actually work again. Repeated generic Telegram `PARTIAL SEARCH` status messages for the same quota episode are suppressed; already queued listing notifications can still be delivered. Other notification channels retain their existing partial-run reporting behavior.

The normal run returns `2` while quota-paused, and logs keep recording the pause. These checks observe a shared application quota, not reserve capacity against other processes using the same credentials. Clean-search previews remain database-free and do not use the persistent cooldown or send Telegram warnings. Set `ebay_check_api_quota` to `false` to disable automatic checks and restore ordinary 429 handling; `--api-quota` remains available.

The same Telegram limit warning includes a scheduling budget for the current search configuration. Each complete normal search records its actual Browse call attempts, including result pages, description requests, authentication replays and retries. The budget uses the maximum of the last five complete measurements within seven days for the same application and search configuration. Changes to keywords, marketplaces, categories, buying options, description/validation settings or the description-cache lifetime require fresh measurements. Incomplete or quota-capped searches are never treated as full-cost samples; changing only the per-run cap or notification settings preserves the profile.

The warning shows measured calls per full run, theoretical runs per 24 hours, and a recommended interval using `api_budget_safety_percent` (default 90). With persistence enabled, it separates search calls and uncacheable checks needed every run from a reserve for refreshing reusable descriptions once per cache lifetime. Retry allowances are included. For each Analytics rate window it conservatively reserves at least one whole refresh, including windows shorter than the cache lifetime. Cold cache filling is shown separately and a cap below that cost is flagged.

For example, with 78 search calls, 463 reusable descriptions, a 24-hour lifetime and an unchanged inventory, 48 runs/day need about `48 * 78 + 463 = 4,207` Browse calls. This fits a 90% budget of a 5,000-call daily quota. Filling or fully refreshing the cache can require 541 calls in a single run, above the default 500-call cap. With persistence disabled, 541 calls on every run still allows only 9 theoretical runs, or 8 with the 90% safety budget (180 minutes apart). The program does not change cron or enforce the recommendation: new or changed listings, retries and other programs using the same quota can increase the cost.

Before the first complete measurement for a profile, the message reports the minimum search-call count and says that scheduling frequency is not yet known. It does not present the search-page minimum as a full-run estimate when description checks add calls. One complete normal run with a sufficient per-run cap is needed to calibrate it; a temporary-database email run stores its measurement only in that temporary database, and clean-search previews do not record these measurements. New budget data appears in the next quota warning without sending duplicate warnings for an episode already acknowledged.

### Scheduled runs with cron

After a successful manual run, use absolute paths and the same working directory for cron. Replace every `/home/user/fastebaysearch` with your checkout's absolute path. The following is a 30-minute example, not a universal interval recommendation:

```cron
*/30 * * * * cd /home/user/fastebaysearch && /home/user/fastebaysearch/.venv/bin/python /home/user/fastebaysearch/fastebaysearch.py /home/user/fastebaysearch/ebaysearch.json >> /home/user/fastebaysearch/cron.log 2>&1
```

Choose an interval that fits your API budget and leaves enough time for a run to finish. Avoid overlapping scheduled runs; SQLite delivery tracking does not make every notification channel immune to duplicate delivery. This command uses the virtual environment without activating it.

## Logs and troubleshooting

The script writes `fastebaysearch.log` in the project directory. It rotates at `log_max_size_mb` and retains up to five backups. With `log_to_console: true`, runtime messages also appear in the terminal. Configuration errors are printed before the log is opened.

Exit codes:

- `0`: all searches completed and attempted notifications/reports succeeded. Items safely queued beyond the normal HTML limit do not make the run fail.
- `1`: configuration, database, token, notification or other runtime error; also an incomplete search or truncated clean-search report.
- `2`: eBay rate limiting was encountered or Developer Analytics reported an exhausted quota; takes precedence over notification/report failures. Results received before the search cancellation are still processed.

A failed or partial search is not equivalent to a successful search with zero results. Check the per-query status, fetched counts and notification errors in the log.

| Symptom | What to check |
| --- | --- |
| Configuration error | Valid JSON, required fields for enabled channels, allowed paths, and the full query's 100-character limit |
| No HTML report | Whether the search succeeded with zero results; in normal mode, whether HTML is enabled and its queue has pending items |
| `PARTIAL SEARCH` or a call-budget stop | Affected queries and API errors in the log; narrow broad searches |
| Clean report contains fewer items than were found | `html_report_max_per_run`; clean search cannot queue the remainder |
| HTTP 401 | Production eBay credentials and application access; the run already attempts one shared token refresh |
| HTTP 429 / exit code 2 | Actual application quota and request frequency before the next run |
| Email or Telegram fails | Channel credentials, settings and logs; the other enabled channels are still attempted |

## Updating

1. Stop scheduled runs and back up your configuration and SQLite database. Use SQLite's backup facility, or stop all database users before copying the database and any WAL files.
2. Review the [release notes](https://github.com/Rakki1/fastebaysearch/releases), then update the code and dependencies using the commands below.
3. Compare your settings with `ebaysearch.example.json`, keeping your credentials and search terms. Database schema updates run automatically and preserve existing history.
4. Run once manually, check the log and exit code, then resume scheduling. Broader search defaults can increase the first result batch.

Replace the project path below with your installation directory. Run these commands after stopping scheduled runs and taking the backup.

### Linux

```bash
cd /path/to/fastebaysearch
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
```

### Windows PowerShell

```powershell
Set-Location C:\path\to\fastebaysearch
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If a command fails, resolve the error before continuing. Keep your existing configuration file; do not overwrite it with the example.

## Local credentials

Copy `ebaysearch.example.json` to create your own configuration; keep real credentials out of Git. The default local configuration and token cache names are ignored. `konfiguraatio.json` is also an ignored personal filename, not a configuration supplied by the repository. Add custom credential or cache filenames to `.gitignore` before committing.

Tests use a credential-free collector-search fixture. Do not replace it with a live configuration.

## Tests

Install development dependencies and run the test suite from the project directory.

Debian / Ubuntu:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```
