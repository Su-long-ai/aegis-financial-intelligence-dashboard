# Aegis Financial Intelligence Dashboard

A local-first financial intelligence research prototype that turns public news feeds into structured market events, stores an auditable reasoning trail in SQLite, and exposes the results through a lightweight dashboard and CLI.

Aegis can run entirely without an LLM using deterministic rules. A DeepSeek/OpenAI-compatible endpoint is optional and is treated as an enrichment layer rather than a hard dependency.

## Why this project exists

The goal is not to predict markets from a single headline. The project explores a more disciplined workflow:

```text
public information
      |
      v
collect + normalize
      |
      v
source credibility + event classification
      |
      v
historical context + causal reasoning
      |
      v
asset-impact hypotheses
      |
      +----> SQLite audit trail
      +----> local dashboard
      +----> optional WeCom notification
```

The repository intentionally distinguishes a **structured research workflow** from a production trading system. It does not claim trading profitability or investment-grade prediction accuracy.

## Highlights

- deterministic event classification with optional LLM enrichment
- source-type and credibility heuristics
- event importance / urgency / region classification
- event-to-asset impact hypotheses and causal-chain records
- SQLite persistence for raw news, events, reasoning chains, cards, reviews and system state
- configurable public RSS/GDELT-style feed ingestion
- market snapshot helpers for contextual review
- local HTTP dashboard without a frontend framework
- optional WeCom digest and urgent-event delivery
- explicit CLI workflows for initialization, ingestion, synchronization, review and export
- offline unit tests for rule logic, CLI parsing and SQLite initialization

## Architecture

The original prototype grew into a single ~2,700-line script. The public portfolio version keeps the same CLI entry point but separates the major responsibilities:

```text
aegis_mvp_deepseek.py      compatibility entry point
        |
        v
     aegis.cli
        |
        v
  aegis.pipeline
   /    |      \
  v     v       v
catalog domain  notifications
        |
        v
      config
```

Repository layout:

```text
aegis/
  __init__.py
  catalog.py          event rules, asset mappings and historical cases
  config.py           repository paths and environment configuration
  domain.py           dataclasses and pure classification helpers
  notifications.py    optional WeCom delivery adapter
  pipeline.py         ingestion, persistence and analysis workflow
  cli.py              argparse command surface

aegis_mvp_deepseek.py  thin backwards-compatible CLI entry point
aegis_dashboard.py     local dashboard server
aegis_feeds.json       starter public-feed configuration
.env.example           optional API / notification configuration
tests/                 deterministic offline tests
```

## Quick start

Python 3.10+ is recommended.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

Initialize the local SQLite database:

```bash
python aegis_mvp_deepseek.py init-db
```

Run the built-in offline demo:

```bash
python aegis_mvp_deepseek.py demo
```

Ingest one item manually:

```bash
python aegis_mvp_deepseek.py ingest \
  --title "Fed signals a more restrictive rate path" \
  --content "Policy guidance caused markets to reprice rates." \
  --source "Reuters"
```

Fetch configured feeds and refresh the research workspace:

```bash
python aegis_mvp_deepseek.py sync-all --config aegis_feeds.json
```

Start the dashboard:

```bash
python aegis_dashboard.py --port 8000
```

Then open `http://localhost:8000`.

## Optional LLM enrichment

Copy the template if you want model-assisted enrichment:

```bash
# Windows PowerShell
Copy-Item .env.example .env

# Linux/macOS
cp .env.example .env
```

Supported variables include:

```text
DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL
AEGIS_MODEL_NAME
WECOM_CORP_ID
WECOM_CORP_SECRET
WECOM_AGENT_ID
WECOM_TOUSER
```

If `DEEPSEEK_API_KEY` is absent, the pipeline falls back to its deterministic rule path. This makes the core classification and database workflow inspectable and testable without network access.

## Testing

Development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run the lightweight offline suite:

```bash
python -m pytest -q
python -m ruff check .
python -m compileall -q aegis aegis_mvp_deepseek.py aegis_dashboard.py tests
```

The tests focus on behavior that can be validated without external services: text normalization, source classification, importance/credibility labels, keyword matching, CLI parsing, deterministic rule classification and repeatable SQLite schema initialization.

External feeds, LLM calls, market endpoints and WeCom delivery are intentionally not treated as offline unit-test guarantees.

## Data and security boundaries

- `.env`, WeCom credentials, SQLite runtime databases and generated outbox files are ignored by Git.
- API credentials are read from environment variables or local ignored configuration, not embedded in source.
- The dashboard is designed for local research use; it is not hardened for public network deployment.
- Public feed availability and usage terms can change; review source terms and rate limits before relying on a feed.

## Scope and limitations

Aegis is a portfolio/research prototype. Its classifications are heuristic and/or model-assisted. News may be delayed, incomplete or wrong; LLM-generated content may be wrong; asset-impact mappings are hypotheses rather than validated trade recommendations.

It is **not investment advice** and should not be used as the sole basis for trading, portfolio allocation or financial decisions.

## Portfolio framing

This project is intended to demonstrate:

- decomposition of an AI application into deterministic and model-assisted layers
- event-driven data pipelines and SQLite workflow design
- rule/LLM fallback architecture
- auditability and review-oriented system design
- practical CLI, notification and dashboard engineering

It should not be presented as a quantitative trading strategy or as evidence of profitable forecasting.

## License

MIT License. See `LICENSE`. External data sources and APIs retain their own terms and licenses.
