#!/usr/bin/env python
"""夜间实测驱动：备份/恢复锚点 + 到点唤醒主循环 + 台账/晨报。

    python scripts/night_test.py prepare --config config/night-test.toml
    python scripts/night_test.py run     --config config/night-test.toml [--dry-run] [--once]
    python scripts/night_test.py finalize --config config/night-test.toml
    python scripts/night_test.py summary --config config/night-test.toml

设计要点（T-0015）：

* 零第三方依赖，复用 ``fatfish`` 包；不启动 NapCat/TTS/WS/tick 后台循环，
  不发任何 QQ 消息 —— 夜间产物只有记忆、说说草稿和台账。
* 每轮唤醒：stop 文件检查 → ``QzoneCollector.fetch_and_ingest(manual=True)``
  → ``BilibiliCollector.browse_once(limit=1)`` → 一次完整 runtime 单轮
  （session_id = night-YYYYMMDD-wNN）→ turn_traces token 记账 → 台账一行。
  采集由驱动显式先行，轮次文本只触发「看看空间动态」旁路消化本地证据。
* 刹车四重：stop 文件、窗口 end 时刻、连续 N 轮硬失败（采集+轮次全失败）、
  每轮 1M token 硬顶（从 turn_traces 读 prompt+completion）。明确不做全晚总闸。
* 会话角色：向 owner_bindings 直接 INSERT 一行 ``role='owner'``、
  ``source='night-test'``（不动既有绑定行；``bind()`` 会整行替换所以不能用）。
  run 启动/结束与 finalize 都幂等清理 ``source='night-test'`` 的行。
* 测试日（备份/tag/台账文件名里的日期）默认 = ``(now + 4h).date()``：
  22:30 预备、窗口内 03:00–07:59、08:00 收尾都落在窗口结束的早晨；
  ``--date YYYYMMDD`` 可覆盖。
* 已知漏计：runtime 的 JSON 修复重试那一次 LLM 调用只进全局 metrics，
  不进 turn_traces，因此不计入每轮预算（其余 LLM 调用全部计入）。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fatfish.bilibili import BilibiliCollector  # noqa: E402
from fatfish.clock import from_local, human_time, to_local  # noqa: E402
from fatfish.config import Config, load_config  # noqa: E402
from fatfish.db import Database  # noqa: E402
from fatfish.memory import MemoryService  # noqa: E402
from fatfish.owner import OwnerRegistry  # noqa: E402
from fatfish.qzone import QzoneCollector  # noqa: E402

BINDING_SOURCE = "night-test"
SLEEP_CHUNK_SECONDS = 5.0

DEFAULT_WAKE_TEXT = (
    "夜间自主活动时间。看看空间里有什么新动态，B站也转一下；"
    "想记的事用 remember，值得分享的用 post_qzone 起草说说（要审批，明早主人看），"
    "没有值得一提的就随便聊两句。"
)

# 默认值与 config/night-test.toml 保持一致；TOML 缺节时驱动仍可用。
_DEFAULTS = {
    "interval_minutes": 30,
    "jitter_minutes": 3,
    "start": "23:00",
    "end": "07:00",
    "wake_token_budget": 1_000_000,
    "bilibili_videos_per_wake": 1,
    "review_items_per_wake": 10,
    "session_prefix": "night",
    "ledger_path": "data/logs/night-test-{date}.jsonl",
    "stop_file": "data/run/night-test.stop",
    "consecutive_failure_limit": 3,
}


# --------------------------------------------------------------------- dates
def night_test_date(now: float | None = None, tz_name: str = "Asia/Shanghai") -> str:
    """测试日 = (now + 4h) 的本地日期（窗口结束的早晨）。

    夜间窗口跨零点时，「23:00 预备」和「07:00 收尾」相差 8 小时却属于同一次
    实测；+4h 让 22:30 的 prepare、窗口内任意时刻、08:00 的 finalize 都落
    在同一个日期上。``--date`` 永远可以显式覆盖。
    """
    now = time.time() if now is None else float(now)
    return to_local(now + 4 * 3600, tz_name).date().isoformat()


def compact_date(iso_date: str) -> str:
    return iso_date.replace("-", "")


def normalize_date(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = text.replace("-", "")
    try:
        parsed = datetime.strptime(cleaned, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"--date expects YYYYMMDD or YYYY-MM-DD, got {value!r}") from exc
    return parsed.isoformat()


def _parse_hhmm(text: str) -> tuple[int, int]:
    raw = str(text or "").strip()
    if ":" not in raw:
        raise ValueError(f"expected HH:MM, got {text!r}")
    hour, minute = raw.split(":", 1)
    h, m = int(hour), int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"expected HH:MM, got {text!r}")
    return h, m


def _apply_night_overrides(config: Config) -> Config:
    """把 [night_test].review_items_per_wake 落到 qzone.review_max_items。

    旁路读取的是产品配置键 ``qzone.review_max_items``（runtime._qzone_review_evidence）；
    驱动只改内存中的 Config 数据，不写文件、不动产品代码。
    """
    limit = config.get("night_test.review_items_per_wake")
    if limit is None:
        return config
    data = config.as_dict()
    data.setdefault("qzone", {})["review_max_items"] = max(1, int(limit))
    return Config(data, profile=config.profile, source=config.source)


# -------------------------------------------------------------------- driver
class NightTestDriver:
    """可注入的单进程驱动；测试传 fake collector/runtime/clock 即可离线跑。"""

    def __init__(
        self,
        config: Config,
        *,
        date: str | None = None,
        db: Database | None = None,
        memory: MemoryService | None = None,
        qzone: Any | None = None,
        bili: Any | None = None,
        runtime: Any | None = None,
        repo_root: Path | str | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] | None = None,
        git_runner: Callable[[list[str]], Any] | None = None,
        out: Callable[[str], None] = print,
    ):
        self.config = _apply_night_overrides(config)
        self._date_override = normalize_date(date)
        self._db = db
        self._memory = memory
        self._qzone = qzone
        self._bili = bili
        self._runtime = runtime
        self.repo_root = Path(repo_root) if repo_root else ROOT
        self._clock = clock
        self._sleep = sleep
        self._git_runner = git_runner
        self._out = out
        jitter_minutes = self.setting("jitter_minutes", 3)
        self._jitter = jitter or (
            lambda: random.uniform(-float(jitter_minutes), float(jitter_minutes)) * 60
        )

    # ------------------------------------------------------------- settings
    def setting(self, key: str, default: Any = None) -> Any:
        value = self.config.get(f"night_test.{key}")
        return default if value is None else value

    @property
    def timezone_name(self) -> str:
        return self.config.timezone_name

    @property
    def wake_text(self) -> str:
        return str(self.setting("wake_text", DEFAULT_WAKE_TEXT) or DEFAULT_WAKE_TEXT)

    @property
    def interval_seconds(self) -> float:
        return max(1, int(self.setting("interval_minutes", 30))) * 60

    @property
    def bili_limit(self) -> int:
        return max(1, int(self.setting("bilibili_videos_per_wake", 1)))

    @property
    def token_budget(self) -> int:
        return max(0, int(self.setting("wake_token_budget", 1_000_000)))

    @property
    def failure_limit(self) -> int:
        return max(1, int(self.setting("consecutive_failure_limit", 3)))

    @property
    def session_prefix(self) -> str:
        return str(self.setting("session_prefix", "night") or "night").strip("-")

    def night_date(self, now: float | None = None) -> str:
        if self._date_override:
            return self._date_override
        return night_test_date(now, self.timezone_name)

    def ledger_path_for(self, date_iso: str) -> Path:
        template = str(self.setting("ledger_path", _DEFAULTS["ledger_path"]))
        raw = Path(template.format(date=compact_date(date_iso)))
        if not raw.is_absolute():
            raw = ROOT / raw
        return raw

    @property
    def stop_file(self) -> Path:
        raw = Path(str(self.setting("stop_file", _DEFAULTS["stop_file"])))
        if not raw.is_absolute():
            raw = ROOT / raw
        return raw

    # ------------------------------------------------------- lazy components
    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = Database(self.config.db_path)
        return self._db

    @property
    def memory(self) -> MemoryService:
        if self._memory is None:
            self._memory = MemoryService(self.db, self.config)
        return self._memory

    @property
    def qzone(self) -> QzoneCollector:
        if self._qzone is None:
            self._qzone = QzoneCollector(self.memory, self.config)
        return self._qzone

    @property
    def bili(self) -> BilibiliCollector:
        if self._bili is None:
            self._bili = BilibiliCollector(self.memory, self.config)
        return self._bili

    @property
    def runtime(self) -> Any:
        if self._runtime is None:
            from fatfish.runtime import Runtime

            self._runtime = Runtime(self.db, self.config)
        return self._runtime

    # -------------------------------------------------------- session identity
    def session_id_for(self, wake_no: int, date_iso: str) -> str:
        return f"{self.session_prefix}-{compact_date(date_iso)}-w{wake_no:02d}"

    def ensure_session_binding(self, session_id: str) -> str:
        """给夜间会话一个能看记忆的角色，绝不改动既有绑定行。

        ``bind()`` 会先 DELETE 同角色旧行（等于改掉真实 owner 绑定），所以这里
        直接 INSERT 一行独立记录；``owner.get()`` 的 ``LIMIT 1`` 无 ORDER BY，
        按 rowid 仍返回先插入的原始绑定，运行期行为不变。
        """
        OwnerRegistry(self.db, self.config)  # 确保表存在（幂等）
        existing = self.db.query_one(
            "SELECT role FROM owner_bindings WHERE session_id=? LIMIT 1", (session_id,)
        )
        if existing is not None:
            return str(existing["role"])
        name = str(
            self.config.get("relationship.owner_display_name", "主人哥哥") or "主人哥哥"
        )
        self.db.execute(
            "INSERT INTO owner_bindings("
            "session_id, platform, display_name, bound_at, source, role) "
            "VALUES (?, '', ?, ?, ?, 'owner')",
            (session_id, name, self._clock(), BINDING_SOURCE),
        )
        self._log_decision(
            "night_test_binding",
            {"session_id": session_id, "source": BINDING_SOURCE},
            action="bind",
            outcome="ok",
        )
        return "owner"

    def remove_session_bindings(self) -> int:
        """清掉本驱动创建的绑定行；既有绑定（其他 source）永不动。"""
        OwnerRegistry(self.db, self.config)
        cur = self.db.execute("DELETE FROM owner_bindings WHERE source=?", (BINDING_SOURCE,))
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------- accounting
    def tokens_since(self, session_id: str, since: float) -> dict[str, int]:
        """turn_traces 记账：该会话自 ``since`` 起的 prompt+completion 总量。"""
        row = self.db.query_one(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total, "
            "COUNT(*) AS turns FROM turn_traces WHERE session_id=? AND at >= ?",
            (session_id, float(since)),
        )
        return {
            "tokens": int(row["total"]) if row else 0,
            "turns": int(row["turns"]) if row else 0,
        }

    def drafts_since(self, session_id: str, since: float) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM qzone_posts WHERE session_id=? AND created_at >= ?",
            (session_id, float(since)),
        )
        return int(row["n"]) if row else 0

    # ---------------------------------------------------------------- ledger
    def _read_ledger(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _last_wake_number(self, path: Path) -> int:
        highest = 0
        for row in self._read_ledger(path):
            value = row.get("wake")
            if isinstance(value, int) and value > highest:
                highest = value
        return highest

    def _append_ledger(self, path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def stop_requested(self) -> bool:
        return self.stop_file.exists()

    def _log_decision(self, kind: str, payload: dict[str, Any], *, action: str, outcome: str) -> None:
        try:
            self.memory.log_decision(kind, payload, action=action, outcome=outcome, model="night-test")
        except Exception:  # 遥测失败绝不影响驱动主流程
            pass

    # ------------------------------------------------------------- time window
    def _in_window(self, now: float) -> bool:
        local = to_local(now, self.timezone_name)
        start = _parse_hhmm(str(self.setting("start", "23:00")))
        end = _parse_hhmm(str(self.setting("end", "07:00")))
        minutes = local.hour * 60 + local.minute
        if end > start:
            return start[0] * 60 + start[1] <= minutes < end[0] * 60 + end[1]
        if end < start:  # 跨零点窗口：[start, 24:00) ∪ [00:00, end)
            return minutes >= start[0] * 60 + start[1] or minutes < end[0] * 60 + end[1]
        return True

    def _window_end_moment(self, now: float) -> float:
        """当前（或即将进入的）窗口的结束时刻。"""
        local = to_local(now, self.timezone_name)
        start = _parse_hhmm(str(self.setting("start", "23:00")))
        end = _parse_hhmm(str(self.setting("end", "07:00")))
        end_dt = local.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
        if end >= start:  # 同日窗口
            minutes = local.hour * 60 + local.minute
            if minutes < end[0] * 60 + end[1]:
                return from_local(end_dt, self.timezone_name)
            return from_local(end_dt + timedelta(days=1), self.timezone_name)
        # 跨零点：now 在 [start, 24:00) 段则结束在明天，否则就是今天
        minutes = local.hour * 60 + local.minute
        if minutes >= start[0] * 60 + start[1]:
            return from_local(end_dt + timedelta(days=1), self.timezone_name)
        return from_local(end_dt, self.timezone_name)

    def _next_start_moment(self, now: float) -> float:
        local = to_local(now, self.timezone_name)
        start = _parse_hhmm(str(self.setting("start", "23:00")))
        start_dt = local.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
        if start_dt.timestamp() > now:
            return from_local(start_dt, self.timezone_name)
        return from_local(start_dt + timedelta(days=1), self.timezone_name)

    def _sleep_until(self, target: float) -> bool:
        """分段睡眠并随时响应 stop 文件；False = 请求了停止。"""
        while True:
            now = self._clock()
            remaining = target - now
            if remaining <= 0:
                return True
            if self.stop_requested():
                return False
            self._sleep(min(SLEEP_CHUNK_SECONDS, remaining))

    # ------------------------------------------------------------------ wake
    def run_wake(self, *, wake_no: int, date_iso: str, dry_run: bool = False) -> dict[str, Any]:
        started = self._clock()
        session_id = self.session_id_for(wake_no, date_iso)
        errors: list[str] = []

        qzone_ok, qzone_new = False, 0
        try:
            result = self.qzone.fetch_and_ingest(manual=True)
            qzone_ok = bool(result.get("ok"))
            if qzone_ok:
                qzone_new = int(result.get("new") or 0)
            else:
                reason = result.get("skipped") or result.get("errors") or "failed"
                errors.append(f"qzone:{str(reason)[:150]}")
        except Exception as exc:  # 采集失败记台账，绝不结束整个夜间
            errors.append(f"qzone:{type(exc).__name__}: {exc}"[:200])

        bili_ok, bili_new = False, 0
        try:
            cards = self.bili.browse_once(limit=self.bili_limit)
            bili_ok, bili_new = True, len(cards)
        except Exception as exc:
            errors.append(f"bilibili:{type(exc).__name__}: {exc}"[:200])

        tokens, turns, actions, turn_error = 0, 0, [], None
        if not dry_run:
            self.ensure_session_binding(session_id)
            try:
                result = self.runtime.handle_user_message(session_id, self.wake_text)
                actions = [
                    str(item.get("tool"))
                    for item in (result.get("actions") or [])
                    if item.get("tool")
                ]
            except Exception as exc:  # 轮次异常（含 JSON 修复路径的意外）只记账
                turn_error = f"{type(exc).__name__}: {exc}"[:300]
                errors.append(f"turn:{turn_error}")
            usage = self.tokens_since(session_id, started)
            tokens, turns = usage["tokens"], usage["turns"]

        drafts = self.drafts_since(session_id, started)
        over_budget = bool(self.token_budget and tokens > self.token_budget)
        hard_failed = (not qzone_ok) and (not bili_ok) and (turn_error is not None)

        row = {
            "ts": self._clock(),
            "event": "wake",
            "wake": int(wake_no),
            "date": date_iso,
            "session": session_id,
            "mode": "dry-run" if dry_run else "live",
            "tokens": tokens,
            "trace_turns": turns,
            "qzone_new": qzone_new,
            "bili_new": bili_new,
            "drafts_created": drafts,
            "actions": actions,
            "errors": errors,
            "failed": hard_failed,
            "over_budget": over_budget,
            "duration_s": round(self._clock() - started, 1),
        }
        self._log_decision(
            "night_test_wake",
            {
                "wake": row["wake"],
                "session": session_id,
                "mode": row["mode"],
                "tokens": tokens,
                "qzone_new": qzone_new,
                "bili_new": bili_new,
                "drafts": drafts,
                "errors": errors,
            },
            action="wake",
            outcome="failed" if hard_failed else "ok",
        )
        return row

    # ------------------------------------------------------------------- run
    def run(self, *, dry_run: bool = False, once: bool = False) -> dict[str, Any]:
        date_iso = self.night_date(self._clock())
        ledger_path = self.ledger_path_for(date_iso)
        wake_no = self._last_wake_number(ledger_path)
        failures = 0
        wakes_done = 0
        stop_reason: str | None = None
        next_wake_at: float | None = None
        window_end: float | None = None

        self.remove_session_bindings()  # 清理上次崩溃可能留下的行
        self._log_decision(
            "night_test",
            {"phase": "start", "date": date_iso, "dry_run": dry_run, "once": once},
            action="start",
            outcome="ok",
        )
        try:
            while True:
                now = self._clock()
                if self.stop_requested():
                    stop_reason = "stop_file"
                    break
                if failures >= self.failure_limit:
                    stop_reason = "consecutive_failures"
                    break
                if once:
                    # 冒烟模式：不等窗口，立刻跑一轮（pre-flight 22:30 也要能用）
                    wake_no += 1
                    row = self.run_wake(wake_no=wake_no, date_iso=date_iso, dry_run=dry_run)
                    self._append_ledger(ledger_path, row)
                    wakes_done += 1
                    stop_reason = "once"
                    break
                if self._in_window(now):
                    if window_end is None:
                        window_end = self._window_end_moment(now)
                    if next_wake_at is not None and now < next_wake_at:
                        self._sleep_until(min(next_wake_at, window_end))
                        continue
                    wake_no += 1
                    row = self.run_wake(wake_no=wake_no, date_iso=date_iso, dry_run=dry_run)
                    self._append_ledger(ledger_path, row)
                    wakes_done += 1
                    failures = failures + 1 if row["failed"] else 0
                    if row["over_budget"]:
                        self._out(
                            f"[night-test] wake {row['wake']} over token budget: "
                            f"{row['tokens']} > {self.token_budget}"
                        )
                    next_wake_at = now + self.interval_seconds + self._jitter()
                    continue
                # 窗口外：已经跑过 = 窗口结束硬停；还没跑过 = 等下一次窗口开始
                if wakes_done:
                    stop_reason = "window_end"
                    break
                target = self._next_start_moment(now)
                self._out(
                    f"[night-test] waiting for window start "
                    f"({human_time(target, self.timezone_name)})"
                )
                if not self._sleep_until(target):
                    continue
        finally:
            removed = self.remove_session_bindings()
            if stop_reason is not None:
                self._append_ledger(
                    ledger_path,
                    {
                        "ts": self._clock(),
                        "event": "stop",
                        "date": date_iso,
                        "reason": stop_reason,
                        "wakes": wakes_done,
                        "bindings_removed": removed,
                    },
                )
                self._log_decision(
                    "night_test",
                    {"phase": "stop", "reason": stop_reason, "date": date_iso},
                    action="stop",
                    outcome="ok",
                )
        return {
            "ok": True,
            "date": date_iso,
            "ledger": str(ledger_path),
            "wakes": wakes_done,
            "stop_reason": stop_reason,
            "dry_run": dry_run,
            "once": once,
        }

    # -------------------------------------------------------------- snapshot
    def _git(self, args: list[str]) -> Any:
        runner = self._git_runner or self._default_git_runner
        return runner(["-C", str(self.repo_root), *args])

    @staticmethod
    def _default_git_runner(args: list[str]) -> Any:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )

    def snapshot(self, *, phase: str) -> dict[str, Any]:
        """一致性快照 → git add -f → commit → 本地 tag（绝不 push）。

        phase: "pre"（prepare）或 "post"（finalize）。
        """
        if phase not in ("pre", "post"):
            raise ValueError(f"phase must be pre/post, got {phase!r}")
        date_iso = self.night_date(self._clock())
        compact = compact_date(date_iso)
        db_path = Path(self.config.db_path)
        if not db_path.exists():
            raise FileNotFoundError(f"database not found: {db_path}")
        word = "before" if phase == "pre" else "after"
        target = self.config.backup_dir / f"fatfish-{word}-nighttest-{compact}.db"
        source = Database(db_path)
        try:
            source.backup_to(target)
        finally:
            source.close()

        try:
            rel = os.path.relpath(target, self.repo_root)
        except ValueError:  # 备份目录和仓库不在同一盘符：退回绝对路径，
            rel = str(target)  # 让 git 自己报 "outside repository" 这类可读错误
        rel = rel.replace(os.sep, "/")
        out: dict[str, Any] = {
            "ok": True,
            "phase": phase,
            "date": date_iso,
            "backup": str(target),
            "git": {},
        }
        add = self._git(["add", "-f", rel])
        if add.returncode != 0:
            raise RuntimeError(f"git add failed: {(add.stderr or add.stdout).strip()[:300]}")
        commit_msg = f"chore(backup): {phase}-nighttest snapshot {compact}"
        commit = self._git(["commit", "-m", commit_msg])
        combined = (commit.stdout or "") + (commit.stderr or "")
        if commit.returncode != 0 and "nothing to commit" not in combined:
            raise RuntimeError(f"git commit failed: {combined.strip()[:300]}")
        out["git"]["commit_message"] = commit_msg
        out["git"]["committed"] = commit.returncode == 0
        tag = f"{phase}-nighttest-{compact}"
        listed = self._git(["tag", "--list", tag])
        if tag in str(listed.stdout or "").split():
            out["git"]["tag"] = f"{tag} (already exists)"
        else:
            created = self._git(["tag", tag])
            if created.returncode != 0:
                raise RuntimeError(f"git tag failed: {(created.stderr or '').strip()[:300]}")
            out["git"]["tag"] = tag
        head = self._git(["rev-parse", "--short", "HEAD"])
        out["git"]["head"] = str(head.stdout or "").strip()
        return out

    def prepare(self) -> dict[str, Any]:
        result = self.snapshot(phase="pre")
        self._log_decision("night_test", result["git"], action="prepare", outcome="ok")
        return result

    def finalize(self) -> dict[str, Any]:
        result = self.snapshot(phase="post")
        result["bindings_removed"] = self.remove_session_bindings()
        self._log_decision("night_test", result["git"], action="finalize", outcome="ok")
        return result

    # --------------------------------------------------------------- summary
    def summary(self) -> dict[str, Any]:
        date_iso = self.night_date(self._clock())
        compact = compact_date(date_iso)
        ledger_path = self.ledger_path_for(date_iso)
        rows = self._read_ledger(ledger_path)
        wakes = [row for row in rows if row.get("event") == "wake"]
        stops = [row for row in rows if row.get("event") == "stop"]

        start = _parse_hhmm(str(self.setting("start", "23:00")))
        end = _parse_hhmm(str(self.setting("end", "07:00")))
        local = to_local(self._clock(), self.timezone_name)
        # 台账缺失时的兜底窗口：测试日前一天 start → 测试日 end
        from datetime import datetime as _dt

        day = _dt.strptime(compact, "%Y%m%d").date()
        start_dt = local.replace(
            year=day.year, month=day.month, day=day.day,
            hour=start[0], minute=start[1], second=0, microsecond=0,
        ) - timedelta(days=1)
        end_dt = local.replace(
            year=day.year, month=day.month, day=day.day,
            hour=end[0], minute=end[1], second=0, microsecond=0,
        )
        if wakes:
            window_start = min(float(row.get("ts") or 0) for row in wakes)
            window_end = max(float(row.get("ts") or 0) for row in wakes)
        else:
            window_start = from_local(start_dt, self.timezone_name)
            window_end = from_local(end_dt, self.timezone_name)

        tokens = [int(row.get("tokens") or 0) for row in wakes]
        over_budget_rows = [row for row in wakes if row.get("over_budget")]
        actions: Counter[str] = Counter()
        for row in wakes:
            for tool in row.get("actions") or []:
                actions[str(tool)] += 1
        draft_rows = self.db.query(
            "SELECT id, status, session_id, created_at, content FROM qzone_posts "
            "WHERE created_at >= ? AND created_at <= ? AND session_id LIKE ? "
            "ORDER BY id",
            (
                window_start,
                window_end,
                f"{self.session_prefix}-%",
            ),
        )
        memory_row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM memory_items "
            "WHERE observed_at >= ? AND observed_at <= ? AND status <> 'deleted'",
            (window_start, window_end),
        )
        payload = {
            "ok": True,
            "date": date_iso,
            "ledger": str(ledger_path),
            "ledger_missing": not ledger_path.exists() and not wakes,
            "window": {
                "start_text": human_time(window_start, self.timezone_name),
                "end_text": human_time(window_end, self.timezone_name),
            },
            "wake_count": len(wakes),
            "tokens": {
                "total": sum(tokens),
                "avg": round(sum(tokens) / len(tokens)) if tokens else 0,
                "max": max(tokens) if tokens else 0,
                "budget_per_wake": self.token_budget,
                "over_budget_wakes": [row.get("wake") for row in over_budget_rows],
                "curve": [
                    {"wake": row.get("wake"), "tokens": int(row.get("tokens") or 0)}
                    for row in wakes
                ],
            },
            "qzone_new_total": sum(int(row.get("qzone_new") or 0) for row in wakes),
            "bili_new_total": sum(int(row.get("bili_new") or 0) for row in wakes),
            "drafts_created_total": sum(int(row.get("drafts_created") or 0) for row in wakes),
            "actions": dict(actions),
            "failed_wakes": [row.get("wake") for row in wakes if row.get("failed")],
            "errors_total": sum(len(row.get("errors") or []) for row in wakes),
            "errors_sample": [
                e for row in wakes for e in (row.get("errors") or [])
            ][:20],
            "stops": [
                {"ts": row.get("ts"), "reason": row.get("reason")} for row in stops
            ],
            "drafts": [
                {
                    "id": int(row["id"]),
                    "status": row["status"],
                    "session": row["session_id"],
                    "created_at": row["created_at"],
                    "content": str(row["content"] or "")[:200],
                }
                for row in draft_rows
            ],
            "memory_new_in_window": int(memory_row["n"]) if memory_row else 0,
        }
        self._out(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return payload


# ----------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="night_test",
        description="夜间实测驱动：prepare / run / finalize / summary（详见 docs/branches/ops.md）",
    )
    parser.add_argument("--profile", default=None, help="基础 profile（默认 FATFISH_PROFILE 或 low）")
    parser.add_argument("--config", default=None, help="追加 TOML，例如 config/night-test.toml")
    parser.add_argument(
        "--date",
        default=None,
        help="测试日 YYYYMMDD（默认 = now+4h 的本地日期，即窗口结束的早晨）",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="一致性快照 + git commit + pre-nighttest tag").set_defaults(
        func="prepare"
    )
    p = sub.add_parser("run", help="夜间主循环（到点唤醒）")
    p.add_argument("--dry-run", action="store_true", help="采集+browse+台账，但跳过 LLM 轮")
    p.add_argument("--once", action="store_true", help="只跑一轮冒烟")
    p.set_defaults(func="run")
    sub.add_parser("finalize", help="跑后快照 + post-nighttest tag + 清理夜间绑定").set_defaults(
        func="finalize"
    )
    sub.add_parser("summary", help="读台账+查库，输出晨报原始统计").set_defaults(func="summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.profile, args.config)
    config.ensure_dirs()
    driver = NightTestDriver(config, date=args.date)
    try:
        if args.func == "run":
            result = driver.run(dry_run=bool(args.dry_run), once=bool(args.once))
        elif args.func == "prepare":
            result = driver.prepare()
        elif args.func == "finalize":
            result = driver.finalize()
        else:
            driver.summary()
            return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
