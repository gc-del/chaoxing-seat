#!/data/data/com.termux/files/usr/bin/bash
# 超星抢座 - 安卓 Termux 一键部署
# 在 Termux 里执行: bash termux_setup.sh
set -e

DIR="${SEAT_DIR:-$HOME/chaoxing-seat}"
echo "==> 1/5 安装依赖"
pkg update -y >/dev/null 2>&1 || true
pkg install -y python termux-api >/dev/null
python -m pip install --quiet --upgrade pip
python -m pip install --quiet requests Pillow cryptography

echo "==> 2/5 放置程序到 $DIR"
mkdir -p "$DIR/debug_median"
cp -f ./*.py "$DIR"/
[ -f config.json ] && cp -f config.json "$DIR"/

echo "==> 3/5 配置开机自启（需要 Termux:Boot 插件）"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/run_seat_loop.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
sleep 15
cd "$HOME/chaoxing-seat" && python main.py loop >> debug_median/loop.log 2>&1
EOF
chmod +x "$HOME/.termux/boot/run_seat_loop.sh"

echo "==> 4/5 开启常驻唤醒（防止系统在待机时杀掉进程）"
termux-wake-lock || echo "（termux-wake-lock 不可用，请检查 Termux:API 插件是否安装）"

echo "==> 5/5 自检（只登录+验证码，不提交预约）"
cd "$DIR" && python -u main.py dryrun

cat <<'TIPS'

============================================================
部署完成！请在手机上手动完成 3 件事（否则定时任务不会触发）：
1) 安装插件：Termux:API + Termux:Boot（F-Droid 版 Termux 里装）
2) 电池优化：设置 → 应用 → Termux → 电池 → 不受限制
   （小米/华为/OPPO 还要开"自启动"；三星要把 Termux 移出"深度睡眠应用"）
3) 保持手机插电并让 Termux 在后台常驻
   （可用 adb 设置永久免杀，见部署说明）
============================================================
TIPS
