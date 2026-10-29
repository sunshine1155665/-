"""Command line access to the expression library.

Examples::

    python -m fatfish.lexicon list --status approved --limit 20
    python -m fatfish.lexicon stats
    python -m fatfish.lexicon mine --days 14 --min-count 3
    python -m fatfish.lexicon approve 0123456789abcdef
    python -m fatfish.lexicon disable 0123456789abcdef
    python -m fatfish.lexicon add --kind expression --content 摸鱼 --meaning 偷偷放松
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from ..config import load_config
from ..db import Database
from .miner import mine
from .service import VALID_KINDS, VALID_STATUSES, VALID_STYLES, LexiconService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fatfish.lexicon",
        description="Inspect, mine and review fatfish expressions.",
    )
    parser.add_argument("--profile", default=None, help="config profile (default: env or low)")
    parser.add_argument("--config", default=None, help="explicit config TOML path")
    parser.add_argument("--db", default=None, help="override the SQLite database path")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list entries")
    listing.add_argument("--status", choices=[*VALID_STATUSES, "all"], default=None)
    listing.add_argument("--query", default=None)
    listing.add_argument("--limit", type=int, default=50)

    sub.add_parser("stats", help="show counts by status/kind/source")

    digging = sub.add_parser("mine", help="mine candidate jargon from conversations")
    digging.add_argument("--days", type=int, default=14)
    digging.add_argument("--limit-turns", type=int, default=800)
    digging.add_argument("--min-count", type=int, default=3)

    for name, help_text in (("approve", "approve an entry"), ("disable", "disable an entry")):
        action = sub.add_parser(name, help=help_text)
        action.add_argument("id")

    adding = sub.add_parser("add", help="add a manual entry")
    adding.add_argument("--kind", choices=VALID_KINDS, required=True)
    adding.add_argument("--content", required=True)
    adding.add_argument("--meaning", default="")
    adding.add_argument("--situation", default="")
    adding.add_argument("--style", choices=["", *VALID_STYLES], default="")
    adding.add_argument("--tags", default="", help="comma-separated tags")
    adding.add_argument("--examples", default="", help="examples separated by |")
    adding.add_argument("--status", choices=VALID_STATUSES, default="candidate")
    adding.add_argument("--confidence", type=float, default=0.5)
    return parser


def _bootstrap(args: argparse.Namespace) -> tuple[Any, Database]:
    if args.db:
        os.environ["FATFISH_DB"] = str(args.db)
    config = load_config(args.profile, args.config)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


def _split(text: str, separator: str) -> list[str]:
    return [part.strip() for part in str(text or "").split(separator) if part.strip()]


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config, db = _bootstrap(args)
    try:
        lexicon = LexiconService(db, config)
        if args.command == "list":
            status = None if args.status in {None, "all"} else args.status
            items = lexicon.list(status=status, query=args.query, limit=args.limit)
            if args.json:
                _print(
                    {
                        "ok": True,
                        "count": len(items),
                        "items": items,
                        "stats": lexicon.stats(),
                    }
                )
            else:
                for item in items:
                    meaning = item["meaning"] or item["situation"] or ""
                    suffix = f" —— {meaning}" if meaning else ""
                    print(
                        f"{item['id']}  [{item['status']}/{item['kind']}]  "
                        f"{item['content']}{suffix}"
                    )
                print(f"-- {len(items)} entries")
        elif args.command == "stats":
            _print({"ok": True, "stats": lexicon.stats()})
        elif args.command == "mine":
            result = mine(
                lexicon,
                days=args.days,
                limit_turns=args.limit_turns,
                min_count=args.min_count,
            )
            _print({"ok": True, **result})
        elif args.command in {"approve", "disable"}:
            status = "approved" if args.command == "approve" else "disabled"
            item = lexicon.review(args.id, status)
            if item is None:
                _print({"ok": False, "error": "not_found", "id": args.id})
                return 1
            _print({"ok": True, "item": item})
        elif args.command == "add":
            item = lexicon.add(
                args.kind,
                args.content,
                meaning=args.meaning,
                situation=args.situation,
                style=args.style,
                tags=_split(args.tags, ","),
                examples=_split(args.examples, "|"),
                status=args.status,
                confidence=args.confidence,
            )
            _print({"ok": True, "item": item})
        else:  # pragma: no cover - argparse enforces the choices
            print(f"unknown command: {args.command}", file=sys.stderr)
            return 2
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
