"""
示例插件 - 让用户了解插件怎么写（本插件本身没什么用，仅作演示）
==============================================================
插件接口标准（v1）：

1. 文件放在 plugins/ 目录，一个功能一个 .py 文件，文件名即插件名
   （不要以 _ 开头，否则会被忽略；不要改程序其它文件）

2. 文件里按需定义以下内容：

   PLUGIN = { "name": ..., "description": ..., "version": ..., "author": ... }   # 可选，用于后台展示

   # —— 匹配规则（三选一，都写了就都生效）——
   COMMANDS = ["/xxx"]            # 消息完全等于它、或以它开头（可带参数如 "/天气 北京"）
   KEYWORDS = ["关键词"]           # 消息包含它就触发
   def match(msg): return True/False   # 完全自定义匹配（优先级最高）

   # —— 处理函数（必需）——
   def on_message(msg):          # msg 是 dict，见下方说明
       return "要回复给用户的内容"   # 返回字符串=回复；返回 None 或空=不回复（交给后面的插件/内置逻辑）

3. msg 字段说明：
   {
     "type": "c2c" | "group" | "channel",
     "content": "消息文本（已去除首尾空白）",
     "user_openid": "发送者 openid",
     "user_name": "发送者昵称",
     "group_openid": "群 openid（私聊为空）",
     "channel_id": "频道 ID（频道私信有值）",
     "msg_id": "消息 ID",
     "unified_openid": "统一身份标识（私聊/群聊一致）",
     "attachments": [...]        # 附件列表（图片等）
   }

4. 注意：
   - 插件在机器人收到消息时同步执行，别做耗时操作（如网络请求请控制在几秒内）
   - 插件代码异常会被捕获，不影响机器人本身；但插件内部请自行 try/except 以定位问题
   - 改完插件文件后，在 Web 后台「插件」页点「重新加载」即可生效（或重启机器人）
"""

PLUGIN = {
    "name": "示例插件",
    "description": "演示插件写法：回复 /你好 或 包含「示例」的消息（本插件无实际用途）",
    "version": "1.0.0",
    "author": "你",
}

# 匹配规则示例
COMMANDS = ["/你好", "/hi"]      # 消息是 "/你好" 或 "/你好 xxx" 时触发
KEYWORDS = ["示例"]              # 消息包含 "示例" 时触发

# 也可以完全自定义匹配：
# def match(msg):
#     return msg.get("content", "").startswith("/自定义")


def on_message(msg):
    """处理消息，返回要回复的内容（字符串）；不回复就返回 None"""
    content = msg.get("content", "").strip()
    name = msg.get("user_name", "用户")

    # COMMANDS 匹配支持带参数（如 "/你好 世界"），这里用 startswith 处理
    if content == "/你好" or content == "/hi" or content.startswith("/你好 ") or content.startswith("/hi "):
        return f"你好呀，{name}！我是示例插件，看到我说明插件系统工作正常～"

    if "示例" in content:
        return "这是示例插件的回复。想加自己的功能？复制本文件改成你想要的样子即可！"

    return None
