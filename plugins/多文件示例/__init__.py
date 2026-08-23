"""
多文件插件示例 - 主入口（__init__.py）
=======================================
这是一个"多文件插件"的示例：整个目录 plugins/多文件示例/ 是一个插件。
入口是 __init__.py，辅助模块（helper.py）用普通 import 引用
（插件系统会把插件目录加入 import 搜索路径）。
"""

import helper   # 同目录的辅助模块（插件系统已把本目录加入搜索路径）


PLUGIN = {
    "name": "多文件插件示例",
    "description": "演示一个插件由多个 .py 文件组成：入口 __init__.py + 辅助模块 helper.py",
    "version": "1.0.0",
    "author": "你",
}

COMMANDS = ["/计算"]
KEYWORDS = ["多文件"]


def on_message(msg):
    content = msg.get("content", "").strip()
    name = msg.get("user_name", "用户")

    if content.startswith("/计算 "):
        # /计算 1+2 或 /计算 3*4
        expr = content[len("/计算 "):].strip()
        try:
            result = helper.add(expr)
            return f"{expr} = {result}"
        except Exception as e:
            return f"算不出来哦：{e}"

    if "多文件" in content:
        return helper.greet(name)

    return None
