# Aegis Financial Intelligence Dashboard

A local-first prototype that collects public news feeds, classifies market-relevant events, records their potential asset impact, and presents the results in a lightweight dashboard.

## Features

- RSS and GDELT feed collection
- Rule-based event, urgency, credibility, and asset-impact classification
- Optional DeepSeek-assisted classification
- SQLite persistence and a built-in dashboard
- Optional WeCom notifications (kept out of this repository)

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python aegis_mvp_deepseek.py init-db
python aegis_mvp_deepseek.py sync-all --config aegis_feeds.json
python aegis_dashboard.py --port 8000
```

Open `http://localhost:8000` after starting the dashboard.

## Data and security

The SQLite database, notification configuration, generated output, and `.env` are intentionally ignored. The provided feed list contains public endpoints; review their terms before production use. This project is for research and information display, not investment advice.

