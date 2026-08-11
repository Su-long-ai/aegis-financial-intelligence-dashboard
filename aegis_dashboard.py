from __future__ import annotations

import argparse
import html
import json
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "aegis_alpha_mvp.db"


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def query_all(sql: str, params: tuple[Any, ...] = (), db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def stats(db_path: Path = DB_PATH) -> dict[str, Any]:
    with connect(db_path) as conn:
        news_count = conn.execute("SELECT COUNT(*) AS c FROM news_raw").fetchone()["c"]
        event_count = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        card_count = conn.execute("SELECT COUNT(*) AS c FROM push_cards").fetchone()["c"]
        review_count = conn.execute("SELECT COUNT(*) AS c FROM reviews").fetchone()["c"]
    return {
        "news_count": news_count,
        "event_count": event_count,
        "card_count": card_count,
        "review_count": review_count,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def recent_cards(limit: int = 8, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    return query_all(
        """
        SELECT p.id, p.card_title, p.card_body_markdown, p.created_at, p.delivery_status,
               e.event_type, e.importance_level, e.credibility_level, e.urgency_level,
               n.title AS news_title, n.source AS news_source
        FROM push_cards p
        JOIN events e ON p.event_id = e.id
        JOIN news_raw n ON e.news_id = n.id
        ORDER BY p.id DESC
        LIMIT ?
        """,
        (limit,),
        db_path,
    )


def recent_reviews(limit: int = 8, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    return query_all(
        """
        SELECT r.id, r.card_id, r.review_stage, r.is_correct, r.error_type, r.review_analysis,
               r.actual_market_report, r.reviewed_at, p.card_title
        FROM reviews r
        JOIN push_cards p ON p.id = r.card_id
        ORDER BY r.id DESC
        LIMIT ?
        """,
        (limit,),
        db_path,
    )


def event_type_counts(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    return query_all(
        """
        SELECT event_type, COUNT(*) AS cnt
        FROM events
        GROUP BY event_type
        ORDER BY cnt DESC, event_type ASC
        """,
        db_path=db_path,
    )


def market_snapshot(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    return query_all(
        """
        SELECT asset_code, asset_name, asset_type, benchmark_group, market_source, market_symbol,
               last_market_price, last_market_change_pct, last_market_updated_at
        FROM market_assets
        ORDER BY asset_code ASC
        """,
        db_path=db_path,
    )


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def render_metric(label: str, value: Any) -> str:
    return f"""
    <div class="metric">
      <div class="metric-label">{escape(label)}</div>
      <div class="metric-value">{escape(value)}</div>
    </div>
    """


def render_card(card: dict[str, Any]) -> str:
    body = escape(card["card_body_markdown"]).replace("\n", "<br>")
    return f"""
    <article class="card">
      <div class="card-head">
        <div>
          <div class="eyebrow">{escape(card["event_type"])}</div>
          <h3>{escape(card["card_title"])}</h3>
        </div>
        <div class="status">{escape(card["delivery_status"])}</div>
      </div>
      <div class="meta">
        <span>Importance: {escape(card["importance_level"])}</span>
        <span>Credibility: {escape(card["credibility_level"])}</span>
        <span>Urgency: {escape(card["urgency_level"])}</span>
        <span>Source: {escape(card["news_source"])}</span>
        <span>Created: {escape(card["created_at"])}</span>
      </div>
      <div class="body">{body}</div>
    </article>
    """


def render_review(review: dict[str, Any]) -> str:
    return f"""
    <div class="review">
      <div class="review-top">
        <strong>{escape(review["card_title"])}</strong>
        <span>{escape(review["review_stage"])}</span>
      </div>
      <div class="review-meta">
        <span>Correct: {escape(review["is_correct"])}</span>
        <span>Error: {escape(review["error_type"])}</span>
        <span>Reviewed: {escape(review["reviewed_at"])}</span>
      </div>
      <pre>{escape(review["review_analysis"])}</pre>
    </div>
    """


def render_market_row(row: dict[str, Any]) -> str:
    change = row.get("last_market_change_pct")
    change_text = "n/a" if change is None else f"{float(change):.2f}%"
    return f"""
    <div class="market-row">
      <div>
        <strong>{escape(row["asset_name"])}</strong>
        <div class="sub">{escape(row["asset_code"])} · {escape(row.get("market_symbol") or "static")} · {escape(row.get("market_source") or "seed")}</div>
      </div>
      <div class="market-value">{escape(row.get("last_market_price") or "n/a")}</div>
      <div class="market-change">{escape(change_text)}</div>
      <div class="sub">{escape(row.get("last_market_updated_at") or "")}</div>
    </div>
    """


def render_page(db_path: Path = DB_PATH) -> str:
    st = stats(db_path)
    cards = recent_cards(db_path=db_path)
    reviews = recent_reviews(db_path=db_path)
    counts = event_type_counts(db_path)
    market_rows = market_snapshot(db_path=db_path)

    count_html = "".join(render_metric(label, value) for label, value in [
        ("News", st["news_count"]),
        ("Events", st["event_count"]),
        ("Cards", st["card_count"]),
        ("Reviews", st["review_count"]),
    ])

    if counts:
        max_count = max(int(row["cnt"]) for row in counts) or 1
        bars = []
        for row in counts:
            width = max(8, int(int(row["cnt"]) / max_count * 100))
            bars.append(
                f"""
                <div class="bar-row">
                  <span>{escape(row["event_type"])}</span>
                  <div class="bar"><i style="width:{width}%"></i></div>
                  <strong>{escape(row["cnt"])}</strong>
                </div>
                """
            )
        counts_html = "".join(bars)
    else:
        counts_html = '<div class="empty">No events yet.</div>'

    cards_html = "".join(render_card(card) for card in cards) if cards else '<div class="empty">No cards yet.</div>'
    reviews_html = "".join(render_review(review) for review in reviews) if reviews else '<div class="empty">No reviews yet.</div>'
    market_html = "".join(render_market_row(row) for row in market_rows) if market_rows else '<div class="empty">No market quotes yet.</div>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Aegis Alpha Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3f5f7;
      --panel: #ffffff;
      --panel-alt: #f8fafc;
      --text: #0f172a;
      --muted: #667085;
      --border: #dbe2ea;
      --accent: #0f766e;
      --accent-2: #1d4ed8;
      --shadow: 0 10px 24px rgba(15, 23, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: linear-gradient(180deg, #eef2f7 0%, var(--bg) 100%);
      color: var(--text);
    }}
    header {{
      padding: 24px 32px 18px;
      background: linear-gradient(135deg, #0f172a 0%, #172554 60%, #0f766e 100%);
      color: #fff;
    }}
    header h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    header p {{ margin: 0; max-width: 1000px; color: rgba(255,255,255,.8); line-height: 1.5; }}
    .feed-badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-top: 10px;
      font-size: 12px;
      color: rgba(255,255,255,.82);
    }}
    .feed-dot {{
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: #22c55e;
      box-shadow: 0 0 0 4px rgba(34, 197, 94, .18);
    }}
    main {{ padding: 24px 32px 32px; display: grid; gap: 20px; }}
    .metrics {{ display: grid; gap: 12px; grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: var(--shadow);
      padding: 16px;
    }}
    .metric-label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
    .metric-value {{ font-size: 30px; font-weight: 700; margin-top: 8px; }}
    .grid-two {{ display: grid; gap: 20px; grid-template-columns: 1.5fr 1fr; align-items: start; }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      box-shadow: var(--shadow);
      padding: 18px;
    }}
    .section h2 {{ margin: 0 0 14px; font-size: 18px; }}
    .cards {{ display: grid; gap: 14px; }}
    .card {{
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      background: var(--panel-alt);
    }}
    .card-head {{ display: flex; gap: 12px; justify-content: space-between; align-items: start; }}
    .eyebrow {{ font-size: 12px; color: var(--accent); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }}
    .status {{
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      color: var(--muted);
      background: #fff;
      white-space: nowrap;
    }}
    .meta {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 12px;
      color: var(--muted);
      margin: 10px 0 12px;
    }}
    .body {{
      font-size: 13px;
      line-height: 1.55;
      color: #1f2937;
      word-break: break-word;
    }}
    .bars {{ display: grid; gap: 10px; }}
    .bar-row {{ display: grid; grid-template-columns: 1fr 2fr auto; gap: 12px; align-items: center; }}
    .bar {{
      height: 10px;
      border-radius: 999px;
      background: #e5edf6;
      overflow: hidden;
      position: relative;
    }}
    .bar i {{
      display: block;
      height: 100%;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
    }}
    .review {{
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--panel-alt);
      padding: 14px;
    }}
    .review-top, .review-meta {{
      display: flex;
      gap: 12px;
      justify-content: space-between;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }}
    .review pre {{
      margin: 12px 0 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: inherit;
      font-size: 13px;
      line-height: 1.55;
      color: #1f2937;
    }}
    .empty {{
      border: 1px dashed var(--border);
      border-radius: 12px;
      padding: 16px;
      color: var(--muted);
      background: #fff;
    }}
    .market-list {{ display: grid; gap: 10px; }}
    .market-row {{
      display: grid;
      grid-template-columns: 1.5fr 0.7fr 0.6fr 1fr;
      gap: 12px;
      align-items: center;
      padding: 12px 0;
      border-bottom: 1px solid var(--border);
    }}
    .market-row:last-child {{ border-bottom: 0; }}
    .market-value {{ font-weight: 700; text-align: right; }}
    .market-change {{ text-align: right; color: var(--accent); font-weight: 600; }}
    .sub {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    footer {{
      padding: 0 32px 28px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 1080px) {{
      .metrics, .grid-two {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Aegis Alpha Dashboard</h1>
    <p>事件驱动投资辅助智能体的本地看板：新闻、事件、推理卡片与复盘结果都集中在这里。页面会定时从 API 拉取最新状态。</p>
    <div class="feed-badge"><span class="feed-dot"></span>Auto-poll enabled</div>
  </header>
  <main>
    <section class="metrics">
      {count_html}
    </section>
    <section class="section">
      <h2>Event Mix</h2>
      <div id="event-mix" class="bars">{counts_html}</div>
    </section>
    <section class="grid-two">
      <div class="section">
        <h2>Recent Cards</h2>
        <div id="cards" class="cards">{cards_html}</div>
      </div>
      <div class="section">
        <h2>Market Snapshot</h2>
        <div id="market" class="market-list">{market_html}</div>
      </div>
    </section>
    <section class="section">
      <h2>Recent Reviews</h2>
      <div id="reviews" class="cards">{reviews_html}</div>
    </section>
  </main>
  <footer>
    Generated at {escape(st["generated_at"])}. Database: {escape(str(db_path))}
  </footer>
  <script>
    const REFRESH_MS = 30000;

    function escapeHtml(value) {{
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }}

    function renderCard(card) {{
      const body = escapeHtml(card.card_body_markdown || '').replace(/\\n/g, '<br>');
      return `
        <article class="card">
          <div class="card-head">
            <div>
              <div class="eyebrow">${{escapeHtml(card.event_type || '')}}</div>
              <h3>${{escapeHtml(card.card_title || '')}}</h3>
            </div>
            <div class="status">${{escapeHtml(card.delivery_status || '')}}</div>
          </div>
          <div class="meta">
            <span>Importance: ${{escapeHtml(card.importance_level || '')}}</span>
            <span>Credibility: ${{escapeHtml(card.credibility_level || '')}}</span>
            <span>Urgency: ${{escapeHtml(card.urgency_level || '')}}</span>
            <span>Source: ${{escapeHtml(card.news_source || '')}}</span>
            <span>Created: ${{escapeHtml(card.created_at || '')}}</span>
          </div>
          <div class="body">${{body}}</div>
        </article>
      `;
    }}

    function renderReview(review) {{
      return `
        <div class="review">
          <div class="review-top">
            <strong>${{escapeHtml(review.card_title || '')}}</strong>
            <span>${{escapeHtml(review.review_stage || '')}}</span>
          </div>
          <div class="review-meta">
            <span>Correct: ${{escapeHtml(review.is_correct ?? '')}}</span>
            <span>Error: ${{escapeHtml(review.error_type || '')}}</span>
            <span>Reviewed: ${{escapeHtml(review.reviewed_at || '')}}</span>
          </div>
          <pre>${{escapeHtml(review.review_analysis || '')}}</pre>
        </div>
      `;
    }}

    function renderMarketRow(row) {{
      const change = row.last_market_change_pct == null ? 'n/a' : `${{Number(row.last_market_change_pct).toFixed(2)}}%`;
      return `
        <div class="market-row">
          <div>
            <strong>${{escapeHtml(row.asset_name || '')}}</strong>
            <div class="sub">${{escapeHtml(row.asset_code || '')}} · ${{escapeHtml(row.market_symbol || 'static')}} · ${{escapeHtml(row.market_source || 'seed')}}</div>
          </div>
          <div class="market-value">${{escapeHtml(row.last_market_price ?? 'n/a')}}</div>
          <div class="market-change">${{escapeHtml(change)}}</div>
          <div class="sub">${{escapeHtml(row.last_market_updated_at || '')}}</div>
        </div>
      `;
    }}

    function renderMetrics(data) {{
      return `
        <div class="metric"><div class="metric-label">News</div><div class="metric-value">${{escapeHtml(data.news_count ?? 0)}}</div></div>
        <div class="metric"><div class="metric-label">Events</div><div class="metric-value">${{escapeHtml(data.event_count ?? 0)}}</div></div>
        <div class="metric"><div class="metric-label">Cards</div><div class="metric-value">${{escapeHtml(data.card_count ?? 0)}}</div></div>
        <div class="metric"><div class="metric-label">Reviews</div><div class="metric-value">${{escapeHtml(data.review_count ?? 0)}}</div></div>
      `;
    }}

    function renderBars(rows) {{
      if (!rows || !rows.length) return '<div class="empty">No events yet.</div>';
      const maxCount = Math.max(...rows.map(row => Number(row.cnt || 0)), 1);
      return rows.map(row => {{
        const width = Math.max(8, Math.round(Number(row.cnt || 0) / maxCount * 100));
        return `
          <div class="bar-row">
            <span>${{escapeHtml(row.event_type || '')}}</span>
            <div class="bar"><i style="width:${{width}}%"></i></div>
            <strong>${{escapeHtml(row.cnt ?? '')}}</strong>
          </div>
        `;
      }}).join('');
    }}

    async function updateSection(url, targetId) {{
      const res = await fetch(url, {{ cache: 'no-store' }});
      if (!res.ok) return;
      const data = await res.json();
      const el = document.getElementById(targetId);
      if (!el) return;
      if (targetId === 'cards') el.innerHTML = data.length ? data.map(renderCard).join('') : '<div class="empty">No cards yet.</div>';
      if (targetId === 'reviews') el.innerHTML = data.length ? data.map(renderReview).join('') : '<div class="empty">No reviews yet.</div>';
      if (targetId === 'market') el.innerHTML = data.length ? data.map(renderMarketRow).join('') : '<div class="empty">No market quotes yet.</div>';
      if (targetId === 'event-mix') el.innerHTML = renderBars(data);
      if (targetId === 'metrics') el.innerHTML = renderMetrics(data);
    }}

    async function refreshAll() {{
      try {{
        await Promise.all([
          updateSection('/api/summary', 'metrics'),
          updateSection('/api/events', 'event-mix'),
          updateSection('/api/cards', 'cards'),
          updateSection('/api/reviews', 'reviews'),
          updateSection('/api/market', 'market'),
        ]);
      }} catch (err) {{
        console.warn(err);
      }}
    }}

    refreshAll();
    setInterval(refreshAll, REFRESH_MS);
  </script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    db_path = DB_PATH

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/summary"):
            self.send_json(stats(self.db_path))
            return
        if self.path.startswith("/api/cards"):
            self.send_json(recent_cards(db_path=self.db_path))
            return
        if self.path.startswith("/api/reviews"):
            self.send_json(recent_reviews(db_path=self.db_path))
            return
        if self.path.startswith("/api/events"):
            self.send_json(event_type_counts(db_path=self.db_path))
            return
        if self.path.startswith("/api/market"):
            self.send_json(market_snapshot(db_path=self.db_path))
            return

        html_text = render_page(self.db_path)
        body = html_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aegis Alpha dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(DB_PATH))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli()
    args = parser.parse_args(argv)
    db_path = Path(args.db).resolve()
    DashboardHandler.db_path = db_path
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Aegis dashboard running at http://{args.host}:{args.port}")
    print(f"Using database: {db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
