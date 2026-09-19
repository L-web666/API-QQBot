"""
骰子插件 - 群聊轻互动
======================
指令：/骰子    掷一个 1-6 的骰子
"""

import random

PLUGIN = {
    "name": "骰子",
    "description": "/骰子 掷一个骰子",
    "version": "2.0.0",
    "author": "L-web666",
}

COMMANDS = ["/骰子"]

_FACES = {1: '⚀', 2: '⚁', 3: '⚂', 4: '⚃', 5: '⚄', 6: '⚅'}


def on_message(msg, bot=None):
    content = (msg.get("content") or "").strip()
    if content != "/骰子":
        return None
    nick = msg.get("user_name") or "你"
    d = random.randint(1, 6)
    return f"🎲 {nick} 掷出了：{_FACES.get(d, str(d))}  （{d} 点）"
