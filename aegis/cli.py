from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from .config import DB_PATH, FEED_CONFIG_PATH, WECOM_CONFIG_PATH
from .pipeline import AegisPipeline


def parse_price_updates(items: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in items:
        if "=" not in item:
            continue
        code, price = item.split("=", 1)
        try:
            result[code.strip()] = float(price.strip())
        except ValueError:
            continue
    return result


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aegis Alpha MVP event-driven investment agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init-db", help="Initialize or migrate the local database")

    demo = sub.add_parser("demo", help="Run the offline demo batch")
    demo.add_argument("--export", action="store_true", help="Print the digest markdown after processing")

    ingest = sub.add_parser("ingest", help="Ingest a single news item")
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--content", required=True)
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--review", action="store_true", help="Simulate a review after analysis")

    batch = sub.add_parser("import-json", help="Import a JSON feed file")
    batch.add_argument("--path", required=True)
    batch.add_argument("--review", action="store_true")

    digest = sub.add_parser("digest", help="Print a markdown digest for recent cards")
    digest.add_argument("--limit", type=int, default=8)

    review = sub.add_parser("review", help="Review an existing card")
    review.add_argument("--card-id", type=int, required=True)
    review.add_argument("--stage", default="24H")
    review.add_argument("--price", action="append", default=[], help="Asset price override like CL00Y=86.5")
    review.add_argument("--simulate", action="store_true", help="Generate a synthetic market path from the card")

    export = sub.add_parser("export", help="Export recent cards to markdown")
    export.add_argument("--limit", type=int, default=20)
    export.add_argument("--output", default="")

    pending = sub.add_parser("process-pending", help="Process news that has not been classified yet")
    pending.add_argument("--limit", type=int, default=20)

    feeds_template = sub.add_parser("feeds-template", help="Write a starter news feed config")
    feeds_template.add_argument("--output", default=str(FEED_CONFIG_PATH))

    wecom_template = sub.add_parser("wecom-template", help="Write a starter WeCom config")
    wecom_template.add_argument("--output", default=str(WECOM_CONFIG_PATH))

    sync = sub.add_parser("sync-feeds", help="Fetch configured feeds and process new items")
    sync.add_argument("--config", default=str(FEED_CONFIG_PATH))
    sync.add_argument("--limit-per-feed", type=int, default=10)
    sync.add_argument("--no-immediate-process", action="store_true")

    sub.add_parser("refresh-market", help="Refresh market quotes from public data sources")

    sync_all = sub.add_parser("sync-all", help="Refresh market quotes, fetch feeds, and write a digest")
    sync_all.add_argument("--config", default=str(FEED_CONFIG_PATH))
    sync_all.add_argument("--limit-per-feed", type=int, default=10)
    sync_all.add_argument("--no-digest", action="store_true")
    sync_all.add_argument("--push-wecom", action="store_true")

    push_digest = sub.add_parser("push-digest", help="Push the daily digest to WeCom")
    push_digest.add_argument("--limit", type=int, default=8)

    push_card = sub.add_parser("push-card", help="Push a single card to WeCom")
    push_card.add_argument("--card-id", type=int, required=True)

    push_urgent = sub.add_parser("push-urgent", help="Push urgent events to WeCom")
    push_urgent.add_argument("--limit", type=int, default=5)

    loop = sub.add_parser("run-loop", help="Run the scheduled fetch/analyze loop")
    loop.add_argument("--config", default=str(FEED_CONFIG_PATH))
    loop.add_argument("--base-interval-minutes", type=int, default=120)
    loop.add_argument("--urgent-interval-minutes", type=int, default=15)
    loop.add_argument("--limit-per-feed", type=int, default=10)
    loop.add_argument("--once", action="store_true")
    loop.add_argument("--cycles", type=int, default=None)
    loop.add_argument("--no-digest", action="store_true")
    loop.add_argument("--push-wecom", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli()
    args = parser.parse_args(argv)
    pipeline = AegisPipeline()

    command = args.command or "demo"

    if command == "init-db":
        logging.info("Database initialized at %s", DB_PATH)
        return 0

    if command == "demo":
        result = pipeline.run_demo()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.export:
            print("\n" + pipeline.daily_digest_markdown())
        return 0

    if command == "ingest":
        result = pipeline.process_single_news(args.title, args.content, args.source, review_with_simulation=args.review)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "import-json":
        news_ids = pipeline.import_news_file(args.path)
        processed: list[dict[str, Any]] = []
        for news_id in news_ids:
            event_id = pipeline.verifier_agent(news_id)
            card_id = pipeline.analyst_agent(event_id)
            payload = {"news_id": news_id, "event_id": event_id, "card_id": card_id}
            if args.review:
                simulated_prices = pipeline.simulate_market_prices_for_card(card_id)
                _, review_report, review_payload = pipeline.review_agent(card_id, simulated_prices, review_stage="24H")
                payload["review_report"] = review_report
                payload["review"] = review_payload
            processed.append(payload)
        print(json.dumps(processed, ensure_ascii=False, indent=2))
        return 0

    if command == "digest":
        print(pipeline.daily_digest_markdown(limit=args.limit))
        return 0

    if command == "review":
        prices = parse_price_updates(args.price)
        market_prices = prices if prices else None
        if args.simulate:
            market_prices = pipeline.simulate_market_prices_for_card(args.card_id)
        card_id, report, payload = pipeline.review_agent(
            args.card_id,
            market_prices=market_prices,
            review_stage=args.stage,
            auto_simulate=args.simulate and market_prices is None,
        )
        print(json.dumps({"card_id": card_id, "report": report, **payload}, ensure_ascii=False, indent=2))
        return 0

    if command == "export":
        markdown = pipeline.export_cards_markdown(limit=args.limit)
        if args.output:
            Path(args.output).write_text(markdown, encoding="utf-8")
            print(f"Exported to {args.output}")
        else:
            print(markdown)
        return 0

    if command == "process-pending":
        event_ids = pipeline.batch_process_pending_news(limit=args.limit)
        print(json.dumps({"event_ids": event_ids}, ensure_ascii=False, indent=2))
        return 0

    if command == "feeds-template":
        path = pipeline.write_feed_template(args.output)
        print(json.dumps({"output": str(path)}, ensure_ascii=False, indent=2))
        return 0

    if command == "wecom-template":
        template = {
            "corp_id": "",
            "corp_secret": "",
            "agent_id": 1000002,
            "touser": "ZhangYiTao",
            "toparty": "",
            "totag": "",
        }
        path = Path(args.output)
        path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(path)}, ensure_ascii=False, indent=2))
        return 0

    if command == "sync-feeds":
        result = pipeline.sync_news_feeds(
            config_path=args.config,
            limit_per_feed=args.limit_per_feed,
            process_immediately=not args.no_immediate_process,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "refresh-market":
        result = pipeline.refresh_market_data()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "push-digest":
        result = pipeline.push_daily_digest(limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "push-card":
        result = pipeline.push_card(args.card_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "push-urgent":
        result = pipeline.push_urgent_events(limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "sync-all":
        market_result = pipeline.refresh_market_data()
        feed_result = pipeline.sync_news_feeds(
            config_path=args.config,
            limit_per_feed=args.limit_per_feed,
            process_immediately=True,
        )
        digest_path = "" if args.no_digest else str(pipeline.export_digest_file())
        wecom_result = None
        if args.push_wecom:
            wecom_result = pipeline.push_daily_digest(limit=8)
        result = {
            "market_result": market_result,
            "feed_result": feed_result,
            "digest_path": digest_path,
            "wecom_result": wecom_result,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "run-loop":
        result = pipeline.run_loop(
            config_path=args.config,
            base_interval_minutes=args.base_interval_minutes,
            urgent_interval_minutes=args.urgent_interval_minutes,
            limit_per_feed=args.limit_per_feed,
            one_shot=args.once,
            max_cycles=args.cycles,
            write_digest=not args.no_digest,
        )
        if args.push_wecom:
            try:
                result.append({"wecom_push": pipeline.push_daily_digest(limit=8)})
            except Exception as exc:
                result.append({"wecom_push_error": str(exc)})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
