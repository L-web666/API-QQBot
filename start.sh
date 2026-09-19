#!/bin/sh
# QQ AI Bot 启动脚本（Linux / 安卓 Termux） - 崩溃/退出后自动重启
while true; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 QQ AI Bot..."
  python qqbot.py
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 机器人已退出，3 秒后自动重启..."
  sleep 3
done
