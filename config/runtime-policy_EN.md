# Runtime Data and Sensitive Information Handling Policy

[简体中文](runtime-policy.md) | English

This document defines how NiuOne handles runtime data, model keys, and private local files. Its purpose is to allow real data to remain inside the project directory while ensuring that content uploaded to a public repository contains no user data or sensitive information.

## Directory Boundaries

Source directory:

```text
/path/to/NiuOne
```

Private runtime directory:

```text
.local-data/
├── dashboard.env
├── .venv/
├── runtime/
└── backups/
```

`.local-data/`, `dashboard.env`, databases, local credentials, logs, and backup files are all ignored by `.gitignore`.

## Content That Must Not Be Committed or Shared Externally

| Path | Description |
|---|---|
| `.local-data/dashboard.env` | Local environment variables, paths, and any model keys or administrator passwords |
| `.local-data/.venv/` | Local Python virtual environment |
| `.local-data/runtime/dashboard_admin_token.txt` | Bootstrap administrator key used when `DASHBOARD_ADMIN_PASSWORD` is not configured |
| `.local-data/runtime/dashboard_users.db` | Local users and authentication data |
| `.local-data/runtime/push_history.db` | Message history |
| `.local-data/runtime/news/realtime_news_latest.json` | Bounded rolling NewsNow history and non-sensitive source status; successful refreshes merge by ID under total/important limits, while upstream failures use it only as a read-only fallback and never replace real trading records |
| `.local-data/runtime/niuniu.db` | Practice trades, account data, complete observed opportunity sets, five-stage holding paths/exit stages, and durable decision evidence |
| `.local-data/runtime/cron/output/niuone_forward_evaluation.json` | NiuOne strict-forward aggregates, five-stage opportunity/sizing funnel, holding paths/stage transitions/exit stages, rejection categories, trade-level and entry-date-by-industry cluster-robust win-rate intervals, daily portfolio return/drawdown, performance gate, coverage diagnostics, and shadow groups |
| `.local-data/runtime/cron/state/niuone_forward_protocol.json` | Frozen code/non-secret runtime-configuration fingerprint and code-free starting-account boundary for the NiuOne strict-forward cohort |
| `.local-data/runtime/cron/state/niuone_cron_scheduler.json` | Bounded Cron run keys and daily task outcomes used by strict-forward evaluation |
| `.local-data/runtime/cron/state/b1_schedule_state.json` | Bounded terminal scan/decision outcomes for configured Practice slots |
| `.local-data/runtime/market_data/tencent_daily_klines.sqlite3` | Full-market daily-K-line cache populated before the open and incrementally filled by intraday scans |
| `.local-data/runtime/backtesting/` | Server-side progress/results for each strategy's current backtest, short-lived subprocess exchange files, and compressed selection replay tapes addressed by protocol/data/classification content; this is not a general historical daily-K cache for other modules |
| `.local-data/runtime/config.yaml` | Model provider, model, and model-key configuration |
| `.local-data/runtime/cron/state/` | Scheduled-task and catch-up-run state |
| `.local-data/runtime/cron/output/` | Practice-trading candidate-scan cache, simulated-account state, and other non-message runtime caches |
| `.local-data/runtime/cron/output/multi_strategy_history/` | Bounded full-scan snapshots for investigation; retains only the latest archive date and at most 12 runs for that date |
| `.local-data/runtime/cron/output/b1_history/` | Retired duplicate B1 archive; the next successful scan removes only old JSON files with the standard date/timestamp layout and preserves unknown files |
| `.local-data/runtime/logs/` | Service and task logs |
| `.local-data/backups/` | Deployment backups, which may contain older configuration |

The bundled Compose NewsNow database and cache live in the separate `newsnow-data` Docker volume, not in the repository or `niuone-data`. Treat it as private runtime data: back it up separately for container deployments and never upload its contents. `docker compose down` preserves it, while `docker compose down -v` removes it together with NiuOne's main data volume and therefore requires a confirmed backup first.

The Dashboard incremental API may return only content inside `.local-data/runtime/public-data/` that was generated through the field allow-lists in `public_projection.py`. Never configure its parent directory, databases, or `cron/output/` as a static-site root. CDN synchronisation must be limited precisely to `objects/`, `manifests/`, and `latest.json`, and sanitisation tests must be reviewed after every schema change.

Do not copy any of the content above into issues, pull requests, the README, documentation examples, or chat contexts. When troubleshooting, provide only sanitized error types, timestamps, and strictly necessary fields.

## Model Keys

Recommended usage:

| Purpose | Recommended model | Settings |
|---|---|---|
| Daily U.S. institutional-rating report | Financial Modeling Prep structured data and local deterministic rules | `FMP_API_BASE_URL`, `FMP_API_KEY`, `FMP_RATING_MAX_RESULTS` |
| Trading decisions, prompt refinement, news judgment, and A-share/overnight U.S. summaries | One shared OpenAI-compatible model | `DASHBOARD_DECISION_BASE_URL`, `DASHBOARD_DECISION_API_KEY`, `DASHBOARD_DECISION_MODEL`, `DASHBOARD_DECISION_STREAM_MODE`, `DASHBOARD_DECISION_REASONING_EFFORT` |
| News prechecks for A-share candidates and dragon-tiger limit-up-streak/consecutive-listing stocks | Tonghuashun iWencai OpenAPI | `IWENCAI_NEWS_PRECHECK_ENABLED` and `IWENCAI_*` |
| Comprehensive decision reference | Local aggregation; no additional model required | `DASHBOARD_DECISION_INTELLIGENCE_ENABLED`, `DASHBOARD_DECISION_INTELLIGENCE_TTL_SECONDS`, `DASHBOARD_DECISION_INTELLIGENCE_MAX_ITEMS` |

The daily U.S. institutional-rating report is controlled by the `DASHBOARD_US_FEATURES_ENABLED` master switch. When it is disabled, the settings page hides the related configuration and skips the scheduled U.S. rating task.

The comprehensive decision reference reads local market-data caches, market-message history, and simulated-account state, then writes a compressed summary to the decision log. It introduces no additional model keys, but the log may contain candidate-news summaries and must still be reviewed under this runtime-data policy before any public troubleshooting disclosure.

Model and FMP keys may be stored only in `.local-data/dashboard.env`, `.local-data/runtime/config.yaml`, or controlled system environment variables. Before committing, confirm that no new `.env`, `*.key`, `*.token`, `*.secret`, database, or backup file has been added.

The iWencai data source uses `IWENCAI_API_KEY`, which is subject to the same restriction and may only be stored in `.local-data/dashboard.env` or a controlled system environment variable.
`IWENCAI_ENABLED` is disabled by default. iWencai data is a research snapshot and supplemental market source; incomplete or cached responses must never overwrite account, fill, or real trading records.
The dragon-tiger job refreshes at 18:00 China time on A-share trading days by default. Only the most recent non-empty successful response is retained and atomically replaced by the next successful query; failures and empty responses preserve the last valid data. Dated archives created by earlier versions are removed after the next successful refresh. If top-five buy/sell seat details fail independently, valid institution, brokerage, and other seat rows in the current snapshot are preserved only when the query date is unchanged.
Consecutive listing may be confirmed only from successful rolling snapshots on adjacent A-share trading days; a missing intermediate snapshot resets the streak. `IWENCAI_NEWS_PRECHECK_ENABLED=1` combines the official `announcement-search`, `news-search`, and `hithink-event-query` Skills for evidence retrieval. Announcement, news, and dated event fields are limited to the latest three calendar dates, identity-checked, and deduplicated. When evidence exists, the configured `DASHBOARD_DECISION_*` trading-decision model must classify it as positive, negative, or neutral. An empty evidence set is neutral without a model call. Missing configuration, timeout, or invalid model output makes the judgment unavailable and never falls back to keyword matching. No Xueqiu/X source is used, and quotes or fund flows never count as message evidence. Failures must never block or clear the main dragon-tiger data. Legacy `DASHBOARD_NEWS_*` configuration is no longer read.

Overseas deployments may set `DASHBOARD_CN_DATA_PROXY_URL=socks5h://host:port` for Tencent, Eastmoney, Sina, and iWencai traffic only. Credentials, query parameters, and paths are rejected. Once configured, a proxy failure follows bounded timeouts, retries, and cache fallback and never silently bypasses the proxy. Docker Compose translates a loopback proxy host to the current gateway discovered from the container route table; the host firewall must allow only that Compose subnet to reach the proxy port. Model, notification, FMP, and NewsNow traffic does not use this setting.
Current-day dragon-tiger data remains public, as does the most recent rolling snapshot until the next successful query replaces it. Earlier dates require a valid administrator session. An empty current-day live query must fall back to the most recent successful snapshot instead of replacing the page with an empty state before new data is published. No non-current-date response may use public or CDN caching, so the replaced date becomes protected immediately after a refresh.

## Local Copies and Testing

Do not experiment directly against the real `.local-data/runtime/` directory. Use a temporary runtime directory for testing:

```bash
DASHBOARD_HOME=/tmp/niuone-smoke DASHBOARD_PORT=8877 ./scripts/run_standalone.sh
```

Before committing, run:

```bash
./scripts/validate.sh
git status --ignored --short
```

`.local-data/` should appear as ignored and must not appear in staged files.

## Releases and Backups

The local deployment script backs up the current `app/`, environment file, and startup scripts to:

```text
.local-data/backups/
```

The backup directory is also private data and must not be committed or shared externally. For rollback, prefer restoring `app/` from a backup or use `git revert` for a non-destructive commit rollback.

## Responding to Suspected Exposure

If a model key, local credential, or database is accidentally published:

1. Immediately revoke or rotate the affected key or credential.
2. Remove the exposed content from code and documentation.
3. Review `git status --ignored --short` and recent commits.
4. If no administrator password is configured, rebuild `.local-data/runtime/dashboard_admin_token.txt` when necessary; rebuild related databases as needed.
5. For sensitive content already pushed to a remote service, follow that service's incident-response process to remove it from history.
