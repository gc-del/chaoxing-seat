#!/usr/bin/env bash
# 超星图书馆抢座 - Linux 服务器一键部署（无 GUI，AI 全自动，7x24 常驻）
# 用法: sudo bash deploy_linux.sh   （在本项目目录下执行）
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/chaoxing-seat}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"

echo "==> 1/6 安装系统依赖"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y python3 python3-pip tzdata
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip tzdata
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip tzdata
fi

echo "==> 2/6 设置时区 Asia/Shanghai（关键！放座时刻按北京时间 07:00）"
timedatectl set-timezone Asia/Shanghai 2>/dev/null || ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
timedatectl set-ntp true 2>/dev/null || true
date

echo "==> 3/6 部署程序到 $APP_DIR"
mkdir -p "$APP_DIR" "$APP_DIR/debug_median"
cp -f "$SRC_DIR"/*.py "$APP_DIR"/
[ -f "$SRC_DIR/config.json" ] && cp -f "$SRC_DIR/config.json" "$APP_DIR"/
chmod +x "$APP_DIR"/*.py 2>/dev/null || true

echo "==> 4/6 安装 Python 依赖"
"$PY" -m pip install --quiet --break-system-packages requests Pillow cryptography 2>/dev/null \
  || "$PY" -m pip install --quiet requests Pillow cryptography

echo "==> 5/6 创建 systemd 定时任务（每日 06:30 触发，Persistent 可补偿错过的触发）"
cat > /etc/systemd/system/chaoxing-grab.service <<EOF
[Unit]
Description=Chaoxing library seat grab
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
ExecStart=$PY $APP_DIR/main.py grab
EOF

cat > /etc/systemd/system/chaoxing-grab.timer <<EOF
[Unit]
Description=Run chaoxing grab daily at 06:30

[Timer]
OnCalendar=*-*-* 06:30:00
Persistent=true
AccuracySec=10s
Unit=chaoxing-grab.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now chaoxing-grab.timer

echo "==> 6/6 完成"
echo
echo "请立刻编辑配置文件（账号密码 / 模型 key / 通知 webhook）:"
echo "    vi $APP_DIR/config.json"
echo
echo "然后做一次连通性自检（登录 + 验证码 + 请求包，不会提交预约）:"
echo "    cd $APP_DIR && $PY main.py dryrun"
echo
echo "查看定时任务与日志:"
echo "    systemctl list-timers chaoxing-grab.timer"
echo "    journalctl -u chaoxing-grab.service -n 100 --no-pager"
echo
echo "注意: config.json 中 captcha_mode 必须是 ai（服务器无图形界面，无法人工点选）"
