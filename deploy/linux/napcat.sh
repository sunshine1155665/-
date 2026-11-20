#!/usr/bin/env bash
# 在云主机上装 NapCat（Docker，host 网络），OneBot WS 固定 127.0.0.1:3001。
#
#   WEBUI_TOKEN=xxx bash napcat.sh
#
# 二维码打印在 `docker logs -f napcat` 里；WebUI 6099 只经 SSH 隧道访问，
# 不要写进安全组。
set -euo pipefail

WEBUI_TOKEN=${WEBUI_TOKEN:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)}

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行"
command -v apt-get >/dev/null || die "只支持 Debian/Ubuntu"

if ! command -v docker >/dev/null 2>&1; then
  log "安装 Docker"
  # 国内拉不动就配阿里云镜像加速器后再重跑：
  #   /etc/docker/daemon.json -> {"registry-mirrors":["https://<id>.mirror.aliyuncs.com"]}
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

log "NapCat 容器"
mkdir -p /opt/napcat/{config,qq,plugins}

if docker ps -a --format '{{.Names}}' | grep -qx napcat; then
  echo "已存在 napcat 容器，跳过创建"
else
  docker run -d --name napcat \
    --restart always \
    --network host \
    -e NAPCAT_UID=0 -e NAPCAT_GID=0 \
    -e WEBUI_TOKEN="${WEBUI_TOKEN}" \
    -v /opt/napcat/config:/app/napcat/config \
    -v /opt/napcat/qq:/app/.config/QQ \
    -v /opt/napcat/plugins:/app/napcat/plugins \
    mlikiowa/napcat-docker:latest
fi

cat <<EOF

WEBUI_TOKEN=${WEBUI_TOKEN}
  （记下来，登录 WebUI 要用；也存在 /opt/napcat/config/webui.json 里）

下一步：
1) 看二维码并扫码：
     docker logs -f napcat
   二维码是日志里的方块图；终端显示不全时取出 png：
     docker cp napcat:/app/napcat/config/qrcode.png /tmp/ && ls -l /tmp/qrcode.png
2) 配置 OneBot（6099 别开放，用隧道）：
     ssh -L 6099:127.0.0.1:6099 root@<这台机器>
   浏览器开 http://127.0.0.1:6099 → 网络配置 → 新建 WebSocket 服务器
     host 127.0.0.1  port 3001  token = .env 里的 QQ_ACCESS_TOKEN  格式 array
3) 验证：
     curl -s http://127.0.0.1:3001 | head -c 200   # 有响应即可
     systemctl restart fatfish-qq
EOF
