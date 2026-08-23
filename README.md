# QQ AI Bot

基于 QQ 开放平台官方 API 的 AI 聊天机器人。支持私聊、群聊 @ 消息自动回复，可接入任意 OpenAI 兼容接口的 AI 服务（DeepSeek、OpenAI、智谱 AI、腾讯混元等），支持多模态图片识别。

## ✨ 功能特性

- 私聊 / 群聊 @ 消息自动回复（AI 对话）
- **私聊与群聊上下文分开存储**，群聊按群隔离（`data/user_context/private/`、`group/`）
- **私聊上下文一键转移到群聊**：群内发送 `/转移私聊到群聊`
- 关键词快捷回复、无意义消息过滤
- 多模态图片识别（OpenAI 兼容格式，失败自动降级为文本描述）
- 消息队列限流：繁忙时友好提示，不丢消息
- 断线自动重连、心跳假死检测、会话恢复（RESUME）

## 📦 环境要求

- Python 3.8+
- 依赖安装：`pip install -r requirements.txt`

## 🚀 快速开始

1. 复制配置模板：`cp config.json.example config.json`
2. 编辑 `config.json`：
   - `qq.app_id` / `qq.app_secret`：QQ 开放平台机器人凭据（未开通请先申请）
   - `api_key` / `base_url` / `model`：AI 服务配置（OpenAI 兼容接口）
   - `qq.sandbox`：`true` 为沙箱测试环境，正式运行请保持 `false`
3. 运行：`python qqbot.py`

> 首次运行会自动创建 `config.json` 和 `data/` 目录。日志位于 `data/logs/`。

## 💬 常用指令

| 指令 | 场景 | 说明 |
|---|---|---|
| `/clear` | 私聊 / 群聊 | 清空当前场景的对话历史 |
| `/转移私聊到群聊` | 群聊 | 把私聊上下文合并到当前群（保留群聊原有内容，自动去重） |
| `/生成转移码` | 私聊 | 生成身份绑定码（兜底用，10 分钟有效） |
| `/绑定转移码 <码>` | 群聊 | 绑定身份并转移上下文（兜底用） |

> 别名：`/将我的私聊上下文转移到当前群聊`、`/转移私聊上下文`、`/导入私聊上下文`。

## 📁 目录结构

```
API_qqbot/
├── qqbot.py                  # 主程序入口
├── config.json               # 配置文件（运行时生成/填写，已被 .gitignore 排除）
├── core/
|   ├── __init__.py           # 让core变成可import的包
│   ├── qq_client.py          # QQ 机器人客户端（WebSocket、心跳、重连、会话恢复）
│   ├── ai_client.py          # AI 服务客户端（OpenAI 兼容、多模态、重试）
│   ├── message_processor.py  # 消息处理器（队列、分段发送、指令）
│   ├── context_manager.py    # 上下文管理（私聊/群聊分文件夹、身份绑定）
│   ├── message_filter.py     # 消息过滤（关键词、无意义消息）
│   ├── file_handler.py       # 附件处理（图片转 base64 等）
│   ├── config_manager.py     # 配置读写
│   ├── logger.py             # 日志（自动分割）
│   ├── stats.py              # 数据统计（按天记录消息/AI调用等）
│   ├── plugin_manager.py     # 插件系统（加载/分发/启用停用）
│   └── web_admin.py          # Web 管理后台（配置编辑/统计/日志/插件管理）
├── plugins/                  # 插件目录（用户可自行添加，无需改代码）
│   ├── 示例插件.py           # 回复型插件示例
│   ├── 多文件示例/           # 多文件插件示例
│   └── 桥接转发示例.py       # 连接型（桥接）插件示例
└── data/                     # 运行时数据（日志、用户上下文、统计，已被 .gitignore 排除）
```

## 🧩 插件系统

无需修改任何源代码，把插件放进 `plugins/` 目录即可扩展机器人功能。支持**单文件插件**（一个 `.py`）和**多文件插件**（一个文件夹）。

### 怎么用

1. 在 `plugins/` 目录新建插件：
   - **单文件插件**：一个 `.py` 文件
   - **多文件插件**：一个文件夹（入口 `__init__.py` 或 `main.py`，其余 `.py` 是辅助模块）
2. 按下面的格式写内容
3. 在 Web 管理后台「插件」页点「🔄 重新加载」，或重启机器人
4. 完成！**不需要改任何程序源代码**

### 启用 / 停用插件

- 在 Web 管理后台「🧩 插件」页，每个插件都有「▶️ 启用 / ⏸️ 停用」按钮
- 停用/启用后点「💾 保存插件设置」才真正生效，状态会保存（重启机器人后保持）
- 停用的插件不会加载，也不会执行

### 多文件插件（一个插件多个 .py 文件）

当插件逻辑较多时，可以用一个文件夹装多个文件：

```
plugins/我的插件/
├── __init__.py     ← 入口（写 PLUGIN / on_message 等，接口和单文件一样）
└── helper.py       ← 辅助模块（随便叫什么名字）
```

入口文件里用普通 import 引用辅助模块（插件系统会把插件目录加入搜索路径）：

```python
import helper          # 引用同目录的 helper.py

def on_message(msg):
    return helper.do_something(msg)
```

入口文件优先级：`__init__.py` → `main.py` → 与目录同名的 `.py` → 目录里第一个含接口的 `.py`。

### 两类插件

**一、回复型插件（给用户回复内容）**

```python
PLUGIN = {"name": "我的功能", "description": "说明", "version": "1.0.0", "author": "你"}

COMMANDS = ["/天气"]              # 消息等于它、或 "/天气 北京" 这种带参数
KEYWORDS = ["天气"]               # 消息包含它就触发
def match(msg):                   # 完全自定义匹配（可三选一/组合）
    return msg.get("content", "").startswith("/xxx")

def on_message(msg):
    # msg 字段：
    #   type        消息类型: "c2c"私聊 / "group"群聊 / "channel"频道私信
    #   content     消息文本
    #   user_openid 发送者 openid
    #   user_name   发送者昵称
    #   group_openid 群 openid（私聊为空）
    #   msg_id      消息 ID
    return "要回复给用户的内容"    # 返回字符串 = 回复；返回 None = 不回复
```

**二、连接型/桥接型插件（连接两个东西，不一定要回复用户）**

适合：把消息转发到外部 Webhook/API、跨平台桥接、后台定时主动推送、监听外部事件等。写法：`on_message` 带第二个参数 `bot`，再可选加 `on_start/on_stop`。

```python
PLUGIN = {"name": "桥接", "description": "...", "version": "1.0.0", "author": "你"}

def on_message(msg, bot):
    # bot 提供：
    #   bot.send_message(openid, text)        主动发私聊
    #   bot.send_group_message(group_openid, text)  主动发群消息
    #   bot.config                           当前配置
    #   bot.log(...) / bot.info(...) / bot.error(...)  写日志
    # 在这里调用外部 API、转发消息等...
    return None        # 返回 None = 不回复用户（消息继续走内置逻辑）

def on_start(bot):
    # 机器人启动时调用一次：适合启动后台线程、建立连接、开始监听
    # 例：threading.Thread(target=..., args=(bot,), daemon=True).start()
    pass

def on_stop(bot):
    # 机器人停止时调用：清理后台线程/连接
    pass
```

### 规则细节

- **优先级**：插件在敏感词检查之后、内置指令（/帮助 /clear 等）之前执行
- **多个插件**：按文件名字母顺序逐个询问，第一个给出回复（返回非空字符串）的插件生效
- **匹配**：`COMMANDS` 支持带参数（`/天气 北京` 会命中 `/天气`）；`KEYWORDS` 是包含匹配；`match` 最灵活
- **连接型插件**：只有 `on_start`（没有 `on_message`）也可以——它只做后台工作，不参与消息回复
- **安全**：插件代码会直接运行，请只放你信任的文件；写错或抛异常会被捕获，不会弄崩机器人，但日志里会有记录

### 小技巧

- 插件是常驻内存的，模块里的全局变量会一直保留（比如记数、缓存）
- 想在插件里调外部 API？直接 `import requests` 即可（程序已安装）
- 想调试？在插件里 `print(...)` 或 `bot.log(...)` 会显示在机器人日志里
- 参考示例：
  - `plugins/示例插件.py`（回复型）
  - `plugins/桥接转发示例.py`（连接型）
  - `plugins/多文件示例/`（多文件）

## ⚠️ 安全提示

- 部署请使用自己的密钥，谨防泄露

## 📄 其他

- 变更记录见 [CHANGELOG.md](CHANGELOG.md)
- 版本说明见 [RELEASE_NOTES.md](RELEASE_NOTES.md)
