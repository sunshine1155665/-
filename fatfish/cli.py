"""Command line interface for the low-profile core."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, env_secret, load_config
from .context import ContextAssembler, ensure_default_persona
from .db import Database
from .doctor import check, format_report
from .life import LifeEngine
from .lexicon import LexiconService, VALID_KINDS, VALID_STATUSES, VALID_STYLES, mine
from .memory import MemoryService
from .outbox import Outbox
from .owner import OwnerRegistry
from .privacy import SecretNotes
from .state import StateMachine
from .sticker import StickerService
from .tts import TTSClient
from .voice import VoicePolicy
from .wakeup import ProactiveGate, WakeupScheduler
from .runtime import Runtime
from .nightly import run_nightly
from .bilibili import BilibiliClient, BilibiliCollector, BilibiliError
from .qzone import QzoneClient, QzoneCollector, QzoneError
from .platforms.onebot import OneBotClient, websocket_available
from .platforms.qq import QQAdapter
from .process_lock import acquire, lock_path
from .security import KernelError, PolicyEngine, load_kernel
from .trace import TraceRecorder


def _bootstrap(args: argparse.Namespace) -> tuple[Any, Database]:
    config = load_config(args.profile, args.config)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.ensure_schema()
    ensure_default_persona(db)
    return config, db


def _services(config: Any, db: Database) -> dict[str, Any]:
    memory = MemoryService(db, config)
    return {
        "db": db,
        "config": config,
        "memory": memory,
        "state": StateMachine(db),
        "outbox": Outbox(db),
        "life": LifeEngine(db, config),
        "wakeup": WakeupScheduler(db, config),
        "gate": ProactiveGate(db, config),
        "voice": VoicePolicy(db, config),
        "sticker": StickerService(db, config),
        "tts": TTSClient(config, db),
        "context": ContextAssembler(db, config, memory),
        "bilibili": BilibiliCollector(memory, config),
        "qzone": QzoneCollector(memory, config),
    }


def cmd_init(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    services = _services(config, db)
    services["life"].tick()
    print(f"initialized: {config.db_path}")
    print(f"profile: {config.profile}; data: {config.data_dir}")
    print(f"wakeups: {services['wakeup'].stats()}")
    print(f"outbox: {services['outbox'].stats()}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.profile, args.config)
    report = check(config)
    print(format_report(report))
    return 0 if report["ok"] else 1


def cmd_state(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        load_kernel(config).ensure_meta(db)
    except KernelError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    s = _services(config, db)
    mem_count = db.query_one("SELECT COUNT(*) AS n FROM memory_items")
    rag_count = db.query_one("SELECT COUNT(*) AS n FROM rag_chunks")
    trace_count = db.query_one("SELECT COUNT(*) AS n FROM turn_traces")
    print(
        json.dumps(
            {
                "profile": config.profile,
                "schema_version": db.get_meta("schema_version", "0"),
                "agent": s["state"].snapshot(),
                "life": asdict(s["life"].state()),
                "memory_items": int(mem_count["n"]) if mem_count else 0,
                "rag_chunks": int(rag_count["n"]) if rag_count else 0,
                "traces": int(trace_count["n"]) if trace_count else 0,
                "wakeups": s["wakeup"].stats(),
                "outbox": s["outbox"].stats(),
                "security": {
                    "kernel_hash": db.get_meta("identity_kernel_hash"),
                    "kernel_version": db.get_meta("identity_kernel_version"),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_owner_status(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        payload = {"ok": True, **OwnerRegistry(db, config).status()}
    finally:
        db.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_owner_bind(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        owner = OwnerRegistry(db, config)
        try:
            binding = owner.bind(
                args.session,
                display_name=args.name or owner.default_display_name,
                source="cli",
            )
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
    finally:
        db.close()
    print(json.dumps({"ok": True, "binding": binding}, ensure_ascii=False, indent=2))
    return 0


def cmd_owner_unbind(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        owner = OwnerRegistry(db, config)
        removed = owner.unbind()
        payload = {"ok": True, "removed": removed, "status": owner.status()}
    finally:
        db.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_owner_friend_status(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        owner = OwnerRegistry(db, config)
        friend = owner.get_friend()
        payload = {
            "ok": True,
            "friend_bound": friend is not None,
            "friend": friend,
            "owner_session": (owner.get() or {}).get("session_id"),
        }
    finally:
        db.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_owner_friend_bind(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        owner = OwnerRegistry(db, config)
        try:
            binding = owner.bind_friend(
                args.session,
                display_name=args.name or owner.default_friend_name,
                source="cli",
            )
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
    finally:
        db.close()
    print(json.dumps({"ok": True, "binding": binding}, ensure_ascii=False, indent=2))
    return 0


def cmd_owner_friend_unbind(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        owner = OwnerRegistry(db, config)
        removed = owner.unbind_friend()
        payload = {"ok": True, "removed": removed, "friend": owner.get_friend()}
    finally:
        db.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_owner_role_bind(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        owner = OwnerRegistry(db, config)
        try:
            binding = owner.bind(
                args.session,
                display_name=args.name or args.session.rsplit(":", 1)[-1],
                source="cli",
                role=args.role,
            )
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
    finally:
        db.close()
    print(json.dumps({"ok": True, "binding": binding}, ensure_ascii=False, indent=2))
    return 0


def cmd_owner_role_unbind(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        owner = OwnerRegistry(db, config)
        removed = owner.unbind(args.role, session_id=args.session)
        payload = {"ok": True, "removed": removed, "status": owner.status()}
    finally:
        db.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_owner_secret_add(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        notes = SecretNotes(db)
        try:
            note = notes.add(args.text, source="cli")
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        payload = {"ok": True, "note": note, "total": notes.count()}
    finally:
        db.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_owner_secret_list(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        notes = SecretNotes(db)
        rows = notes.list(limit=args.limit)
        payload = {"ok": True, "count": len(rows), "notes": rows}
    finally:
        db.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_owner_secret_remove(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        notes = SecretNotes(db)
        removed = notes.remove(args.id)
        payload = {"ok": True, "removed": removed, "total": notes.count()}
    finally:
        db.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_memory_add(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    item = s["memory"].add_item(
        args.text,
        type=args.type,
        importance=args.importance,
        event_time=args.event_time,
        tags=args.tag or [],
        entities=args.entity or [],
        source_type=args.source_type,
        source_id=args.source_id,
        evidence=args.evidence,
    )
    print(json.dumps(_without_embedding(asdict(item)), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_memory_audience(args: argparse.Namespace) -> int:
    """T-0073: flip one memory row's audience (open / owner)."""
    config, db = _bootstrap(args)
    try:
        value = str(args.value).strip().lower()
        if value not in ("open", "owner"):
            print(json.dumps({"ok": False, "error": "value must be open|owner"}, ensure_ascii=False))
            return 2
        row = db.query_one("SELECT id, audience FROM memory_items WHERE id=?", (args.id,))
        if row is None:
            print(json.dumps({"ok": False, "error": "no such memory id"}, ensure_ascii=False))
            return 1
        db.execute(
            "UPDATE memory_items SET audience=?, updated_at=? WHERE id=?",
            (value, time.time(), args.id),
        )
        print(json.dumps({"ok": True, "id": args.id, "was": row["audience"], "now": value}, ensure_ascii=False))
    finally:
        db.close()
    return 0


def cmd_memory_search(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    hits = s["memory"].search(args.query, limit=args.limit)
    for hit in hits:
        item = hit.item
        when = item.event_date or time.strftime("%Y-%m-%d", time.localtime(item.observed_at))
        print(f"[{hit.score:.3f}] ({when}) {item.gist}  # {item.type} tier={item.tier}")
    if not hits:
        print("no hits")
    return 0


def cmd_memory_details(args: argparse.Namespace) -> int:
    from .memory.details import search_details

    config, db = _bootstrap(args)
    rows = search_details(db, args.query, limit=args.limit)
    for row in rows:
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(row["occurred_at"]))
            if row.get("occurred_at") else "—"
        )
        title = row.get("event_title") or "未命名事件"
        print(f"[{row['kind']}] ({when}) {title}")
        print(f"  细节: {row['text']}")
        if row.get("quote_text"):
            print(f"  原话: {row['quote_text']}")
    if not rows:
        print("no details")
    return 0


def cmd_memory_list(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    for item in s["memory"].list_items(limit=args.limit):
        when = item.event_date or time.strftime("%Y-%m-%d", time.localtime(item.observed_at))
        print(f"({when}) [{item.type}] imp={item.importance:.2f} tier={item.tier} {item.gist}")
    return 0


def cmd_memory_meta(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    print(json.dumps(s["memory"].meta_directory(limit=args.limit), ensure_ascii=False, indent=2))
    return 0


def cmd_memory_episodes(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    clauses: list[str] = []
    params: list[Any] = []
    if args.date:
        clauses.append("id LIKE ?")
        params.append(f"ep:{args.date}:%")
    sql = "SELECT * FROM episodes"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY start_at DESC, id LIMIT ?"
    params.append(int(args.limit))
    rows = []
    for row in db.query(sql, params):
        item = dict(row)
        try:
            item["memory_ids"] = json.loads(item.pop("memory_ids_json", "[]") or "[]")
        except json.JSONDecodeError:
            item["memory_ids"] = []
        try:
            item["event_ids"] = json.loads(item.pop("event_ids_json", "[]") or "[]")
        except json.JSONDecodeError:
            item["event_ids"] = []
        rows.append(item)
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_memory_recall(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    result = s["memory"].recall(
        args.query, limit_memory=args.limit, include_quotes=bool(args.quotes)
    )
    print(result.ephemeral_text or "no recall")
    print("refs:", result.refs)
    return 0


def cmd_memory_entities(args: argparse.Namespace) -> int:
    from .memory.entities import entities_for_query

    config, db = _bootstrap(args)
    if args.query:
        rows = entities_for_query(db, args.query, limit=int(args.limit))
    else:
        rows = [
            dict(row)
            for row in db.query(
                "SELECT e.id, e.kind, e.canonical_name, COUNT(m.id) AS mentions "
                "FROM entities AS e LEFT JOIN entity_mentions AS m "
                "ON m.entity_id = e.id "
                "GROUP BY e.id, e.kind, e.canonical_name "
                "ORDER BY mentions DESC, e.canonical_name LIMIT ?",
                (int(args.limit),),
            )
        ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_memory_backfill(args: argparse.Namespace) -> int:
    from .memory.edges import backfill_edges
    from .memory.entities import backfill_entities
    from .memory.tags import backfill_tags

    config, db = _bootstrap(args)
    print(
        json.dumps(
            {
                "ok": True,
                "entities": backfill_entities(db),
                "tags": backfill_tags(db),
                "edges": backfill_edges(db),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_memory_edges(args: argparse.Namespace) -> int:
    from .memory.edges import list_edges

    config, db = _bootstrap(args)
    print(
        json.dumps(
            list_edges(db, limit=int(args.limit)), ensure_ascii=False, indent=2
        )
    )
    return 0


# ----------------------------------------------------------- people (T-0057)
def _mask_person_payload(payload: Any, full_uin: bool) -> Any:
    """Hide every QQ account id in a person payload unless explicitly asked.

    The uin is a local-only identifier: it lives in the entity id and attrs
    and must not end up in a pasted log or a bug report by accident, so both
    the ``uin`` field and any ``person:qq:<uin>`` id (also inside
    ``merged_from`` / ``merged``) are masked by default; ``--full-uin``
    prints everything as stored.
    """
    if full_uin:
        return payload
    from .qzone.qr_login import mask_uin, mask_uin_text

    if isinstance(payload, str):
        return mask_uin_text(payload)
    if isinstance(payload, list):
        return [_mask_person_payload(item, False) for item in payload]
    if isinstance(payload, dict):
        masked: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "uin" and isinstance(value, str) and value:
                masked["uin"] = mask_uin(value)
                masked["uin_masked"] = True
            else:
                masked[key] = _mask_person_payload(value, False)
        return masked
    return payload


def _person_selector(db: Any, text: str) -> str:
    """Resolve ``id | 姓名 | 别名 | uin`` to one live entity id."""
    from .memory.entities import resolve_person_selector

    return resolve_person_selector(db, text)


def _person_error(exc: ValueError) -> int:
    print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
    return 2


def cmd_memory_person_add(args: argparse.Namespace) -> int:
    from .memory.entities import create_person

    config, db = _bootstrap(args)
    try:
        result = create_person(
            db,
            name=args.name,
            uin=args.uin or "",
            aliases=args.alias or [],
            relation=args.relation,
            remark=args.remark,
            source="cli",
        )
    except ValueError as exc:
        return _person_error(exc)
    print(json.dumps(_mask_person_payload(result, args.full_uin), ensure_ascii=False, indent=2))
    return 0


def cmd_memory_person_list(args: argparse.Namespace) -> int:
    from .memory.entities import list_people

    config, db = _bootstrap(args)
    people = list(list_people(db, query=args.query or "", limit=args.limit))
    print(
        json.dumps(
            _mask_person_payload(
                {"ok": True, "count": len(people), "people": people},
                args.full_uin,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_memory_person_show(args: argparse.Namespace) -> int:
    from .memory.entities import entity_card

    config, db = _bootstrap(args)
    try:
        entity_id = _person_selector(db, args.selector)
    except ValueError as exc:
        return _person_error(exc)
    card = entity_card(db, entity_id)
    print(
        json.dumps(
            _mask_person_payload(
                {
                    "ok": True,
                    "entity": card,
                    "alias": card.get("aliases") if card else None,
                },
                args.full_uin,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_memory_person_rename(args: argparse.Namespace) -> int:
    from .memory.entities import update_person

    config, db = _bootstrap(args)
    try:
        entity_id = _person_selector(db, args.selector)
        result = update_person(db, entity_id, name=args.name)
    except ValueError as exc:
        return _person_error(exc)
    print(json.dumps(_mask_person_payload(result, args.full_uin), ensure_ascii=False, indent=2))
    return 0


def cmd_memory_person_alias(args: argparse.Namespace) -> int:
    from .memory.entities import update_person

    config, db = _bootstrap(args)
    try:
        entity_id = _person_selector(db, args.selector)
        result = update_person(
            db,
            entity_id,
            aliases_add=[args.alias] if args.action == "add" else [],
            aliases_remove=[args.alias] if args.action == "remove" else [],
        )
    except ValueError as exc:
        return _person_error(exc)
    print(json.dumps(_mask_person_payload(result, args.full_uin), ensure_ascii=False, indent=2))
    return 0


def cmd_memory_person_merge(args: argparse.Namespace) -> int:
    from .memory.entities import merge_entities

    config, db = _bootstrap(args)
    try:
        source = _person_selector(db, args.source)
        target = _person_selector(db, args.target)
        result = merge_entities(db, source, target)
    except ValueError as exc:
        return _person_error(exc)
    print(json.dumps(_mask_person_payload(result, args.full_uin), ensure_ascii=False, indent=2))
    return 0


def cmd_memory_person_duplicates(args: argparse.Namespace) -> int:
    from .memory.entities import duplicate_candidates

    config, db = _bootstrap(args)
    pairs = list(duplicate_candidates(db, limit=args.limit))
    print(
        json.dumps(
            _mask_person_payload(
                {"ok": True, "count": len(pairs), "pairs": pairs},
                args.full_uin,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_memory_person_sync(args: argparse.Namespace) -> int:
    from .memory.entities import sync_binding_entities

    config, db = _bootstrap(args)
    result = sync_binding_entities(db)
    print(
        json.dumps(
            _mask_person_payload({"ok": True, "roles": result}, args.full_uin),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    stats = s["memory"].compact(dry_run=not args.apply)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_wakeup_schedule(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    w = s["wakeup"].schedule(
        args.when,
        intent=args.intent,
        reason=args.reason,
        session_id=args.session,
        urgent=args.urgent,
    )
    print(json.dumps(asdict(w), ensure_ascii=False, indent=2))
    return 0


def cmd_wakeup_list(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    for w in s["wakeup"].list(args.status, limit=args.limit):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(w.fire_at))
        print(f"{w.id[:8]} {when} [{w.status}] {w.intent}: {w.reason}")
    return 0


def cmd_wakeup_due(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    due = s["wakeup"].due(limit=args.limit)
    for w in due:
        ok, reason = s["gate"].should_act(w)
        print(f"{w.id[:8]} [{w.intent}] gate={ok} ({reason}) {w.reason}")
    if not due:
        print("no due wakeups")
    return 0


def cmd_wakeup_fire(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    w = s["wakeup"].fire(args.id)
    if not w:
        print("wakeup not found or not pending")
        return 1
    ok, reason = s["gate"].should_act(w)
    print(json.dumps({"wakeup": asdict(w), "gate": ok, "gate_reason": reason}, ensure_ascii=False, indent=2))
    return 0


def cmd_voice_decide(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    d = s["voice"].decide(
        args.text,
        session_id=args.session,
        is_group=args.group,
        user_requested=args.request,
    )
    print(json.dumps(asdict(d), ensure_ascii=False, indent=2))
    return 0


def cmd_sticker_add(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    st = s["sticker"].add_sticker(
        args.file,
        caption=args.caption,
        tags=args.tag or [],
        intents=args.intent or [],
        intensity=args.intensity,
        source=args.source,
    )
    print(json.dumps(_without_embedding(asdict(st)), ensure_ascii=False, indent=2))
    return 0


def cmd_sticker_search(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    for c in s["sticker"].search(args.need, limit=args.limit):
        print(f"[{c.score:.3f}] {c.sticker.caption} (id={c.sticker.id[:8]})")
    return 0


def cmd_sticker_decide(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    d = s["sticker"].decide(args.need, session_id=args.session)
    payload = {
        "send": d.send,
        "reason": d.reason,
        "score": d.score,
        "sticker": _without_embedding(asdict(d.sticker)) if d.sticker else None,
        "candidates": [
            {"caption": c.sticker.caption, "score": c.score, "id": c.sticker.id}
            for c in d.candidates[:5]
        ],
        "generated": d.generated,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_context_preview(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    recall = s["memory"].recall(args.message, limit_memory=4) if args.recall else None
    bundle = s["context"].build(args.session, args.message, recall=recall)
    print(bundle.text())
    print("\n--- stats ---")
    print(
        json.dumps(
            {
                "stable_prefix_hash": bundle.stable_prefix_hash[:16],
                "dynamic_hash": bundle.dynamic_hash[:16],
                "prefix_hit": bundle.prefix_hit,
                "estimated_tokens": bundle.estimated_tokens,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    now = time.time()
    s["life"].tick(now=now)
    s["memory"].add_item(
        "用户说他最近在准备面试，有点紧张。",
        type="relationship",
        importance=0.8,
        event_time=now,
        tags=["用户", "面试"],
        entities=["用户"],
        source_type="demo",
    )
    s["memory"].add_rag_chunk(
        "bilibili",
        "BV_DEMO",
        "一个关于面试准备的视频，讲了三件事：提前查公司、准备两个项目故事、面试前深呼吸。",
        metadata={"title": "面试准备", "bvid": "BV_DEMO"},
    )
    recall = s["memory"].recall("面试准备")
    bundle = s["context"].build("demo", "我明天要面试了，有点慌", recall=recall)
    voice = s["voice"].decide("别慌，我陪你顺一遍。", session_id="demo")
    sticker = s["sticker"].decide("想安慰一下对方，轻轻的", session_id="demo")
    wake = s["wakeup"].schedule("+30m", intent="check_user", reason="看看他面试准备得怎么样", session_id="demo")
    print(bundle.text())
    print("\n--- decisions ---")
    print("voice:", voice)
    print("sticker:", sticker.reason, sticker.score)
    print("wakeup:", wake.id[:8], time.strftime("%H:%M", time.localtime(wake.fire_at)))
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    runtime = Runtime(db, config)
    result = runtime.handle_user_message(args.session, args.message, is_group=args.group)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    runtime = Runtime(db, config)
    result = runtime.tick()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    runtime = Runtime(db, config)
    if args.once:
        print(json.dumps(runtime.tick(), ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"fatfish runtime started (profile={config.profile}, poll={args.poll}s)")
    try:
        runtime.run_forever(poll_seconds=args.poll)
    except KeyboardInterrupt:
        print("stopped")
    return 0


def cmd_nightly(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    result = run_nightly(
        db,
        config,
        s["memory"],
        s["life"],
        target_date=args.date,
        write_voice=not args.no_voice,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    db.close()
    host = args.host or str(config.get("web.host", "127.0.0.1"))
    raw_port = args.port if args.port is not None else config.get("web.port", 8765)
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        print(f"error: invalid web.port: {raw_port!r}", file=sys.stderr)
        return 2
    from .web.server import PortRangeExhausted, run_server

    try:
        run_server(
            config,
            host,
            port,
            args.token,
            open_browser=args.open_browser,
            allow_remote=args.allow_remote,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except PortRangeExhausted as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot bind {host}:{port}: {exc}", file=sys.stderr)
        return 2
    except OverflowError:
        print(f"error: invalid port: {port}", file=sys.stderr)
        return 2
    return 0


def cmd_bilibili_ingest(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    subtitle = args.subtitle or ""
    if args.subtitle_file:
        subtitle = Path(args.subtitle_file).read_text(encoding="utf-8")
    card = s["bilibili"].ingest_video(
        bvid=args.bvid,
        title=args.title,
        up=args.up,
        subtitle=subtitle,
        url=args.url,
        topics=args.topic or [],
        hot_score=args.hot_score,
    )
    print(json.dumps(asdict(card), ensure_ascii=False, indent=2))
    return 0


def cmd_bilibili_status(args: argparse.Namespace) -> int:
    config, _db = _bootstrap(args)
    client = BilibiliClient.from_config(config)
    try:
        nav = client.nav()
    except BilibiliError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "logged_in": bool(nav.get("is_login")),
                "mid": nav.get("mid"),
                "uname": nav.get("uname") or "",
                "level": nav.get("level"),
                "vip": bool(nav.get("vip")),
                "cookie_loaded": bool(client.cookies),
                "cookie_path": str(client.cookie_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_bilibili_login(args: argparse.Namespace) -> int:
    config, _db = _bootstrap(args)
    client = BilibiliClient.from_config(config)
    try:
        if args.cookie:
            client.login_with_cookie(args.cookie)
            nav = client.nav()
            print(
                json.dumps(
                    {
                        "ok": bool(nav.get("is_login")),
                        "logged_in": bool(nav.get("is_login")),
                        "mid": nav.get("mid"),
                        "uname": nav.get("uname") or "",
                        "cookie_path": str(client.cookie_path),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if nav.get("is_login") else 1

        qr = client.qr_generate()
        print(f"QR URL: {qr['url']}")
        image_path = _write_qr_image(qr["url"], args.qr_file, config)
        if image_path:
            print(f"QR image: {image_path}")

        deadline = time.time() + max(10, int(args.timeout))
        last_status = ""
        while time.time() < deadline:
            result = client.qr_poll(qr["qrcode_key"])
            status = str(result.get("status") or "error")
            if status != last_status:
                print(f"[qr] {status}: {result.get('message') or ''}", flush=True)
                last_status = status
            if status == "success":
                nav = client.nav()
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "logged_in": bool(nav.get("is_login")),
                            "mid": nav.get("mid"),
                            "uname": nav.get("uname") or "",
                            "cookie_path": str(client.cookie_path),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if status in {"expired", "error"}:
                return 1
            time.sleep(2)
        print("QR login timed out")
        return 1
    except BilibiliError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


def _write_qr_image(url: str, qr_file: str | None, config: Any) -> str | None:
    try:
        import qrcode  # optional dependency: pip install qrcode
    except Exception:
        print("qrcode is not installed; scan the URL above (pip install qrcode).")
        return None
    target = Path(qr_file) if qr_file else Path(config.data_dir) / "bilibili_qr.png"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        qrcode.make(url).save(str(target))
    except Exception as exc:
        print(f"could not write QR image: {type(exc).__name__}: {exc}")
        return None
    return str(target)


def cmd_bilibili_browse(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    client = BilibiliClient.from_config(config)
    try:
        if args.dry_run:
            videos = client.browse(limit=args.limit, keyword=args.keyword)
            print(json.dumps(videos, ensure_ascii=False, indent=2))
            return 0
        cards = s["bilibili"].browse_once(
            limit=args.limit,
            keyword=args.keyword,
            client=client,
        )
        print(json.dumps([asdict(card) for card in cards], ensure_ascii=False, indent=2))
        return 0
    except BilibiliError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


def cmd_qzone_status(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    collector = QzoneCollector(MemoryService(db, config), config)
    payload = collector.status()
    payload["browser_login"] = _qzone_browser_status(config)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _qzone_browser_status(config: Any) -> dict[str, Any]:
    """免扫码续期的环境探针：只看有没有浏览器，绝不启动它。"""
    from .qzone.browser_login import BrowserLogin, browser_available, profile_size

    login = BrowserLogin(config)
    return {
        "available": browser_available(),
        "profile": str(login.profile_dir),
        "profile_bytes": profile_size(login.profile_dir),
    }


def cmd_qzone_login(args: argparse.Namespace) -> int:
    config, _db = _bootstrap(args)
    if getattr(args, "browser", False):
        return _cmd_qzone_browser(args, config)
    if args.qr:
        return _cmd_qzone_login_qr(args, config)
    if not args.cookie:
        print(
            json.dumps(
                {"ok": False, "error": "specify --cookie <string> or --qr"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    client = QzoneClient.from_config(config)
    try:
        cookies = client.login_with_cookie(args.cookie)
    except QzoneError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    # Only field names are echoed: cookie values never reach stdout or logs.
    print(
        json.dumps(
            {
                "ok": True,
                "saved": sorted(cookies),
                "cookie_path": str(client.cookie_path),
                "full_read": client.cookie_status()["full_read"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_qzone_login_qr(args: argparse.Namespace, config: Any) -> int:
    """`fatfish qzone login --qr`：扫码登录（无界面环境的兜底路径）。

    只输出状态行/字段名/掩码 uin；cookie 值、qrsig、check_sig URL 永不出现在
    stdout。生成后尝试用系统默认看图程序打开 PNG（失败不影响轮询）。
    """
    from .qzone.qr_login import QrLogin, QrLoginError

    login = QrLogin(config=config)
    if args.qr_file:
        login.png_path = Path(args.qr_file)
    try:
        code = login.run(timeout=args.timeout, emit=print, open_image=True)
    except QrLoginError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    if code == 0:
        client = login.client
        print(
            json.dumps(
                {
                    "ok": True,
                    "saved": sorted(login.last_captured),
                    "uin_masked": _mask_uin_safe(login.last_captured.get("uin", "")),
                    "cookie_path": str(client.cookie_path),
                    "full_read": client.cookie_status()["full_read"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return code


def _mask_uin_safe(uin: str) -> str:
    from .qzone.qr_login import mask_uin

    return mask_uin(uin)


def _cmd_qzone_browser(args: argparse.Namespace, config: Any) -> int:
    """`fatfish qzone login --browser` / `qzone refresh`：借本机浏览器续期。"""
    from .qzone.browser_login import BrowserLogin, BrowserLoginError

    headless = bool(getattr(args, "headless", False))
    timeout = float(getattr(args, "timeout", 300) or 300)
    if headless:
        # 无人值守时不该等十分钟：profile 里没登录态就快速失败，让人去开窗口。
        timeout = min(timeout, 120.0)
    login = BrowserLogin(
        config,
        browser_name=getattr(args, "browser_name", None),
        browser_path=getattr(args, "browser_path", None),
        profile_dir=getattr(args, "profile", None),
        timeout=timeout,
    )
    try:
        result = login.run(
            headless=headless,
            keep_open=bool(getattr(args, "keep_open", False)),
            verify_online=not bool(getattr(args, "no_verify", False)),
            true_headless=bool(getattr(args, "true_headless", False)),
        )
    except BrowserLoginError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


def cmd_qzone_refresh(args: argparse.Namespace) -> int:
    """`fatfish qzone refresh`：headless 续期，失败就退回手动窗口。"""
    config, _db = _bootstrap(args)
    setattr(args, "headless", not bool(getattr(args, "headful", False)))
    return _cmd_qzone_browser(args, config)


def cmd_qzone_fetch(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    collector = QzoneCollector(MemoryService(db, config), config)
    try:
        result = collector.fetch_and_ingest(manual=True)
    except QzoneError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


def cmd_qzone_backfill_entities(args: argparse.Namespace) -> int:
    """T-0013: re-sync stored Zone authors into the entity layer (idempotent)."""
    from .qzone.collector import backfill_qzone_entities

    config, db = _bootstrap(args)
    result = backfill_qzone_entities(db)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


def cmd_qzone_show(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    collector = QzoneCollector(MemoryService(db, config), config)
    items = collector.recent_items(limit=args.limit, include_friend=args.friend)
    # Local display is not bound by friends_content_to_llm -- nothing here is
    # sent anywhere. raw_json stays out of the output by construction.
    print(json.dumps({"count": len(items), "items": items}, ensure_ascii=False, indent=2))
    return 0


def cmd_qzone_resume(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    collector = QzoneCollector(MemoryService(db, config), config)
    collector.resume()
    print(json.dumps({"ok": True, "breaker_open": False}, ensure_ascii=False, indent=2))
    return 0


def cmd_qzone_doctor(args: argparse.Namespace) -> int:
    config, _db = _bootstrap(args)
    client = QzoneClient.from_config(config)
    report = client.doctor(online=not args.offline)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("ok") else 1


def cmd_qzone_forget(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    collector = QzoneCollector(MemoryService(db, config), config)
    if not args.all and not args.external_id:
        print(
            json.dumps(
                {"ok": False, "error": "specify --external-id <id> or --all"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    removed = collector.forget(args.external_id, all_items=args.all)
    print(json.dumps({"ok": True, "removed": removed}, ensure_ascii=False, indent=2))
    return 0


def _qzone_post_service(config: Any, db: Database) -> "QzonePostService":
    from .qzone import QzonePostService

    return QzonePostService(db, config, outbox=Outbox(db))


def cmd_qzone_posts(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    service = _qzone_post_service(config, db)
    try:
        items = service.list_posts(status=args.status, limit=args.limit)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "count": len(items),
                "items": items,
                "write": service.status(),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_qzone_approve(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    service = _qzone_post_service(config, db)
    try:
        outcome = service.review_post(args.post_id, "approved")
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    if outcome is None:
        print(json.dumps({"ok": False, "error": "post not found"}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(outcome, ensure_ascii=False, indent=2, default=str))
    return 0 if outcome.get("ok") else 1


def cmd_qzone_reject(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    service = _qzone_post_service(config, db)
    try:
        outcome = service.review_post(args.post_id, "rejected")
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    if outcome is None:
        print(json.dumps({"ok": False, "error": "post not found"}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(outcome, ensure_ascii=False, indent=2, default=str))
    return 0 if outcome.get("ok") else 1


def _ai_chat_on_sent(runtime: Any):
    """Post-send hook for ``ai_chat_message`` (T-0048).

    Without this the message goes out but the desire threshold never lifts,
    so she would keep talking at the initial threshold forever.  The callback
    runs only after the platform accepted the send (see
    ``QQAdapter._sent``), which is exactly the contract ``record_sent`` wants:
    a failed delivery costs nothing.  Returns ``None`` when the feature is off
    so the adapter simply has no hook.
    """
    service = getattr(runtime, "ai_chat", None)
    if service is None or not getattr(service.settings, "active", False):
        return None

    def _record(payload: dict[str, Any], result: dict[str, Any]) -> None:
        service.record_sent(
            (payload or {}).get("text", ""),
            desire=(payload or {}).get("desire"),
            threshold=(payload or {}).get("threshold"),
            direction="out",
            turn_id=(payload or {}).get("turn_id"),
        )

    return _record


def cmd_remote_status(args: argparse.Namespace) -> int:
    """`fatfish remote status`：家里能力层在不在线（云上运维用）。"""
    from .remote import build_probe

    config, _db = _bootstrap(args)
    probe = build_probe(config)
    if probe is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "enabled": False,
                    "hint": "在 [remote] 里设 enabled = true，并给 endpoints（或 home_url）",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    if getattr(args, "force", False):
        for name in list(probe.endpoints):
            probe.check(name, force=True)
    report = probe.status()
    report["enabled"] = True
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("home_online") else 1


def cmd_qq_status(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    runtime = Runtime(db, config)
    adapter = QQAdapter(runtime, config, on_sent=_ai_chat_on_sent(runtime))
    print(json.dumps(adapter.status(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_qq_run(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    if not websocket_available():
        print(
            "error: websocket-client is required for QQ/OneBot. "
            "Install it with: pip install websocket-client",
            file=sys.stderr,
        )
        return 2
    # Two adapters would both answer the same message and both write to the
    # outbox.  The pid file is only a hint (it can be stale, and psutil is an
    # optional dependency); an OS lock is the actual single-instance guard.
    lock = acquire(lock_path(config.data_dir, "qq-adapter"))
    if lock is None:
        print(
            "error: another QQ adapter is already running "
            f"(lock: {lock_path(config.data_dir, 'qq-adapter')})",
            file=sys.stderr,
        )
        return 1
    runtime = Runtime(db, config)
    adapter = QQAdapter(runtime, config, on_sent=_ai_chat_on_sent(runtime))
    # T-0072: the runtime's read_chat branch reads through this adapter's
    # fault-tolerant fetch_* APIs; without the back-reference the action
    # refuses with ``qq_unavailable`` instead of guessing a transport.
    runtime.qq = adapter
    if not adapter.enabled:
        print("[qq] warning: qq.enabled=false; starting because you asked explicitly")
    try:
        adapter.run_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        lock.release()
    return 0


def cmd_qq_send(args: argparse.Namespace) -> int:
    config = load_config(args.profile, args.config)
    if not websocket_available():
        print(
            "error: websocket-client is required for QQ/OneBot. "
            "Install it with: pip install websocket-client",
            file=sys.stderr,
        )
        return 2
    section = config.section("qq")
    ws_url = str(section.get("ws_url", "ws://127.0.0.1:3001"))
    token = env_secret(str(section.get("access_token_env", "QQ_ACCESS_TOKEN")))
    client = OneBotClient(ws_url, access_token=token, timeout=5)
    try:
        client.connect()
        if args.group_id:
            echo = client.send_group_text(args.group_id, args.text)
            target_type, target_id = "group", args.group_id
        else:
            echo = client.send_private_text(args.private_id, args.text)
            target_type, target_id = "private", args.private_id
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    finally:
        client.close()
    print(
        json.dumps(
            {
                "ok": True,
                "ws_url": ws_url,
                "target_type": target_type,
                "target_id": str(target_id),
                "echo": echo,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_lexicon_list(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    lexicon = LexiconService(db, config)
    status = None if args.status in {None, "all"} else args.status
    try:
        items = lexicon.list(status=status, query=args.query, limit=args.limit)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for item in items:
        meaning = item["meaning"] or item["situation"] or ""
        suffix = f" —— {meaning}" if meaning else ""
        print(
            f"{item['id']}  [{item['status']}/{item['kind']}]  "
            f"{item['content']}{suffix}"
        )
    print(f"-- {len(items)} entries")
    return 0


def cmd_lexicon_stats(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    lexicon = LexiconService(db, config)
    print(json.dumps(lexicon.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_lexicon_mine(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    lexicon = LexiconService(db, config)
    result = mine(
        lexicon,
        days=args.days,
        limit_turns=args.limit_turns,
        min_count=args.min_count,
    )
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


def cmd_lexicon_review(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    lexicon = LexiconService(db, config)
    status = {"approve": "approved", "disable": "disabled", "enable": "approved"}[
        args.lexicon_command
    ]
    item = lexicon.review(args.id, status)
    if item is None:
        print(
            json.dumps(
                {"ok": False, "error": "not_found", "id": args.id},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps({"ok": True, "item": item}, ensure_ascii=False, indent=2))
    return 0


def cmd_lexicon_add(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    lexicon = LexiconService(db, config)
    try:
        item = lexicon.add(
            args.kind,
            args.content,
            meaning=args.meaning or "",
            situation=args.situation or "",
            style=args.style or "",
            tags=args.tag or [],
            status=args.status,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "item": item}, ensure_ascii=False, indent=2))
    return 0


def cmd_tts_status(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    print(json.dumps(s["tts"].status(), ensure_ascii=False, indent=2))
    return 0


def cmd_tts_synth(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    result = s["tts"].synthesize(args.text, emotion=args.emotion)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def cmd_tts_clear_cache(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    s = _services(config, db)
    removed = s["tts"].clear_cache()
    print(json.dumps({"ok": True, "removed": removed}, ensure_ascii=False, indent=2))
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    target = config.backup_dir / f"fatfish-{time.strftime('%Y%m%d-%H%M%S')}.db"
    db.backup_to(target)
    print(f"backup: {target}")
    return 0


def cmd_security_status(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    try:
        kernel = load_kernel(config)
        kernel.ensure_meta(db)
        intact = kernel.verify_file()
    except KernelError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    policy = PolicyEngine(config)
    traces = TraceRecorder(db).summary(limit=100)
    report = {
        "ok": intact,
        "kernel": kernel.to_dict(),
        "kernel_intact": intact,
        "schema_version": db.get_meta("schema_version", "0"),
        "block_hostile_inputs": bool(config.get("security.block_hostile_inputs", True)),
        "redact_outbound": bool(config.get("security.redact_outbound", True)),
        "actions": {
            name: {
                "min_trust": spec.min_trust.name.lower(),
                "max_calls": spec.max_calls,
                "allow_group": spec.allow_group,
                "write": spec.write,
            }
            for name, spec in policy.specs.items()
        },
        "traces": traces,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if intact else 1


def cmd_trace_last(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    rows = TraceRecorder(db).last(limit=args.limit)
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    config, db = _bootstrap(args)
    result = db.migrate()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _without_embedding(payload: dict[str, Any]) -> dict[str, Any]:
    clone = dict(payload)
    clone.pop("embedding", None)
    return clone


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fatfish", description="Low-profile companion core")
    parser.add_argument("--profile", default=None, help="low / standard / remote")
    parser.add_argument("--config", default=None, help="extra TOML file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="initialize data dir and database").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="check environment and resources").set_defaults(func=cmd_doctor)
    sub.add_parser("state", help="show current state").set_defaults(func=cmd_state)
    sub.add_parser("demo", help="run a smoke demo without an API key").set_defaults(func=cmd_demo)
    sub.add_parser("backup", help="backup the SQLite database").set_defaults(func=cmd_backup)
    sub.add_parser("migrate", help="run idempotent schema migrations").set_defaults(func=cmd_migrate)

    owner = sub.add_parser("owner", help="explicit owner binding (认主)")
    owner_sub = owner.add_subparsers(dest="owner_command", required=True)
    owner_sub.add_parser("status", help="show the current owner binding").set_defaults(
        func=cmd_owner_status
    )
    p = owner_sub.add_parser("bind", help="bind one private session as the owner")
    p.add_argument("--session", required=True, help="e.g. qq:private:10001")
    p.add_argument(
        "--name",
        default=None,
        help="display name (default: relationship.owner_display_name)",
    )
    p.set_defaults(func=cmd_owner_bind)
    owner_sub.add_parser("unbind", help="remove the owner binding").set_defaults(
        func=cmd_owner_unbind
    )

    friend = owner_sub.add_parser("friend", help="the single friend slot (好友位)")
    friend_sub = friend.add_subparsers(dest="friend_command", required=True)
    friend_sub.add_parser("status", help="show the friend binding").set_defaults(
        func=cmd_owner_friend_status
    )
    p = friend_sub.add_parser("bind", help="bind one private session as the friend")
    p.add_argument("--session", required=True, help="e.g. qq:private:10002")
    p.add_argument(
        "--name",
        default=None,
        help="display name (default: relationship.friend_display_name)",
    )
    p.set_defaults(func=cmd_owner_friend_bind)
    friend_sub.add_parser("unbind", help="remove the friend binding").set_defaults(
        func=cmd_owner_friend_unbind
    )

    secret = owner_sub.add_parser("secret", help="things never to reveal (保密清单)")
    secret_sub = secret.add_subparsers(dest="secret_command", required=True)
    p = secret_sub.add_parser("add", help="add a confidential note")
    p.add_argument("text", help="what must never be revealed to others")
    p.set_defaults(func=cmd_owner_secret_add)
    p = secret_sub.add_parser("list", help="list confidential notes")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_owner_secret_list)
    p = secret_sub.add_parser("remove", help="remove a confidential note by id")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(func=cmd_owner_secret_remove)

    p = sub.add_parser(
        "owner-role",
        help="family / teacher bindings（T-0073：可多持有人，按 session 增删）",
    )
    role_sub = p.add_subparsers(dest="role_command", required=True)
    p2 = role_sub.add_parser("bind", help="bind one private session as family/teacher")
    p2.add_argument("--session", required=True, help="e.g. qq:private:10003")
    p2.add_argument(
        "--role",
        required=True,
        choices=("family", "teacher"),
        help="family = 家人（妈妈），teacher = 老师（可多持有人）",
    )
    p2.add_argument("--name", default=None, help="display name, e.g. 妈妈")
    p2.set_defaults(func=cmd_owner_role_bind)
    p2 = role_sub.add_parser("unbind", help="remove one family/teacher binding")
    p2.add_argument("--session", required=True)
    p2.add_argument("--role", required=True, choices=("family", "teacher"))
    p2.set_defaults(func=cmd_owner_role_unbind)

    p = sub.add_parser("security", help="identity kernel and action policy status")
    security_sub = p.add_subparsers(dest="security_command", required=True)
    security_sub.add_parser("status").set_defaults(func=cmd_security_status)

    p = sub.add_parser("trace", help="per-turn traces")
    trace_sub = p.add_subparsers(dest="trace_command", required=True)
    p = trace_sub.add_parser("last")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_trace_last)

    mem = sub.add_parser("memory", help="memory operations")
    mem_sub = mem.add_subparsers(dest="memory_command", required=True)
    p = mem_sub.add_parser("add")
    p.add_argument("text")
    p.add_argument("--type", default="episodic")
    p.add_argument("--importance", type=float, default=None)
    p.add_argument("--event-time", default=None)
    p.add_argument("--tag", action="append")
    p.add_argument("--entity", action="append")
    p.add_argument("--source-type", default="cli")
    p.add_argument("--source-id", default=None)
    p.add_argument("--evidence", default=None)
    p.set_defaults(func=cmd_memory_add)
    p = mem_sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_memory_search)

    p = mem_sub.add_parser(
        "audience",
        help="flip one memory row's visibility (T-0073: open=可对外, owner=仅主人)",
    )
    p.add_argument("id", help="memory id")
    p.add_argument("value", choices=("open", "owner"))
    p.set_defaults(func=cmd_memory_audience)

    p = mem_sub.add_parser("details")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_memory_details)
    p = mem_sub.add_parser("list")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_memory_list)
    p = mem_sub.add_parser("meta")
    p.add_argument("--limit", type=int, default=12)
    p.set_defaults(func=cmd_memory_meta)
    p = mem_sub.add_parser("episodes")
    p.add_argument("--date", default=None, help="filter by local date YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_memory_episodes)
    p = mem_sub.add_parser("recall")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--quotes", action="store_true", help="include exact quotes")
    p.set_defaults(func=cmd_memory_recall)

    p = mem_sub.add_parser("entities", help="match a query or list all entities with mention counts")
    p.add_argument("query", nargs="?", default=None, help="optional query text to match entities")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_memory_entities)

    p = mem_sub.add_parser(
        "edges",
        help="实体关系边（co_mention）：谁和谁在同一条细节里一起出现过",
    )
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_memory_edges)

    p = mem_sub.add_parser(
        "person",
        help="人物卡：建卡 / 改名 / 别名 / 合并 / 查重（人名 ↔ QQ号）",
    )
    person_sub = p.add_subparsers(dest="person_command", required=True)

    p = person_sub.add_parser("add", help="create or refresh one person card")
    p.add_argument("name", help="display name (remark / nickname)")
    p.add_argument("--uin", default="", help="QQ account id; omit while unknown")
    p.add_argument("--alias", action="append", default=[], help="extra name (repeatable)")
    p.add_argument("--relation", default=None, help="e.g. 同学 / 同事")
    p.add_argument("--remark", default=None, help="free-form note")
    p.add_argument(
        "--full-uin", action="store_true", help="print the account id unmasked"
    )
    p.set_defaults(func=cmd_memory_person_add)

    p = person_sub.add_parser("list", help="list live person cards")
    p.add_argument("--query", default="", help="name / alias substring, or a full uin")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--full-uin", action="store_true")
    p.set_defaults(func=cmd_memory_person_list)

    p = person_sub.add_parser("show", help="show one card (id / name / alias / uin)")
    p.add_argument("selector")
    p.add_argument("--full-uin", action="store_true")
    p.set_defaults(func=cmd_memory_person_show)

    p = person_sub.add_parser("rename", help="rename a card; the old name stays an alias")
    p.add_argument("selector")
    p.add_argument("name")
    p.add_argument("--full-uin", action="store_true")
    p.set_defaults(func=cmd_memory_person_rename)

    p = person_sub.add_parser("alias", help="add or remove one alias")
    p.add_argument("selector")
    p.add_argument("action", choices=["add", "remove"])
    p.add_argument("alias")
    p.add_argument("--full-uin", action="store_true")
    p.set_defaults(func=cmd_memory_person_alias)

    p = person_sub.add_parser(
        "merge",
        help="merge SOURCE into TARGET (mentions stay, aliases move, merged_into set)",
    )
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--full-uin", action="store_true")
    p.set_defaults(func=cmd_memory_person_merge)

    p = person_sub.add_parser("duplicates", help="list same-name card pairs (suggestions only)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--full-uin", action="store_true")
    p.set_defaults(func=cmd_memory_person_duplicates)

    p = person_sub.add_parser(
        "sync", help="mirror the owner/friend bindings onto their cards"
    )
    p.add_argument("--full-uin", action="store_true")
    p.set_defaults(func=cmd_memory_person_sync)

    p = mem_sub.add_parser(
        "backfill",
        help="backfill entities and tags (对当前库做增量回填，幂等；只新增不删除)",
    )
    p.set_defaults(func=cmd_memory_backfill)

    p = sub.add_parser("compact", help="run nightly memory compaction")
    p.add_argument("--apply", action="store_true", help="write changes; default dry-run")
    p.set_defaults(func=cmd_compact)

    wake = sub.add_parser("wakeup", help="wakeup operations")
    wake_sub = wake.add_subparsers(dest="wakeup_command", required=True)
    p = wake_sub.add_parser("schedule")
    p.add_argument("when")
    p.add_argument("--intent", default="think")
    p.add_argument("--reason", default="")
    p.add_argument("--session", default="default")
    p.add_argument("--urgent", action="store_true")
    p.set_defaults(func=cmd_wakeup_schedule)
    p = wake_sub.add_parser("list")
    p.add_argument("--status", default=None)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_wakeup_list)
    p = wake_sub.add_parser("due")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_wakeup_due)
    p = wake_sub.add_parser("fire")
    p.add_argument("id")
    p.set_defaults(func=cmd_wakeup_fire)

    p = sub.add_parser("voice", help="voice/text decision")
    voice_sub = p.add_subparsers(dest="voice_command", required=True)
    p = voice_sub.add_parser("decide")
    p.add_argument("text")
    p.add_argument("--session", default="default")
    p.add_argument("--group", action="store_true")
    p.add_argument("--request", choices=["voice", "text"], default=None)
    p.set_defaults(func=cmd_voice_decide)

    tts = sub.add_parser("tts", help="local TTS adapter")
    tts_sub = tts.add_subparsers(dest="tts_command", required=True)
    p = tts_sub.add_parser("status")
    p.set_defaults(func=cmd_tts_status)
    p = tts_sub.add_parser("synth")
    p.add_argument("text")
    p.add_argument("--emotion", default=None)
    p.set_defaults(func=cmd_tts_synth)
    p = tts_sub.add_parser("clear-cache")
    p.set_defaults(func=cmd_tts_clear_cache)

    sticker = sub.add_parser("sticker", help="sticker operations")
    sticker_sub = sticker.add_subparsers(dest="sticker_command", required=True)
    p = sticker_sub.add_parser("add")
    p.add_argument("file")
    p.add_argument("--caption", required=True)
    p.add_argument("--tag", action="append")
    p.add_argument("--intent", action="append")
    p.add_argument("--intensity", type=float, default=2.0)
    p.add_argument("--source", default="library")
    p.set_defaults(func=cmd_sticker_add)
    p = sticker_sub.add_parser("search")
    p.add_argument("need")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_sticker_search)
    p = sticker_sub.add_parser("decide")
    p.add_argument("need")
    p.add_argument("--session", default="default")
    p.set_defaults(func=cmd_sticker_decide)

    p = sub.add_parser("context", help="prompt assembly")
    context_sub = p.add_subparsers(dest="context_command", required=True)
    p = context_sub.add_parser("preview")
    p.add_argument("message")
    p.add_argument("--session", default="default")
    p.add_argument("--recall", action="store_true")
    p.set_defaults(func=cmd_context_preview)

    p = sub.add_parser("chat", help="handle one user message through the runtime")
    p.add_argument("message")
    p.add_argument("--session", default="default")
    p.add_argument("--group", action="store_true")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("tick", help="run one runtime tick")
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("run", help="run the polling runtime")
    p.add_argument("--once", action="store_true")
    p.add_argument("--poll", type=int, default=30)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("nightly", help="archive the previous local day and compact")
    p.add_argument(
        "--date",
        default=None,
        help="local date YYYY-MM-DD to archive (default: previous local day)",
    )
    p.add_argument("--dry-run", action="store_true", help="preview archive and compaction")
    p.add_argument(
        "--no-voice",
        action="store_true",
        help="never call the LLM (reuses a stored narrative if present)",
    )
    p.set_defaults(func=cmd_nightly)

    p = sub.add_parser("serve", help="serve the local web UI and JSON API")
    p.add_argument("--host", default=None, help="bind address (default: config web.host)")
    p.add_argument("--port", type=int, default=None, help="bind port (default: config web.port)")
    p.add_argument("--token", default=None, help="fixed access token (default: generated)")
    p.add_argument("--open", action="store_true", dest="open_browser", help="open the browser")
    p.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow binding a non-loopback address",
    )
    p.set_defaults(func=cmd_serve)

    bili = sub.add_parser("bilibili", help="read-only Bilibili ingestion")
    bili_sub = bili.add_subparsers(dest="bilibili_command", required=True)
    p = bili_sub.add_parser("ingest")
    p.add_argument("--bvid", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--up", default="")
    p.add_argument("--subtitle", default="")
    p.add_argument("--subtitle-file", default=None)
    p.add_argument("--url", default="")
    p.add_argument("--topic", action="append")
    p.add_argument("--hot-score", type=float, default=0.5)
    p.set_defaults(func=cmd_bilibili_ingest)

    p = bili_sub.add_parser("login", help="QR or cookie login (cookies stay local)")
    p.add_argument("--cookie", default=None, help="cookie string: SESSDATA=...; bili_jct=...")
    p.add_argument("--qr-file", default=None, help="write the QR PNG here")
    p.add_argument("--timeout", type=int, default=180, help="QR polling timeout seconds")
    p.set_defaults(func=cmd_bilibili_login)

    p = bili_sub.add_parser("status", help="show Bilibili login state")
    p.set_defaults(func=cmd_bilibili_status)

    p = bili_sub.add_parser("browse", help="fetch popular/search videos (read-only)")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--keyword", default=None)
    p.add_argument("--dry-run", action="store_true", help="print without ingesting")
    p.set_defaults(func=cmd_bilibili_browse)

    qz = sub.add_parser("qzone", help="QQ Zone feed ingestion and shuoshuo write path")
    qz_sub = qz.add_subparsers(dest="qzone_command", required=True)
    qz_sub.add_parser("status", help="show QQ Zone cookie/breaker/store state").set_defaults(
        func=cmd_qzone_status
    )
    p = qz_sub.add_parser("login", help="save a pasted cookie or scan a QR code (values stay local)")
    p.add_argument(
        "--cookie",
        default=None,
        help="cookie string: uin=...; skey=...; p_skey=... (required without --qr)",
    )
    p.add_argument(
        "--qr",
        action="store_true",
        help="generate a ptlogin2 QR code and poll until the phone confirms",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="QR login total wait seconds (default 600)",
    )
    p.add_argument(
        "--qr-file",
        default=None,
        help="override the QR PNG path (default data/qq_login_qr.png)",
    )
    p.add_argument(
        "--browser",
        action="store_true",
        help="免扫码续期：借本机 Edge/Chrome 的登录态换新 cookie（推荐）",
    )
    p.add_argument("--browser-name", choices=("edge", "chrome"), default=None)
    p.add_argument("--browser-path", default=None, help="显式指定浏览器可执行文件")
    p.add_argument(
        "--profile",
        default=None,
        help="浏览器 profile 目录（默认 data/run/qzone-browser）",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="无人值守：窗口挪到屏幕外（不是真无头；qzone 会拒绝真无头）",
    )
    p.add_argument(
        "--true-headless",
        action="store_true",
        help="真无头，仅调试用：qzone 会把无头浏览器打回登录页",
    )
    p.add_argument("--keep-open", action="store_true", help="结束后不关浏览器")
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过服务端探测（不推荐：只有服务端说行才会覆盖 cookie 文件）",
    )
    p.set_defaults(func=cmd_qzone_login)
    p = qz_sub.add_parser(
        "refresh",
        help="免扫码续期 cookie（headless；失败时用 login --browser 开窗口）",
    )
    p.add_argument("--browser-name", choices=("edge", "chrome"), default=None)
    p.add_argument("--browser-path", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument(
        "--headful",
        action="store_true",
        help="强制把窗口开到屏幕上（profile 里第一次登录时用）",
    )
    p.add_argument("--keep-open", action="store_true")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--true-headless", action="store_true")
    p.set_defaults(func=cmd_qzone_refresh)
    p = qz_sub.add_parser("fetch", help="poll feeds and ingest into memory (manual)")
    p.set_defaults(func=cmd_qzone_fetch)
    p = qz_sub.add_parser(
        "backfill-entities",
        help="re-sync stored Zone authors into the entity layer (idempotent, additive)",
    )
    p.set_defaults(func=cmd_qzone_backfill_entities)
    p = qz_sub.add_parser("show", help="print stored dynamics from the local DB")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--friend", action="store_true", help="include friend dynamics")
    p.set_defaults(func=cmd_qzone_show)
    qz_sub.add_parser("resume", help="clear the poll circuit breaker").set_defaults(
        func=cmd_qzone_resume
    )
    p = qz_sub.add_parser("doctor", help="check cookie completeness (+ live probe)")
    p.add_argument(
        "--offline",
        action="store_true",
        help="only check cookie fields; skip the network probe",
    )
    p.set_defaults(func=cmd_qzone_doctor)
    p = qz_sub.add_parser("forget", help="delete stored dynamics and their memories")
    p.add_argument("--external-id", default=None, help="one feed item id")
    p.add_argument("--all", action="store_true", help="forget every stored item")
    p.set_defaults(func=cmd_qzone_forget)
    p = qz_sub.add_parser("posts", help="list shuoshuo drafts and the publish ledger")
    p.add_argument(
        "--status",
        default=None,
        help="filter: draft|approved|rejected|posted|failed|blocked",
    )
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_qzone_posts)
    p = qz_sub.add_parser("approve", help="approve one draft and queue it for publishing")
    p.add_argument("post_id", type=int)
    p.set_defaults(func=cmd_qzone_approve)
    p = qz_sub.add_parser("reject", help="reject one draft")
    p.add_argument("post_id", type=int)
    p.set_defaults(func=cmd_qzone_reject)

    rem = sub.add_parser(
        "remote",
        help="capability-tier probe (is the home GPU machine reachable?)",
    )
    rem_sub = rem.add_subparsers(dest="remote_command", required=True)
    p = rem_sub.add_parser("status", help="probe the configured capability endpoints")
    p.add_argument("--force", action="store_true", help="bypass the TTL cache")
    p.set_defaults(func=cmd_remote_status)

    qq = sub.add_parser("qq", help="QQ / OneBot v11 adapter")
    qq_sub = qq.add_subparsers(dest="qq_command", required=True)
    qq_sub.add_parser("status", help="show adapter config and connection state").set_defaults(
        func=cmd_qq_status
    )
    qq_sub.add_parser("run", help="connect to NapCat and serve messages").set_defaults(
        func=cmd_qq_run
    )
    p = qq_sub.add_parser("send", help="send one text message as a connectivity test")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--private", dest="private_id", default=None, help="private user id")
    target.add_argument("--group", dest="group_id", default=None, help="group id")
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_qq_send)

    lex = sub.add_parser("lexicon", help="expression library: list, review, mine, add")
    lex_sub = lex.add_subparsers(dest="lexicon_command", required=True)
    p = lex_sub.add_parser("list", help="list expressions")
    p.add_argument("--status", choices=[*VALID_STATUSES, "all"], default=None)
    p.add_argument("--query", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_lexicon_list)
    lex_sub.add_parser("stats", help="show counts by status/kind/source").set_defaults(
        func=cmd_lexicon_stats
    )
    p = lex_sub.add_parser("mine", help="mine candidate jargon from recent turns")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--min-count", type=int, default=3)
    p.add_argument("--limit-turns", type=int, default=800)
    p.set_defaults(func=cmd_lexicon_mine)
    for name, help_text in (
        ("approve", "approve an entry"),
        ("disable", "disable an entry"),
        ("enable", "re-enable a disabled entry"),
    ):
        p = lex_sub.add_parser(name, help=help_text)
        p.add_argument("id")
        p.set_defaults(func=cmd_lexicon_review)
    p = lex_sub.add_parser("add", help="add a manual entry")
    p.add_argument("--kind", choices=VALID_KINDS, required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--meaning", default=None)
    p.add_argument("--situation", default=None)
    p.add_argument("--style", choices=["", *VALID_STYLES], default=None)
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--status", choices=VALID_STATUSES, default="candidate")
    p.set_defaults(func=cmd_lexicon_add)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
