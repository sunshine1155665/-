#!/usr/bin/env bash
# 把一台干净的 Debian 12 / Ubuntu 24.04 云主机准备成 fatfish 的「在线层」。
#
#   bash bootstrap.sh /tmp/fatfish-app.zip
#
# 做什么：系统包 → 2G swap → 服务用户 → 解包 → venv → systemd unit。
# 不做：NapCat（见 napcat.sh）、Tailscale、.env（见 README）。
# 幂等：重复跑不会重建 venv、不会重复加 swap、不会覆盖 data/。
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=${APP_DIR:-/opt/fatfish}
APP_USER=${APP_USER:-fatfish}
SWAP_GB=${SWAP_GB:-2}
ARCHIVE=${1:-}

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo bash $0 ...）"
command -v apt-get >/dev/null || die "只支持 Debian/Ubuntu（没有 apt-get）"

log "系统包"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y install \
  curl ca-certificates git unzip ffmpeg sqlite3 \
  python3 python3-venv python3-pip

log "swap ${SWAP_GB}G"
if [ ! -f /swapfile ]; then
  fallocate -l "${SWAP_GB}G" /swapfile 2>/dev/null \
    || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB * 1024)) status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
  printf 'vm.swappiness=10\n' >/etc/sysctl.d/99-fatfish-swap.conf
  sysctl -p /etc/sysctl.d/99-fatfish-swap.conf >/dev/null
else
  echo "已存在 /swapfile，跳过"
fi

log "服务用户 ${APP_USER}"
id -u "$APP_USER" >/dev/null 2>&1 \
  || adduser --disabled-password --gecos "" "$APP_USER"

log "解包到 ${APP_DIR}"
mkdir -p "$APP_DIR"
if [ -n "$ARCHIVE" ]; then
  [ -f "$ARCHIVE" ] || die "找不到 $ARCHIVE"
  case "$ARCHIVE" in
    *.zip) unzip -q -o "$ARCHIVE" -d "$APP_DIR" ;;
    *.tar.gz | *.tgz) tar -xzf "$ARCHIVE" -C "$APP_DIR" ;;
    *) die "只认 .zip / .tar.gz" ;;
  esac
  # make_app 产出的 zip 里有一个顶层 fatfish-app/，把内容提上来
  if [ ! -f "$APP_DIR/fatfish/__init__.py" ]; then
    shopt -s nullglob
    entries=("$APP_DIR"/*)
    shopt -u nullglob
    if [ ${#entries[@]} -eq 1 ] && [ -d "${entries[0]}" ] \
      && [ -f "${entries[0]}/fatfish/__init__.py" ]; then
      echo "检测到顶层目录 ${entries[0]##*/}/，展平"
      cp -a "${entries[0]}/." "$APP_DIR"/
      rm -rf "${entries[0]}"
    fi
  fi
fi
[ -f "$APP_DIR/fatfish/__init__.py" ] \
  || die "${APP_DIR} 里没有 fatfish/（先 scp 代码包，或直接传参给本脚本）"
[ -f "$APP_DIR/config/cloud.toml" ] \
  || die "缺 config/cloud.toml——代码包不是最新，先在本机重跑 scripts/make_app.py"

log "Python 环境"
cd "$APP_DIR"
# 挑一个 ≥3.11 的解释器：Ubuntu 24.04 = 3.12，Debian 12 = 3.11，
# Ubuntu 22.04 默认只有 3.10（没有 tomllib），需要先装 python3.11。
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 \
    && "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cand"
    break
  fi
done
[ -n "$PY" ] || die "找不到 Python 3.11+。Ubuntu 22.04 请先装 python3.11（见 README）。"
echo "使用 $PY ($("$PY" -V 2>&1))"

if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/pip install -q -U pip wheel
# 应用本体零第三方依赖；QQ 适配器需要 websocket-client。
.venv/bin/pip install -q "websocket-client>=1.7"

.venv/bin/python - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(f"需要 Python 3.11+，当前 {sys.version.split()[0]}")
print("python", sys.version.split()[0], "ok")
PY

log "systemd unit"
UNIT_DIR="$SCRIPT_DIR"
[ -f "$UNIT_DIR/fatfish-web.service" ] || UNIT_DIR="$APP_DIR/deploy/linux"
[ -f "$UNIT_DIR/fatfish-web.service" ] || die "找不到 fatfish-web.service（把整个 deploy/ 目录一起传上来）"
install -m 644 "$UNIT_DIR/fatfish-web.service" /etc/systemd/system/
install -m 644 "$UNIT_DIR/fatfish-qq.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable fatfish-web fatfish-qq >/dev/null

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

free -h

cat <<EOF

完成。接下来手动做三件事：

1) 写 /opt/fatfish/.env（照 ${SCRIPT_DIR}/env.example），然后
     chown ${APP_USER}:${APP_USER} /opt/fatfish/.env && chmod 600 /opt/fatfish/.env
2) 初始化数据库（已有 data/fatfish.db 就跳过）：
     sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/python -m fatfish init
3) 装 NapCat（bash ${SCRIPT_DIR}/napcat.sh）并扫码，然后：
     systemctl start fatfish-web fatfish-qq
     journalctl -u fatfish-web -u fatfish-qq -f

自检：curl -s http://127.0.0.1:8765/api/health
EOF
