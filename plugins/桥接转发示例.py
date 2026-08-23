"""
连接型插件示例 - 把消息转发到外部服务（Webhook），并演示后台监听
=================================================================
这类插件的用途：不是"给用户回复"，而是把机器人的消息/事件连接到
另一个系统（Webhook、API、别的平台等），或主动向群里推送内容。

本示例演示三种能力：
1. on_message(msg, bot)  收到消息时转发到外部 Webhook（不回复用户）
2. on_start(bot)         机器人启动时启动一个后台线程，定期主动发消息
3. 用 bot 主动发群消息 / 私聊消息

使用方法：
- 把下面 WEBHOOK_URL 换成你自己的服务地址（随便一个能接收 POST 的地址即可）
- 把 TARGET_GROUP 换成你的群 openid（或留空禁用主动推送）
- 放进 plugins/ 目录，Web 后台「插件」页点重新加载
"""

import threading
import time
import requests

PLUGIN = {
    "name": "桥接转发示例",
    "description": "演示连接型插件：消息转发到 Webhook + 后台定时主动推送",
    "version": "1.0.0",
    "author": "你",
}

# ====== 配置（改成你自己的） ======
WEBHOOK_URL = ""          # 外部服务地址，如 https://example.com/hook
TARGET_GROUP = ""         # 要主动推送的群 openid；留空 = 不主动推送
TARGET_USER = ""          # 要主动推送的用户 openid；留空 = 不推送
PUSH_INTERVAL = 3600      # 后台主动推送间隔（秒）
# ====== 结束配置 ======

# 后台线程控制
_thread = None
_running = False


def on_message(msg, bot):
    """收到消息时调用。这里把消息转发给外部 Webhook，不回复用户。"""
    if not WEBHOOK_URL:
        return None
    try:
        payload = {
            "type": msg.get("type"),
            "content": msg.get("content", ""),
            "user_openid": msg.get("user_openid", ""),
            "user_name": msg.get("user_name", ""),
            "group_openid": msg.get("group_openid", ""),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=5)
        bot.log(f"已转发到 Webhook: {resp.status_code}")
    except Exception as e:
        bot.error(f"转发 Webhook 失败: {e}")
    return None   # 返回 None = 不回复用户（消息继续走内置逻辑）


def on_start(bot):
    """机器人启动时调用：启动后台线程，定期主动推送。"""
    global _thread, _running
    if _thread is not None:
        return
    _running = True
    _thread = threading.Thread(target=_worker, args=(bot,), daemon=True)
    _thread.start()
    bot.log("桥接示例插件后台线程已启动")


def on_stop(bot):
    """机器人停止时调用：停掉后台线程。"""
    global _running
    _running = False


def _worker(bot):
    """后台线程：每隔一段时间主动向指定群/用户推送一条消息。"""
    while _running:
        try:
            if TARGET_GROUP:
                bot.send_group_message(TARGET_GROUP, "⏰ 桥接示例插件的定时推送（可改成任意内容）")
            if TARGET_USER:
                bot.send_message(TARGET_USER, "⏰ 这是主动私聊推送")
        except Exception as e:
            bot.error(f"后台推送失败: {e}")
        # 睡到下一个周期（每 5 秒检查一次开关，方便 on_stop 及时退出）
        for _ in range(int(PUSH_INTERVAL / 5)):
            if not _running:
                return
            time.sleep(5)
