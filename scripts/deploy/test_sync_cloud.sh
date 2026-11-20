#!/bin/sh
# test_sync_cloud.sh — sync_cloud.sh 的隔离模拟测试（不碰 /opt，不碰真实服务）。
#
# 覆盖四个场景：
#   1. 正常同步：文件更新、多余文件被清理、受保护路径不变、服务重启一次；
#   2. 幂等：同一份包再跑一次 → RESULT: NOCHANGE，且不再重启；
#   3. 坏包：能过预检但让服务起不来 → 自动回滚，树回到同步前，服务恢复健康；
#   4. 残缺包：缺 manifest → 预检拦下，线上零改动、零重启。
#
# 用法： sh scripts/deploy/test_sync_cloud.sh
#       MF_PYTHON=/path/to/python sh scripts/deploy/test_sync_cloud.sh   # Windows/Git Bash
# 依赖：sh / tar / gzip / find / sort / cksum / python（解析 manifest）。

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/sync_cloud.sh"
[ -f "$SCRIPT" ] || { echo "找不到 $SCRIPT" >&2; exit 1; }

PY=${MF_PYTHON:-}
if [ -z "$PY" ]; then
    for candidate in python3 python; do
        command -v "$candidate" >/dev/null 2>&1 && PY=$candidate && break
    done
fi
[ -n "$PY" ] || { echo "找不到 python" >&2; exit 1; }

TMP=$(mktemp -d "${TMPDIR:-/tmp}/fatfish-sync-test.XXXXXX")
trap 'rm -rf "$TMP"' EXIT INT TERM

ROOT="$TMP/root"
BACKUPS="$TMP/backups"
COUNTER="$TMP/restarts"
: > "$COUNTER"
HEALTH="$TMP/health.sh"

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }
check() {  # check <描述> <命令...>
    desc=$1
    shift
    if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}
has_line() {  # has_line <多行文本> <期望子串>
    printf '%s\n' "$1" | grep -qF -- "$2"
}

# ---------------------------------------------------------------- 造 manifest
write_manifest() {  # $1=目录 $2=built_at $3=version
    "$PY" - "$1" "$2" "${3:-0.2.0}" <<'PYEOF'
import hashlib, json, os, sys
root, built_at, version = sys.argv[1], sys.argv[2], sys.argv[3]
skip_dirs = {"data", ".venv", "__pycache__", ".git"}
entries = []
for base, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if d not in skip_dirs]
    for name in files:
        if name in ("manifest.json",) or name.endswith((".pyc", ".zip")):
            continue
        full = os.path.join(base, name)
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        blob = open(full, "rb").read()
        entries.append({"path": rel, "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)})
entries.sort(key=lambda item: item["path"])
manifest = {"name": "fatfish-app", "version": version, "built_at": built_at, "files": entries}
with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2)
PYEOF
}

built_at_of() {
    "$PY" - "$1/manifest.json" <<'PYEOF'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("built_at", ""))
PYEOF
}

# ---------------------------------------------------------------- 造环境
make_root() {
    mkdir -p "$ROOT/fatfish" "$ROOT/web" "$ROOT/docs" "$ROOT/config" "$ROOT/data" "$ROOT/.venv/bin"
    echo '0.2.0' > "$ROOT/VERSION"
    echo '__version__ = "0.2.0"' > "$ROOT/fatfish/__init__.py"
    echo 'core v1' > "$ROOT/fatfish/core.py"
    echo 'old-only' > "$ROOT/fatfish/legacy.py"
    echo '<html>v1</html>' > "$ROOT/web/index.html"
    echo '# doc' > "$ROOT/docs/readme.md"
    echo '[cloud]' > "$ROOT/config/cloud.toml"
    # 受保护文件
    echo 'sentinel-db' > "$ROOT/data/fatfish.db"
    echo 'sentinel-env' > "$ROOT/.env"
    echo 'sentinel-local' > "$ROOT/config/cloud.local.toml"
    echo 'sentinel-venv' > "$ROOT/.venv/bin/marker"
    # 假 venv：让"导入冒烟"真的跑到（生产上是 .venv/bin/python）
    cat > "$ROOT/.venv/bin/python" <<EOF
#!/bin/sh
exec "$PY" "\$@"
EOF
    chmod +x "$ROOT/.venv/bin/python"
    write_manifest "$ROOT" "2026-10-29T00:00:00+08:00"
}

make_pkg() {  # $1=目录 $2=built_at $3=core内容 [4=BROKEN]
    pkg=$1
    rm -rf "$pkg"
    mkdir -p "$pkg/fatfish" "$pkg/web" "$pkg/docs" "$pkg/config"
    echo '0.2.0' > "$pkg/VERSION"
    echo '__version__ = "0.2.0"' > "$pkg/fatfish/__init__.py"
    echo "$3" > "$pkg/fatfish/core.py"
    echo '<html>v2</html>' > "$pkg/web/index.html"
    echo '# doc' > "$pkg/docs/readme.md"
    echo '[cloud]' > "$pkg/config/cloud.toml"
    mkdir -p "$pkg/data/stickers"
    : > "$pkg/data/stickers/.gitkeep"
    if [ "${4:-}" = "BROKEN" ]; then echo 'boom' > "$pkg/fatfish/BROKEN"; fi
    write_manifest "$pkg" "$2"
}

# 健康桩：存在 fatfish/BROKEN 就判不健康（模拟"能过预检但起不来"的包）
cat > "$HEALTH" <<EOF
#!/bin/sh
[ -f "$ROOT/fatfish/BROKEN" ] && exit 1
exit 0
EOF
chmod +x "$HEALTH"

restart_stub="printf x >> '$COUNTER'; true"
restart_count() { wc -c < "$COUNTER" 2>/dev/null | tr -d ' ' || echo 0; }

run_sync() {  # run_sync <pkg> [额外参数...]
    pkg=$1
    shift
    MF_PYTHON="$PY" sh "$SCRIPT" --pkg "$pkg" --root "$ROOT" --backup-dir "$BACKUPS" \
        --restart-cmd "$restart_stub" --health-cmd "sh '$HEALTH'" --stable-secs 1 \
        --owner "$(id -un 2>/dev/null || echo root)" "$@"
}

echo "=== 环境: $TMP"
make_root
make_pkg "$TMP/pkg-v2" "2026-10-30T10:00:00+08:00" 'core v2'
make_pkg "$TMP/pkg-bad" "2026-10-30T11:00:00+08:00" 'core v3-broken' BROKEN

# ---------------------------------------------------------------- 1) 正常同步
echo
echo "--- 场景1：正常同步"
OUT=$(run_sync "$TMP/pkg-v2" --label ok 2>&1); RC=$?
echo "$OUT" | sed 's/^/    /'
[ "$RC" = "0" ] && ok "退出码 0" || bad "退出码 $RC（期望 0）"
check "结果行 RESULT: OK" has_line "$OUT" "RESULT: OK"
check "core.py 已更新到 v2" grep -q 'core v2' "$ROOT/fatfish/core.py"
check "新版删掉的 legacy.py 已清理" test ! -e "$ROOT/fatfish/legacy.py"
check "built_at 已更新" grep -q '2026-10-30T10:00:00' "$ROOT/manifest.json"
check "data/ 未被动过" grep -q 'sentinel-db' "$ROOT/data/fatfish.db"
check ".env 未被动过" grep -q 'sentinel-env' "$ROOT/.env"
check "config/cloud.local.toml 未被动过" grep -q 'sentinel-local' "$ROOT/config/cloud.local.toml"
check ".venv 未被动过" test -f "$ROOT/.venv/bin/marker"
check "data/stickers 没有被包里的 .gitkeep 写进运行目录" test ! -e "$ROOT/data/stickers/.gitkeep"
check "服务被重启一次" test "$(restart_count)" = "1"
check "备份文件存在" sh -c "ls '$BACKUPS'/fatfish-*.tar.gz >/dev/null 2>&1"
check "备份里不含 data/" sh -c "! tar -tzf '$BACKUPS'/*.tar.gz | grep -q '^\./data/'"

AFTER_OK_BUILT=$(built_at_of "$ROOT")
AFTER_OK_CORE=$(cat "$ROOT/fatfish/core.py")
FP_OK=$(cksum "$ROOT/manifest.json" | awk '{print $1}')

# ---------------------------------------------------------------- 2) 幂等
echo
echo "--- 场景2：同一份包再跑一次（幂等）"
OUT=$(run_sync "$TMP/pkg-v2" --label ok2 2>&1); RC=$?
echo "$OUT" | sed 's/^/    /'
[ "$RC" = "0" ] && ok "退出码 0" || bad "退出码 $RC（期望 0）"
check "结果行 RESULT: NOCHANGE" has_line "$OUT" "RESULT: NOCHANGE"
check "服务没有被二次重启" test "$(restart_count)" = "1"
check "built_at 没有漂移" test "$(built_at_of "$ROOT")" = "$AFTER_OK_BUILT"
check "内容未变" test "$(cat "$ROOT/fatfish/core.py")" = "$AFTER_OK_CORE"
check "没有产生第二份备份" test "$(ls -1 "$BACKUPS"/fatfish-*.tar.gz | wc -l | tr -d ' ')" = "1"

# ---------------------------------------------------------------- 3) 坏包回滚
echo
echo "--- 场景3：能过预检但让服务起不来的包 → 应自动回滚"
OUT=$(run_sync "$TMP/pkg-bad" --label bad 2>&1); RC=$?
echo "$OUT" | sed 's/^/    /'
[ "$RC" = "2" ] && ok "退出码 2（已回滚）" || bad "退出码 $RC（期望 2）"
check "结果行 RESULT: ROLLED_BACK" has_line "$OUT" "RESULT: ROLLED_BACK"
check "坏文件 BROKEN 已被回滚清理" test ! -e "$ROOT/fatfish/BROKEN"
check "core.py 回到 v2" grep -q 'core v2' "$ROOT/fatfish/core.py"
check "built_at 回到上一版" test "$(built_at_of "$ROOT")" = "$AFTER_OK_BUILT"
check "回滚后服务健康" sh "$HEALTH"
check "重启计入回滚：累计 3 次" test "$(restart_count)" = "3"
check "data/ 仍完好" grep -q 'sentinel-db' "$ROOT/data/fatfish.db"

# ---------------------------------------------------------------- 4) 残缺包
echo
echo "--- 场景4：缺 manifest 的残缺包 → 预检拦下，零改动"
make_pkg "$TMP/pkg-broken" "2026-10-30T12:00:00+08:00" 'core v4-should-not-run'
rm -f "$TMP/pkg-broken/manifest.json"
BEFORE_FP=$(cksum "$ROOT/manifest.json" | awk '{print $1}')
OUT=$(run_sync "$TMP/pkg-broken" 2>&1); RC=$?
echo "$OUT" | sed 's/^/    /'
[ "$RC" = "1" ] && ok "退出码 1（预检失败）" || bad "退出码 $RC（期望 1）"
check "服务没有重启" test "$(restart_count)" = "3"
check "线上树未变" test "$(cksum "$ROOT/manifest.json" | awk '{print $1}')" = "$BEFORE_FP"
check "core.py 仍是 v2" grep -q 'core v2' "$ROOT/fatfish/core.py"
check "没有新增备份" test "$(ls -1 "$BACKUPS"/fatfish-*.tar.gz | wc -l | tr -d ' ')" = "2"

# ---------------------------------------------------------------- 5) 真实健康检查路径
echo
echo "--- 场景5：走真实健康检查（systemctl is-active + venv 导入冒烟，不传 --health-cmd）"
mkdir -p "$TMP/fakebin"
cat > "$TMP/fakebin/systemctl" <<EOF
#!/bin/sh
case "\$1" in
  is-active) [ -f "$ROOT/fatfish/BROKEN" ] && exit 3; exit 0 ;;
  restart) printf x >> "$COUNTER"; exit 0 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$TMP/fakebin/systemctl"
make_pkg "$TMP/pkg-v3" "2026-10-30T13:00:00+08:00" 'core v3'
OUT=$(PATH="$TMP/fakebin:$PATH" MF_PYTHON="$PY" \
    sh "$SCRIPT" --pkg "$TMP/pkg-v3" --root "$ROOT" --backup-dir "$BACKUPS" \
    --stable-secs 1 --owner "$(id -un 2>/dev/null || echo root)" 2>&1); RC=$?
echo "$OUT" | sed 's/^/    /'
[ "$RC" = "0" ] && ok "退出码 0" || bad "退出码 $RC（期望 0）"
check "结果行 RESULT: OK" has_line "$OUT" "RESULT: OK"
check "导入冒烟通过（没有 冒烟失败 字样）" sh -c "! printf '%s' \"\$0\" | grep -q '冒烟失败'" "$OUT"
check "core.py 已到 v3" grep -q 'core v3' "$ROOT/fatfish/core.py"

# ---------------------------------------------------------------- 汇总
echo
echo "=== 通过 $PASS 项，失败 $FAIL 项"
[ "$FAIL" = "0" ] || exit 1
printf 'SIM_RESULT: OK\n'
