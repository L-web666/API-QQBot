# QQ AI Bot 插件说明（接口标准 v1）

## 概述

无需修改任何源代码，把插件放进 `plugins/` 目录即可扩展机器人功能。
支持**单文件插件**（一个 `.py`）和**多文件插件**（一个文件夹）。

## 怎么用

1. 在 `plugins/` 目录新建插件：
   - **单文件插件**：一个 `.py` 文件
   - **多文件插件**：一个文件夹（入口 `__init__.py` 或 `main.py`，其余 `.py` 是辅助模块）
2. 按下面的格式写内容
3. 在 Web 管理后台「插件」页点「🔄 刷新」，或重启机器人
4. 完成！**不需要改任何程序源代码**

## 启用 / 停用插件

- 在 Web 管理后台「🧩 插件」页，每个插件都有「▶️ 启用 / ⏸️ 停用」按钮
- 停用/启用后点「💾 保存插件设置」才真正生效，状态会保存（重启机器人后保持）
- 停用的插件不会加载，也不会执行

## 多文件插件（一个插件多个 .py 文件）

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

## 两类插件

### 一、回复型插件（给用户回复内容）

```python
PLUGIN = {"name": "我的功能", "description": "说明", "version": "1.0.0", "author": "L-web666"}

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

### 二、连接型/桥接型插件（连接两个东西，不一定要回复用户）

适合：把消息转发到外部 Webhook/API、跨平台桥接、后台定时主动推送、
监听外部事件等。写法：`on_message` 带第二个参数 `bot`，再可选加 `on_start/on_stop`。

```python
PLUGIN = {"name": "桥接", "description": "...", "version": "1.0.0", "author": "L-web666"}

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

## 规则细节

- **优先级**：插件在敏感词检查之后、内置指令（/帮助 /clear 等）之前执行
- **多个插件**：按文件名字母顺序逐个询问，第一个给出回复（返回非空字符串）的插件生效
- **同名插件冲突**：`plugins/` 下不能同时存在 `X.py` 和 `X/`（同名文件+目录会被拒绝加载，避免数据互相覆盖）；显示名（PLUGIN["name"]）重复没关系，**文件名必须唯一**
- **匹配**：`COMMANDS` 支持带参数（`/天气 北京` 会命中 `/天气`）；`KEYWORDS` 是包含匹配；`match` 最灵活
- **连接型插件**：只有 `on_start`（没有 `on_message`）也可以——它只做后台工作，不参与消息回复
- **安全**：插件代码会直接运行，请只放你信任的文件；写错或抛异常会被捕获，不会弄崩机器人，但日志里会有记录

## 数据存储规范（重要）

插件如果需要**保存数据到文件**，必须遵守以下规则（插件系统自动提供路径）：

- 所有插件数据统一放在 **`data/plugins_data/`** 目录下
- 插件加载时系统自动注入两个变量，插件代码里**直接用即可**（不要自己拼路径）：
  - **`DATA_FILE`**：本插件的单文件数据路径 → `data/plugins_data/<插件名>.json`
    - **只存一个文件时用它**（推荐，大多数插件足够）
  - **`DATA_DIR`**：本插件的专属文件夹 → `data/plugins_data/<插件名>/`
    - **需要存多个文件时**，全部放进这个文件夹（系统已自动创建，只有你的插件能用）
- 插件**禁止**把数据写到 `plugins/` 目录、程序根目录或其它插件的位置

```python
# 单文件数据（推荐）
import json, os

def _load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {}

def _save(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 多文件数据：直接往 DATA_DIR 里放文件（目录已建好）
#   open(os.path.join(DATA_DIR, 'config.json'), 'w', ...)
```

> 数据放在 `data/` 下可随程序自动备份、不会混进插件目录；各插件按名隔离，互不覆盖。

## 可配置插件：把设置放进 JSON 文件（推荐做法）

如果插件有"用户需要改的设置"（地址、模型名、开关、超时…），**不要写死在代码里**，
按下面的约定放进 `DATA_DIR` 下的一个 JSON 文件，体验最好：

1. **首次运行自动生成**配置 JSON（文件不存在就写一份带默认值的）
2. 同时在**同一个目录**自动生成这份 JSON 的**说明文件**（`.txt`，逐项解释怎么填）
3. 插件加载/启动时用 `bot.log(...)` **在控制台打印这两个文件的完整路径**，用户一看就知道去哪改
4. 支持**保存即生效**：用文件修改时间判断配置有没有变，变了就重新读（不用重启机器人）
5. JSON 写坏了也不能让插件崩：捕获 `ValueError`/`OSError`，本次用默认值并 `bot.error(...)` 提示

```python
# DATA_DIR 由系统注入；单独调试时用下面的兜底路径
CONFIG_DIR = globals().get('DATA_DIR') or os.path.join('data', 'plugins_data', '我的插件')
CONFIG_FILE = os.path.join(CONFIG_DIR, '我的插件.json')
DOC_FILE = os.path.join(CONFIG_DIR, '我的插件配置说明.txt')

def _ensure_files():
    """首次运行：生成配置 JSON + 同目录的说明文件"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({"开关": True, "超时": 30}, f, ensure_ascii=False, indent=2)
    if not os.path.exists(DOC_FILE):
        with open(DOC_FILE, 'w', encoding='utf-8') as f:
            f.write("开关：true=启用，false=关闭\n超时：单位秒\n")

def on_start(bot):
    _ensure_files()
    bot.log(f"我的插件：配置文件 → {os.path.abspath(CONFIG_FILE)}")
    bot.log(f"我的插件：配置说明 → {os.path.abspath(DOC_FILE)}")
```

> 现成范例：`ollama.py`（配置文件 `data/plugins_data/ollama/ollama.json` + 同目录
> `ollama配置说明.txt`，支持热加载与环境变量覆盖）、`每日签到.py`（`DATA_DIR/lottery.json`）。
> 配置优先级建议：**环境变量 > JSON 文件 > 代码内置默认值**（方便 Docker / Ubuntu 部署时用环境变量覆盖）。

## 小技巧

- 插件是常驻内存的，模块里的全局变量会一直保留（比如记数、缓存）
- 想在插件里调外部 API？直接 `import requests` 即可（程序已安装）
- 想调试？在插件里 `print(...)` 或 `bot.log(...)` 会显示在机器人日志里

## 内置示例插件

| 插件 | 指令 | 说明 |
|---|---|---|
| `示例插件.py` | `/你好` | 回复型插件模板 |
| `多文件示例/` | `/计算 1+2` | 多文件插件模板 |
| `桥接转发示例.py` | — | 连接型插件模板（转发 Webhook） |
| `每日签到.py` | `/签到`、`/积分`、`/抽奖` | 签到领积分、今日第几个签到、趣味抽奖（奖项概率见其 lottery.json） |
| `每日运势.py` | `/运势` | 每日运势，结果当天固定 |
| `骰子.py` | `/骰子` | 掷骰子 |
