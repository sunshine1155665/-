#!/bin/sh
# sync_cloud.sh — 把 fatfish-app 包覆盖式同步到云端运行目录，失败自动回滚。
#
# 设计要点（改之前先读）：
#   * 只覆盖「包里有的文件」，data/ .venv/ .env config/cloud.local.toml 永不触碰；
#   * 覆盖前先打 tar 备份（同样排除 data/），备份路径一定写进日志；
#   * 用 manifest.json 做「上一版文件清单」，删掉新版已不存在的文件（不误删 .venv 等）；
#   * 覆盖后重启服务并等它稳定；不健康就解回备份再重启；
#   * 幂等：同一份包跑第二次，指纹不变 → 不重启、不备份、退出码 0。
#
# 只依赖：sh / tar / gzip / find / sort / cksum / python3（读 manifest）。
#
# 退出码：0 = 已同步或无需同步；1 = 预检失败（未改动线上）；
#         2 = 已回滚（服务已按备份恢复）；3 = 回滚也失败，需人工介入。

set -eu

ROOT="${FATFISH_ROOT:-/opt/fatfish}"
BACKUP_DIR="${FATFISH_BACKUP_DIR:-/opt/fatfish-backups}"
KEEP=5
SERVICE="fatfish-qq"
PKG=""
RESTART_CMD=""
HEALTH_CMD=""
STABLE_SECS=15
OWNER="fatfish"
DRY_RUN=0
LABEL=""

usage() {
    cat <<'EOF'
用法: sync_cloud.sh --pkg <目录|tar.gz> [选项]

  --pkg PATH        包路径：解开的 fatfish-app 目录，或 fatfish-app-*.tar.gz
  --root DIR        运行目录（默认 /opt/fatfish）
  --backup-dir DIR  备份目录（默认 /opt/fatfish-backups）
  --keep N          保留最近 N 份备份（默认 5）
  --service NAME    重启的服务名（默认 fatfish-qq）
  --restart-cmd CMD 覆盖重启命令（模拟测试用；默认 systemctl restart <service>）
  --health-cmd CMD  覆盖健康检查命令（模拟测试用；默认 systemctl is-active + 导入冒烟）
  --stable-secs N   重启后需要持续健康的秒数（默认 15）
  --owner USER      覆盖后把新文件的属主改成该用户（默认 fatfish；不存在则跳过）
  --label TEXT      写进备份名与日志的标签
  --dry-run         只报告将要发生的改动，不备份、不覆盖、不重启
  -h, --help        显示本帮助

受保护路径（永不覆盖/删除）：
  data/  .venv/  .env  config/cloud.local.toml
  NapCat：napcat/、deploy/linux/napcat*、scripts/napcat*、setup_napcat/start_napcat、
          tests/test_napcat*、docs 里 napcat-pskey / napcat-qr-phone
EOF
}

log() { printf '%s [sync] %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '%s [sync] WARN: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { printf '%s [sync] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --pkg) PKG="${2:?}"; shift 2 ;;
        --root) ROOT="${2:?}"; shift 2 ;;
        --backup-dir) BACKUP_DIR="${2:?}"; shift 2 ;;
        --keep) KEEP="${2:?}"; shift 2 ;;
        --service) SERVICE="${2:?}"; shift 2 ;;
        --restart-cmd) RESTART_CMD="${2:?}"; shift 2 ;;
        --health-cmd) HEALTH_CMD="${2:?}"; shift 2 ;;
        --stable-secs) STABLE_SECS="${2:?}"; shift 2 ;;
        --owner) OWNER="${2:?}"; shift 2 ;;
        --label) LABEL="${2:?}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "未知参数: $1" ;;
    esac
done

[ -n "$PKG" ] || { usage >&2; exit 1; }
ROOT="${ROOT%/}"
BACKUP_DIR="${BACKUP_DIR%/}"

STAGE=""
WORK=$(mktemp -d /tmp/fatfish-sync.XXXXXX)
cleanup() {
    [ -n "$STAGE" ] && rm -rf "$STAGE"
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

# ---------------------------------------------------------------- 小工具
PY=""
pick_python() {
    PY="${MF_PYTHON:-}"
    if [ -n "$PY" ] && command -v "$PY" >/dev/null 2>&1; then return 0; fi
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then PY=$candidate; return 0; fi
    done
    if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; return 0; fi
    return 1
}

# 目录指纹：路径|大小|mtime。不含 data/ .venv/（不该变的部分）。
fingerprint() {
    find "$ROOT" \
        \( -path "$ROOT/data" -o -path "$ROOT/.venv" -o -path "$ROOT/.git" \) -prune -o \
        -type f ! -name '*.pyc' -printf '%P|%s|%T@\n' 2>/dev/null \
        | LC_ALL=C sort | cksum | awk '{print $1"-"$2}'
}

count_files() {
    find "$ROOT" \
        \( -path "$ROOT/data" -o -path "$ROOT/.venv" \) -prune -o \
        -type f ! -name '*.pyc' -print 2>/dev/null | wc -l | tr -d ' '
}

manifest_list() {
    "$PY" - "$1" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    files = json.load(handle)["files"]
for entry in files:
    path = entry["path"] if isinstance(entry, dict) else entry
    sys.stdout.buffer.write((path + "\n").encode("utf-8"))
PYEOF
}

manifest_field() {
    "$PY" - "$1" "$2" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle).get(sys.argv[2], "")
sys.stdout.buffer.write((str(value) + "\n").encode("utf-8"))
PYEOF
}

is_protected() {
    case "$1" in
        data|data/*|.venv|.venv/*|.env|config/cloud.local.toml) return 0 ;;
        # 2026-11-20: 云端部署明确不碰 NapCat（容器/脚本/unit/登录态）。
        # 包内不含这些文件；prune 也必须跳过，否则会把线上 NapCat 删掉。
        napcat|napcat/*|deploy/linux/napcat*|scripts/napcat*|scripts/setup_napcat.ps1|scripts/start_napcat.bat|tests/test_napcat*|docs/branches/napcat-pskey.md|docs/runbooks/napcat-qr-phone.md) return 0 ;;
    esac
    return 1
}

# 包内每个文件是否已与运行目录逐字节一致（幂等判定，不写盘）
content_same() {
    "$PY" - "$PKG_DIR" "$ROOT" <<'PYEOF'
import hashlib, json, os, sys
pkg, root = sys.argv[1], sys.argv[2]
with open(os.path.join(pkg, "manifest.json"), encoding="utf-8") as handle:
    files = json.load(handle)["files"]

def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

for entry in files:
    target = os.path.join(root, entry["path"])
    if not os.path.isfile(target):
        print("missing:" + entry["path"])
        raise SystemExit(1)
    if os.path.getsize(target) != entry["size"] or sha256(target) != entry["sha256"]:
        sys.stdout.buffer.write(("dirty:" + entry["path"] + "\n").encode("utf-8"))
        raise SystemExit(1)
sys.stdout.buffer.write(b"same\n")
PYEOF
}

# ---------------------------------------------------------------- 健康检查
health_probe() {
    if [ -n "$HEALTH_CMD" ]; then
        sh -c "$HEALTH_CMD" >/dev/null 2>&1
        return $?
    fi
    systemctl is-active --quiet "$SERVICE" || return 1
    if [ -x "$ROOT/.venv/bin/python" ] && [ -f "$ROOT/VERSION" ]; then
        # 必须在 $ROOT 里跑：venv 的 python 只有 `cd` 到运行目录才会 import 到新代码。
        got=$( cd "$ROOT" && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
            -c 'import fatfish;print(fatfish.__version__)' 2>/dev/null || true )
        want=$(tr -d '\r\n ' < "$ROOT/VERSION" 2>/dev/null || true)
        if [ -n "$want" ] && [ "$got" != "$want" ]; then
            warn "冒烟失败：导入版本 ${got:-<空>} != VERSION $want"
            return 1
        fi
    fi
    return 0
}

restart_service() {
    if [ -n "$RESTART_CMD" ]; then
        sh -c "$RESTART_CMD"
    else
        systemctl restart "$SERVICE"
    fi
}

wait_healthy() {
    phase=$1
    log "$phase：等待服务健康（需持续 ${STABLE_SECS}s）"
    if ! health_probe; then
        waited=0
        while [ "$waited" -lt 30 ]; do
            sleep 2
            waited=$((waited + 2))
            health_probe && break
        done
    fi
    health_probe || return 1
    sleep "$STABLE_SECS"
    health_probe || return 1
    return 0
}

# ---------------------------------------------------------------- 覆盖 / 清理
overlay_package() {  # $1 = 源目录
    payload="$WORK/payload.tar"
    tar -C "$1" --exclude='./data' --exclude='./data/*' --exclude='./.venv' \
        --exclude='./.venv/*' --exclude='__pycache__' -cf "$payload" . \
        || return 1
    tar -C "$ROOT" -xf "$payload"
}

# 清理清单里都有、但已不需要的文件（受保护路径跳过）
prune_stale() {  # $1 = 待删清单
    removed=0
    while IFS= read -r rel; do
        rel=$(printf '%s' "$rel" | tr -d '
')
        [ -n "$rel" ] || continue
        if is_protected "$rel"; then continue; fi
        case "$rel" in *__pycache__*|*.pyc) continue ;; esac
        if [ -e "$ROOT/$rel" ]; then
            rm -f "$ROOT/$rel"
            log "删除多余文件: $rel"
            removed=$((removed + 1))
        fi
    done < "$1"
    find "$ROOT" -mindepth 1 \
        \( -path "$ROOT/data" -o -path "$ROOT/.venv" \) -prune -o \
        -type d -empty -print 2>/dev/null | while IFS= read -r dir; do
            rmdir "$dir" 2>/dev/null || true
        done
    log "清理完成：删除 $removed 个文件"
}

fix_owner() {
    if ! id -u "$OWNER" >/dev/null 2>&1; then
        warn "用户 $OWNER 不存在，跳过 chown（模拟环境会这样）"
        return 0
    fi
    for entry in "$PKG_DIR"/*; do
        base=$(basename "$entry")
        [ "$base" = "data" ] && continue
        if [ -e "$ROOT/$base" ]; then
            chown -R "$OWNER:$OWNER" "$ROOT/$base" 2>/dev/null || true
        fi
    done
    log "属主已对齐到 $OWNER"
}

# ---------------------------------------------------------------- 预检
pick_python || die "找不到可用的 python 解释器（用于解析 manifest.json）"
[ -d "$ROOT" ] || die "运行目录不存在: $ROOT"
if [ ! -f "$ROOT/manifest.json" ]; then
    warn "运行目录没有 manifest.json，本次不会清理旧文件"
fi

case "$PKG" in
    *.tar.gz|*.tgz)
        [ -f "$PKG" ] || die "包文件不存在: $PKG"
        STAGE=$(mktemp -d /tmp/fatfish-pkg.XXXXXX)
        tar -xzf "$PKG" -C "$STAGE" || die "解包失败（包可能损坏）: $PKG"
        if [ -f "$STAGE/manifest.json" ]; then
            PKG_DIR="$STAGE"
        else
            PKG_DIR=$(find "$STAGE" -mindepth 1 -maxdepth 1 -type d | head -1)
            [ -n "${PKG_DIR:-}" ] || die "包内没有目录: $PKG"
        fi
        ;;
    *)
        [ -d "$PKG" ] || die "包目录不存在: $PKG"
        PKG_DIR="$PKG"
        ;;
esac

[ -f "$PKG_DIR/manifest.json" ] || die "包里没有 manifest.json（不是 make_app.py 的产物）"
[ -f "$PKG_DIR/VERSION" ] || die "包里没有 VERSION"
[ -f "$PKG_DIR/fatfish/__init__.py" ] || die "包里缺 fatfish/__init__.py（包不完整）"

NEW_VER=$(manifest_field "$PKG_DIR/manifest.json" version)
NEW_BUILT=$(manifest_field "$PKG_DIR/manifest.json" built_at)
NEW_COUNT=$(manifest_list "$PKG_DIR/manifest.json" | wc -l | tr -d ' ')
[ "${NEW_COUNT:-0}" -ge 5 ] || die "manifest 文件数异常（$NEW_COUNT），拒绝部署"

MISSING=$("$PY" - "$PKG_DIR" <<'PYEOF'
import json, os, sys
root = sys.argv[1]
with open(os.path.join(root, "manifest.json"), encoding="utf-8") as handle:
    files = json.load(handle)["files"]
missing = [e["path"] for e in files if not os.path.isfile(os.path.join(root, e["path"]))]
sys.stdout.buffer.write((str(len(missing)) + "\n").encode("utf-8"))
if missing:
    sys.stderr.buffer.write(("\n".join(missing[:5]) + "\n").encode("utf-8"))
PYEOF
) || die "manifest 校验失败"
[ "$MISSING" = "0" ] || die "包内缺少 $MISSING 个 manifest 声明的文件（包不完整）"

[ -n "$LABEL" ] && log "标签: $LABEL"
log "包: $PKG_DIR  version=$NEW_VER  built_at=$NEW_BUILT  files=$NEW_COUNT"
log "目标: $ROOT  服务: $SERVICE"

BEFORE_FP=$(fingerprint)
BEFORE_COUNT=$(count_files)
log "同步前：$BEFORE_COUNT 个文件，指纹 $BEFORE_FP"

# ---------------------------------------------------------------- 差异
manifest_list "$PKG_DIR/manifest.json" | LC_ALL=C sort > "$WORK/new.txt"

if [ -f "$ROOT/manifest.json" ]; then
    OLD_BUILT=$(manifest_field "$ROOT/manifest.json" built_at || true)
    OLD_VER=$(manifest_field "$ROOT/manifest.json" version || true)
    log "线上现状：version=${OLD_VER:-?} built_at=${OLD_BUILT:-?}"
    if ! manifest_list "$ROOT/manifest.json" | LC_ALL=C sort > "$WORK/old.txt"; then
        : > "$WORK/old.txt"
        warn "旧 manifest 解析失败，本次不清理旧文件"
    fi
else
    : > "$WORK/old.txt"
fi
LC_ALL=C comm -23 "$WORK/old.txt" "$WORK/new.txt" > "$WORK/stale.txt"
STALE=$(wc -l < "$WORK/stale.txt" | tr -d ' ')
log "上一版有 $STALE 个文件在新版中不存在（将清理，受保护路径除外）"

if [ "$DRY_RUN" = "1" ]; then
    log "dry-run：将覆盖 $NEW_COUNT 个文件、删除 $STALE 个文件，然后重启 $SERVICE"
    while IFS= read -r rel; do
        if [ -n "$rel" ] && ! is_protected "$rel"; then log "  将删除: $rel"; fi
    done < "$WORK/stale.txt"
    printf 'RESULT: DRY_RUN\n'
    exit 0
fi

# 幂等：包内容与运行目录逐字节一致且没有要清理的文件 → 不备份、不覆盖、不重启
if [ "$STALE" = "0" ]; then
    if SAME_MSG=$(content_same); then
        log "包内容与运行目录完全一致（$SAME_MSG），无需任何操作"
        printf 'RESULT: NOCHANGE\n'
        exit 0
    fi
    log "存在差异（${SAME_MSG#*:}），开始同步"
fi

# ---------------------------------------------------------------- 备份
mkdir -p "$BACKUP_DIR"
if command -v flock >/dev/null 2>&1; then
    LOCK="$BACKUP_DIR/.sync.lock"
    exec 9>"$LOCK"
    flock -n 9 || die "已有另一个同步在跑（锁: $LOCK）"
fi

STAMP=$(date +%Y%m%d-%H%M%S)
SUFFIX=${LABEL:+-$LABEL}
ARCHIVE="$BACKUP_DIR/fatfish-$STAMP$SUFFIX.tar.gz"
log "备份 $ROOT → $ARCHIVE（排除 data/ .venv/）"
tar -C "$ROOT" --exclude='./data' --exclude='./data/*' --exclude='./.venv' \
    --exclude='./.venv/*' --exclude='__pycache__' -czf "$ARCHIVE" . \
    || die "备份失败，未做任何改动"
ENTRIES=$(tar -tzf "$ARCHIVE" 2>/dev/null | wc -l | tr -d ' ')
[ "${ENTRIES:-0}" -ge 5 ] || die "备份包异常（仅 $ENTRIES 项），中止"
tar -tzf "$ARCHIVE" 2>/dev/null | grep -qx './manifest.json' \
    || die "备份包不完整（缺 manifest.json），中止"
log "备份完成：$ARCHIVE（$ENTRIES 项，$(du -h "$ARCHIVE" | cut -f1)）"

KEPT=$(ls -1t "$BACKUP_DIR"/fatfish-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
if [ "$KEPT" -gt "$KEEP" ]; then
    ls -1t "$BACKUP_DIR"/fatfish-*.tar.gz | tail -n +$((KEEP + 1)) | while IFS= read -r old; do
        rm -f "$old"
        log "清理旧备份: $(basename "$old")"
    done
fi

# ---------------------------------------------------------------- 覆盖
log "覆盖文件（$NEW_COUNT 个）"
overlay_package "$PKG_DIR" || die "覆盖失败；备份在 $ARCHIVE，可手工还原"
if [ -s "$WORK/stale.txt" ]; then
    prune_stale "$WORK/stale.txt"
fi
fix_owner || true

AFTER_FP=$(fingerprint)
AFTER_COUNT=$(count_files)
log "同步后：$AFTER_COUNT 个文件，指纹 $AFTER_FP"

if [ "$BEFORE_FP" = "$AFTER_FP" ]; then
    log "内容无变化，跳过重启（服务保持原状）"
    printf 'RESULT: NOCHANGE\n'
    exit 0
fi

# ---------------------------------------------------------------- 重启 + 校验
log "重启 $SERVICE"
restart_service || warn "重启命令返回非零"

if wait_healthy "重启后"; then
    log "服务健康，同步成功"
    log "回滚锚点：$ARCHIVE"
    printf 'RESULT: OK\n'
    exit 0
fi

# ---------------------------------------------------------------- 回滚
warn "服务不健康，开始回滚：$ARCHIVE"
restart_service || warn "回滚前重启命令返回非零（忽略）"
RB=$(mktemp -d /tmp/fatfish-rollback.XXXXXX)
if ! tar -C "$RB" -xzf "$ARCHIVE"; then
    rm -rf "$RB"
    warn "备份解不开，回滚失败"
    printf 'RESULT: ROLLBACK_FAILED\n'
    printf '手工恢复：tar -C %s -xzf %s\n' "$ROOT" "$ARCHIVE"
    printf '然后：systemctl restart %s\n' "$SERVICE"
    exit 3
fi
# 先把「坏的这一版」的文件清单抓下来：还原会覆盖 manifest.json
if [ -f "$ROOT/manifest.json" ]; then
    manifest_list "$ROOT/manifest.json" | LC_ALL=C sort > "$WORK/bad.txt" 2>/dev/null || : > "$WORK/bad.txt"
else
    : > "$WORK/bad.txt"
fi
overlay_package "$RB" || warn "还原覆盖有报错（继续尝试）"
if [ -f "$RB/manifest.json" ] && [ -s "$WORK/bad.txt" ]; then
    manifest_list "$RB/manifest.json" | LC_ALL=C sort > "$WORK/rb.txt"
    LC_ALL=C comm -13 "$WORK/rb.txt" "$WORK/bad.txt" > "$WORK/rb-stale.txt"
    if [ -s "$WORK/rb-stale.txt" ]; then
        prune_stale "$WORK/rb-stale.txt"
    fi
fi
fix_owner || true
rm -rf "$RB"

if wait_healthy "回滚后"; then
    warn "已回滚到同步前状态，服务健康"
    printf 'RESULT: ROLLED_BACK\n'
    printf '备份: %s\n' "$ARCHIVE"
    printf '手工再试：tar -C %s -xzf %s && systemctl restart %s\n' "$ROOT" "$ARCHIVE" "$SERVICE"
    exit 2
fi

warn "回滚后服务仍不健康，需要人工介入"
printf 'RESULT: ROLLBACK_FAILED\n'
printf '备份: %s\n' "$ARCHIVE"
printf '手工恢复：tar -C %s -xzf %s && systemctl restart %s\n' "$ROOT" "$ARCHIVE" "$SERVICE"
printf '日志：journalctl -u %s -n 80 --no-pager\n' "$SERVICE"
exit 3
