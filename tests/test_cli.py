from aegis.cli import build_cli, parse_price_updates


def test_price_update_parser_ignores_invalid_values():
    assert parse_price_updates(["CL00Y=86.5", "bad", "DXY=nope", " US10Y = 4.2 "]) == {
        "CL00Y": 86.5,
        "US10Y": 4.2,
    }


def test_cli_exposes_core_commands():
    parser = build_cli()
    assert parser.parse_args(["init-db"]).command == "init-db"
    args = parser.parse_args(["ingest", "--title", "t", "--content", "c", "--source", "s"])
    assert args.command == "ingest"
    assert args.title == "t"
