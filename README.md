# Aegis Financial Intelligence Dashboard

A local-first research prototype for turning public news feeds into structured market-intelligence events. Aegis ingests RSS/GDELT-style sources, classifies event importance and asset relevance, stores results in SQLite, and serves a lightweight dashboard for review.

The pipeline works without an LLM using deterministic rules, while an optional DeepSeek-compatible API can enrich classification and summaries.

## Highlights

- public-feed ingestion and normalization
- rule-based event, urgency, credibility and asset-impact classification
- optional LLM-assisted enrichment with graceful fallback
- SQLite persistence for events and workflow history
- local HTTP dashboard with no frontend framework required
- configurable feed list through `aegis_feeds.json`
- optional WeCom digest / alert delivery
- CLI workflows for initialization, synchronization and notifications

## Architecture

```text
public feeds
    │
    ▼
fetch + normalize
    │
    ▼
rule classifier ───── optional LLM enrichment
    │
    ▼
structured event records
    │
    ├── SQLite database
    ├── dashboard
    └── optional WeCom notifications
```

## Repository layout

```text
aegis_mvp_deepseek.py   ingestion, classification, storage and CLI workflows
aegis_dashboard.py      local dashboard server
aegis_feeds.json        starter feed configuration
.env.example            optional LLM / WeCom configuration
requirements.txt        Python dependencies
```

Runtime databases, notification credentials, generated outbox files and local secrets are excluded from version control.

## Quick start

Python 3.10+ is recommended.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

Copy the environment template if you want LLM enrichment or WeCom notifications:

```bash
# Windows PowerShell
Copy-Item .env.example .env

# Linux/macOS
cp .env.example .env
```

Initialize the local database:

```bash
python aegis_mvp_deepseek.py init-db
```

Fetch and process configured feeds:

```bash
python aegis_mvp_deepseek.py sync-all --config aegis_feeds.json
```

Start the dashboard:

```bash
python aegis_dashboard.py --port 8000
```

Then open `http://localhost:8000`.

## Configuration

The main optional environment variables are:

```text
DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL
AEGIS_MODEL_NAME
WECOM_CORP_ID
WECOM_CORP_SECRET
WECOM_AGENT_ID
WECOM_TOUSER
```

If `DEEPSEEK_API_KEY` is not set, the pipeline can continue with its rule-based classification path.

For WeCom, you can either use environment variables or generate a starter configuration with the CLI's `wecom-template` command. Never commit the resulting credential file.

## Example workflow

```bash
python aegis_mvp_deepseek.py init-db
python aegis_mvp_deepseek.py sync-all --config aegis_feeds.json
python aegis_dashboard.py --port 8000
```

The included feed configuration is a starting point. Review source availability, usage terms and rate limits before relying on any external endpoint.

## Security and repository hygiene

- `.env`, WeCom credentials, SQLite runtime data and outbox files are ignored.
- API keys are read from environment variables, not hard-coded in source.
- The dashboard is intended for local experimentation; add authentication and deployment hardening before exposing it to a network.

## Scope and limitations

Aegis is a portfolio/research prototype. Its classifications are heuristic and/or model-assisted, public feeds can be delayed or incomplete, and generated summaries can be wrong. It is **not investment advice** and should not be used as the sole basis for trading or financial decisions.

## License

MIT License. See `LICENSE`.
