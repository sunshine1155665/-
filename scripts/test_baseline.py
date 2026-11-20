"""全量测试基线报告：跑一次 unittest discover，产出机器可读的基线数字。

用法::

    python scripts/test_baseline.py                        # 只打 stdout 摘要
    python scripts/test_baseline.py --json qa_baseline.json
    python scripts/test_baseline.py --json out.json --md out.md --timeout 3600

设计约束：
- 只用标准库（subprocess 跑 unittest + 文本解析），不引入任何第三方依赖；
- 不写 ``data/``、不读真实数据库；
- 数字只从 unittest 自己的输出里解析，解析不到就报错退出（不猜）。

退出码：0 = 测试全绿且解析成功；1 = 测试有 failure/error；2 = 解析失败或运行异常。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# unittest 的汇总行：Ran 1433 tests in 412.345s
RE_RAN = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s\s*$")
# 全绿：OK / OK (skipped=2) / OK (skipped=2, expected failures=1)
RE_OK = re.compile(r"^OK(?: \((.*)\))?\s*$")
# 有失败：FAILED (failures=1, errors=2, skipped=3)
RE_FAILED = re.compile(r"^FAILED \((.*)\)\s*$")
# 汇总括号里的键值：skipped=2, failures=1 ...
RE_COUNT = re.compile(r"(\w[\w ]*?)=(\d+)")

# verbose 模式下每个用例的描述行：test_foo (module.Class.test_foo)
# 只锚定行首的「名字 (点分路径)」，因为用例自己往 stdout 打印时结果后缀会跑到别的行去
# （例如 "... [web] 127.0.0.1 GET /api/health 401 0ms" 换行后才是 "ok"）。
# 失败块头部 "FAIL: test_foo (...)" 带冒号，不会被这条匹配。
RE_DESC = re.compile(r"^([A-Za-z0-9_]+) \(([A-Za-z0-9_.]+)\)")
# 结果后缀：可能和描述同行，也可能在下一行（用例带 docstring 或被自己的 stdout 打断）
RE_RESULT = re.compile(r"\.\.\. (ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b")
RE_SKIP_REASON = re.compile(r"\.\.\. skipped(?::| )?(.*)$")
# 失败/错误块的头部：FAIL: test_x (mod.Cls.test_x)
RE_BLOCK_HEAD = re.compile(r"^(FAIL|ERROR): (\S+) \((.*)\)\s*$")


def _counts(blob: str) -> dict:
    """把 'failures=1, errors=0' 这类括号内容解析成 dict。"""
    out = {}
    for key, value in RE_COUNT.findall(blob):
        out[key.strip()] = int(value)
    return out


def _normalize_reason(raw: str) -> str:
    reason = raw.strip()
    if reason.startswith("'") and reason.endswith("'") and len(reason) >= 2:
        reason = reason[1:-1]
    reason = reason.strip()
    return reason or "(无原因)"


def parse_unittest_output(text: str) -> dict:
    """解析 unittest -v 的输出。拿不到关键数字时抛 ValueError。"""
    lines = text.splitlines()

    total = None
    reported_seconds = None
    skipped = 0
    failures = 0
    errors = 0
    summary_kind = None

    for line in lines:
        m = RE_RAN.match(line)
        if m:
            total = int(m.group(1))
            reported_seconds = float(m.group(2))
            continue
        m = RE_OK.match(line)
        if m:
            summary_kind = "ok"
            counts = _counts(m.group(1) or "")
            skipped = counts.get("skipped", 0)
            failures = 0
            errors = 0
            continue
        m = RE_FAILED.match(line)
        if m:
            summary_kind = "failed"
            counts = _counts(m.group(1))
            failures = counts.get("failures", 0)
            errors = counts.get("errors", 0)
            skipped = counts.get("skipped", 0)

    if total is None:
        raise ValueError("unittest 输出里找不到 'Ran N tests in Xs' 汇总行")
    if summary_kind is None:
        raise ValueError("unittest 输出里找不到 'OK' / 'FAILED (...)' 汇总结论行")

    per_module: dict[str, int] = {}
    skip_reasons: dict[str, int] = {}
    pending_module = None  # 最近一条描述行还没读到结果后缀时暂存，等下一行

    for line in lines:
        m = RE_DESC.match(line)
        if m:
            module = m.group(2).split(".")[0]
            per_module[module] = per_module.get(module, 0) + 1
            pending_module = module
            hit = RE_RESULT.search(line)
        else:
            hit = RE_RESULT.search(line) if pending_module else None
        if hit is None:
            continue
        if hit.group(1) == "skipped":
            raw = RE_SKIP_REASON.search(line)
            reason = _normalize_reason(raw.group(1) if raw else "")
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        pending_module = None

    # 描述行总和应与 Ran N 一致；不一致说明解析漏了，明确告警而不是静默。
    seen_desc = sum(per_module.values())
    if seen_desc != total:
        print(
            f"警告：从 verbose 输出数到 {seen_desc} 个用例描述行，与 Ran {total} tests 不一致；"
            f"per_module 数字可能不完整",
            file=sys.stderr,
        )
    counted_skips = sum(skip_reasons.values())
    if counted_skips != skipped:
        print(
            f"警告：解析到 {counted_skips} 条 skip 原因，与 skipped={skipped} 不一致",
            file=sys.stderr,
        )

    failed_ids = []
    for line in lines:
        m = RE_BLOCK_HEAD.match(line)
        if m:
            failed_ids.append(m.group(3).strip())

    return {
        "total": total,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        "failed_ids": failed_ids,
        "per_module": dict(sorted(per_module.items())),
        "unittest_reported_seconds": reported_seconds,
    }


def run_suite(timeout: float) -> tuple[dict, str]:
    cmd = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_*.py",
        "-v",
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"错误：全量测试超过 --timeout {timeout:g}s 被中止（命令：{' '.join(cmd)}）"
        )
    elapsed = time.time() - started
    report = parse_unittest_output(proc.stdout)
    report["elapsed_seconds"] = round(elapsed, 2)
    report["exit_code"] = proc.returncode
    return report, proc.stdout


def build_summary(report: dict) -> str:
    parts = [
        f"total={report['total']}",
        f"failures={report['failures']}",
        f"errors={report['errors']}",
        f"skipped={report['skipped']}",
        f"elapsed={report['elapsed_seconds']}s",
        f"modules={len(report['per_module'])}",
    ]
    return "基线：" + " ".join(parts)


def render_md(report: dict) -> str:
    lines = [
        "# 测试基线报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- Python：{report['python_version']}",
        f"- 用例总数：{report['total']}",
        f"- failures：{report['failures']}",
        f"- errors：{report['errors']}",
        f"- skipped：{report['skipped']}",
        f"- 墙钟耗时：{report['elapsed_seconds']}s",
        "",
        "## skip 原因",
        "",
    ]
    if report["skip_reasons"]:
        lines += ["| 原因 | 条数 |", "| --- | --- |"]
        for reason, count in report["skip_reasons"].items():
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("（无 skip）")
    lines += ["", "## 失败用例", ""]
    if report["failed_ids"]:
        lines += [f"- `{fid}`" for fid in report["failed_ids"]]
    else:
        lines.append("（无）")
    lines += ["", "## 各测试文件用例数", "", "| 文件 | 条数 |", "| --- | --- |"]
    for module, count in report["per_module"].items():
        lines.append(f"| {module} | {count} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑全量 unittest 并产出基线报告")
    parser.add_argument("--json", metavar="PATH", help="把 JSON 报告写到该路径")
    parser.add_argument("--md", metavar="PATH", help="把 Markdown 报告写到该路径")
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        metavar="SEC",
        help="整套测试的超时秒数（默认 1800）",
    )
    args = parser.parse_args(argv)

    try:
        report, raw_output = run_suite(args.timeout)
    except ValueError as exc:
        print(f"错误：无法解析 unittest 输出：{exc}", file=sys.stderr)
        return 2

    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python_version": platform.python_version(),
        "total": report["total"],
        "failures": report["failures"],
        "errors": report["errors"],
        "skipped": report["skipped"],
        "elapsed_seconds": report["elapsed_seconds"],
        "skip_reasons": report["skip_reasons"],
        "failed_ids": report["failed_ids"],
        "per_module": report["per_module"],
        "unittest_reported_seconds": report["unittest_reported_seconds"],
        "exit_code": report["exit_code"],
    }

    print(build_summary(report))
    if report["skip_reasons"]:
        for reason, count in report["skip_reasons"].items():
            print(f"  skip x{count}: {reason}")
    if report["failed_ids"]:
        print("失败用例：")
        for fid in report["failed_ids"]:
            print(f"  - {fid}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"JSON 报告：{os.path.abspath(args.json)}")
    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(render_md(report))
        print(f"Markdown 报告：{os.path.abspath(args.md)}")

    if not raw_output.strip():
        print("错误：unittest 没有产生任何输出", file=sys.stderr)
        return 2
    return 0 if (report["failures"] == 0 and report["errors"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
