"""
Ollama 本地 AI 插件 - 默认用本地 Ollama 回答用户问题，不消耗 API 额度
====================================================================
功能：用户直接提问（如 "你好"、"写首诗"），机器人就调用本机 Ollama
      的本地模型回答，不再走 DeepSeek API。

内置指令（/帮助 /clear /转移私聊到群聊 等）和关键词回复由主程序先处理，
不会受影响；只有普通问题会交给本插件。

依赖：
  1. 装好 Ollama（https://ollama.com 下载，或 Docker 容器）并启动
  2. 拉取至少一个模型，例如：  ollama pull qwen2.5
  3. 确认 Ollama 能访问（默认 http://127.0.0.1:11434）

==== 配置文件（推荐用法）====
所有可修改的设置都放在一个 JSON 文件里，**首次运行插件时自动生成**：

    配置文件：data/plugins_data/ollama/ollama.json
    配置说明：data/plugins_data/ollama/ollama配置说明.txt   （同目录，自动生成）

改完保存即可生效（插件下一条消息时自动重新加载），也可以在 Web 后台
「插件」页点「重新加载」。首次运行/加载插件时，控制台会打印这两个文件的完整路径。

配置优先级：**环境变量 > ollama.json > 本文件里的内置默认值**
（环境变量适合 Ubuntu Server / Docker 部署，见下面的说明文件）

兼容说明：若系统里已有 OLLAMA_HOST 环境变量（ollama CLI 用的 "主机:端口" 格式），
本插件也会读取并自动补上 http:// 前缀。
"""

import collections
import json
import os
import threading

import requests

PLUGIN = {
    "name": "Ollama 本地 AI",
    "description": "用本地 Ollama 接管普通消息回复（配置文件：data/plugins_data/ollama/ollama.json）",
    "version": "2.4.0",
    "author": "L-web666",
}

# ==================== 配置文件位置 ====================
# DATA_DIR 由主程序注入（data/plugins_data/<插件名>）；单独运行调试时用下面的默认值
CONFIG_DIR = globals().get('DATA_DIR') or os.path.join('data', 'plugins_data', 'ollama')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'ollama.json')
DOC_FILE = os.path.join(CONFIG_DIR, 'ollama配置说明.txt')

# 内置默认值（ollama.json 里缺哪项就用这里的）
DEFAULT_CONFIG = {
    "host": "http://127.0.0.1:11434",   # Ollama 地址
    "model": "qwen2.5",                 # 模型名（ollama list 可查看本机已有模型）
    "think": False,                     # 是否让思考型模型先"思考"：false / true / "none"
    "history": 6,                       # 每个用户记住最近几轮对话（0=不记，单轮问答）
    "timeout": 300,                     # 单次请求超时（秒），CPU 跑大模型可调大
    "force_takeover": True,             # true=所有普通消息都由本插件回答
    "max_sessions": 200,                # 最多同时记住多少个用户/群的会话（0=不限制）
    "temperature": 0.7,                 # 回答随机度：0~1，越小越稳定
    "system_prompt": "你是一个乐于助人的 AI 助手，请用简洁清晰的中文回答问题。",
}

# 环境变量（可选）：名字 -> 配置项。环境变量的值优先于 ollama.json
ENV_KEYS = {
    "BOT_OLLAMA_HOST": "host",
    "BOT_OLLAMA_MODEL": "model",
    "BOT_OLLAMA_THINK": "think",
    "BOT_OLLAMA_HISTORY": "history",
    "BOT_OLLAMA_TIMEOUT": "timeout",
    "BOT_OLLAMA_FORCE": "force_takeover",
    "BOT_OLLAMA_MAX_SESSIONS": "max_sessions",
    "BOT_OLLAMA_TEMPERATURE": "temperature",
    "BOT_OLLAMA_PROMPT": "system_prompt",
}

CONFIG_DOC_HEAD = """═══════════════════════════════════════════════════════════════
        Ollama 本地 AI 插件 · 配置文件说明
        （本文件对应同目录的 ollama.json，自动生成，可直接删除重新生成）
═══════════════════════════════════════════════════════════════

【这个文件是干什么的】
  本文件说明同目录下的 ollama.json 里每一项是什么意思、能填什么。
  ollama.json 是 plugins/ollama.py 插件的配置文件，首次运行插件时自动生成，
  里面所有设置都可以改。

    配置文件：data/plugins_data/ollama/ollama.json
    说明文件：data/plugins_data/ollama/ollama配置说明.txt（就是本文件）

【怎么改】
  1. 用记事本 / VS Code 打开 ollama.json，改完保存即可。
  2. 保存后**下一条消息自动生效**（插件会检测文件修改时间并重新加载），
     也可以在 Web 后台「插件」页点「重新加载」。
  3. 万一 JSON 写坏了（少了逗号/引号），插件不会崩：本次会用默认值并在日志里提示，
     改好保存后自动恢复。
  4. 优先级：环境变量 > ollama.json > 插件内置默认值。

"""

CONFIG_DOC_TAIL = """
【环境变量（可选）】
  适合 Ubuntu Server / Docker 部署：不想改 JSON 时可以用环境变量覆盖上面的配置，
  环境变量一旦设置，优先级高于 ollama.json。

  BOT_OLLAMA_HOST            Ollama 地址（等价 host）
  BOT_OLLAMA_MODEL           模型名（等价 model）
  BOT_OLLAMA_THINK           false / true / none（等价 think）
  BOT_OLLAMA_HISTORY         记忆轮数（等价 history）
  BOT_OLLAMA_TIMEOUT         请求超时秒数（等价 timeout）
  BOT_OLLAMA_FORCE           true / false（等价 force_takeover）
  BOT_OLLAMA_MAX_SESSIONS    会话数上限（等价 max_sessions）
  BOT_OLLAMA_TEMPERATURE     随机度（等价 temperature）
  BOT_OLLAMA_PROMPT          系统提示词（等价 system_prompt）

  另外：系统里已有的 OLLAMA_HOST（ollama CLI 用的 "主机:端口" 格式）也会被识别。

【地址（host）怎么写】
  · 机器人和 Ollama 在同一台电脑           → http://127.0.0.1:11434（默认）
  · 机器人跑在宿主机、Ollama 跑在 Docker   → http://127.0.0.1:11434，同时要映射端口
                                             docker run -p 11434:11434 ...
  · 机器人和 Ollama 都在 Docker 网络里     → http://ollama:11434（用容器名/服务名）
  · 只写 11434 或 127.0.0.1:11434 也可以，插件会自动补上 http:// 和端口。

【常见问题】
  Q: 提示"连接不上 Ollama"？
  A: 先在本机执行 `ollama list` 确认服务已启动；再用浏览器访问 host 地址看是否通；
     Docker 场景检查端口映射，并确认 host 填的是容器能访问到的地址。

  Q: 提示"模型只输出了思考内容"？
  A: 把 think 改成 false（默认），或换用非思考型模型（例如 qwen2.5 系列普通版）。

  Q: 回答太慢 / 超时？
  A: 把 timeout 调大（例如 600），或换更小的模型、减少 history 轮数。

  Q: 怎么让内置 AI 也能回答（本插件不接管）？
  A: 把 force_takeover 改成 false：这样主程序配好 API Key 时由内置 AI 回答，
     只有内置 AI 不可用（没配 Key 或 ai.enabled=false）时才由本插件回答。

  Q: 会话记忆存在哪？会不会占内存？
  A: 记忆只在内存里（重启清空），不写磁盘；最多保留 max_sessions 个用户/群的会话，
     超出后自动丢掉最久没说话的。想完全关闭记忆把 history 设为 0。

【插件信息】
  插件：Ollama 本地 AI（plugins/ollama.py）
  版本：{version}
  作者：{author}
"""


# ==================== 工具函数 ====================
def _env(name: str) -> str:
    v = os.environ.get(name)
    return v.strip() if v else ""


def _normalize_host(h) -> str:
    """把 11434 / 127.0.0.1:11434 / http://ollama:11434 统一成 http://主机:端口"""
    h = str(h or "").strip().rstrip("/")
    if not h:
        return DEFAULT_CONFIG["host"]
    if h.isdigit():                      # 只写了端口号 → 按本机处理
        h = "127.0.0.1:" + h
    if "://" not in h:
        h = "http://" + h
    body = h.split("://", 1)[1]
    if "/" not in body and ":" not in body:      # 只写了主机名 → 补默认端口
        h += ":11434"
    return h


def _as_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "").strip().lower()
    if s in ("1", "true", "yes", "on", "enable", "enabled", "是"):
        return True
    if s in ("0", "false", "no", "off", "disable", "disabled", "否"):
        return False
    return default


def _as_int(v, default: int) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _as_float(v, default: float) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _as_think(v, default=False):
    """think 支持三种值：true / false / "none"（none=请求里不带该参数）"""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("none", "null", "-", ""):
        return None
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


# ==================== 读写配置文件 ====================
def _ensure_files(logger=None):
    """首次运行：自动生成 ollama.json 与同目录的说明文件；返回是否新建了配置"""
    created = False
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError as e:
        if logger:
            logger.error(f"[ollama] 无法创建配置目录 {os.path.abspath(CONFIG_DIR)}：{e}")
        return False
    if not os.path.exists(CONFIG_FILE):
        try:
            data = {"_说明": "本文件的每一项说明见同目录的 ollama配置说明.txt；"
                             "也可用环境变量覆盖（见说明文件）"}
            data.update(DEFAULT_CONFIG)
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            created = True
        except OSError as e:
            if logger:
                logger.error(f"[ollama] 生成配置文件失败 {os.path.abspath(CONFIG_FILE)}：{e}")
    # 说明文件：不存在就生成；插件升级后（版本号变化）自动更新
    try:
        doc = (CONFIG_DOC_HEAD
               + _items_doc()
               + CONFIG_DOC_TAIL.format(version=PLUGIN["version"], author=PLUGIN["author"]))
        need = True
        if os.path.exists(DOC_FILE):
            with open(DOC_FILE, encoding='utf-8') as f:
                need = (f"版本：{PLUGIN['version']}" not in f.read())
        if need:
            with open(DOC_FILE, 'w', encoding='utf-8') as f:
                f.write(doc)
    except OSError:
        pass
    return created


def _items_doc() -> str:
    """把各项配置渲染成说明文字（跟着 DEFAULT_CONFIG 自动保持一致）"""
    rows = [
        ("host", "文本", "http://127.0.0.1:11434",
         "Ollama 服务地址。可以只写 11434 / 127.0.0.1:11434，插件会自动补 http:// 和端口"),
        ("model", "文本", "qwen2.5",
         "使用的模型名。先在本机执行 ollama list 看有哪些模型，填错会收到 Ollama 的报错"),
        ("think", "true/false/\"none\"", "false",
         "是否让思考型模型先输出思考再回答。false=关闭（推荐，回答更快）；\n"
         "                  true=开启；\"none\"=请求里不带这个参数（老版本 Ollama 用）"),
        ("history", "整数", "6",
         "每个用户/群记住最近几轮对话（0=不记忆，每次都是单轮问答；建议 0~10）"),
        ("timeout", "整数（秒）", "300",
         "单次请求超时时间。CPU 上跑大模型可能要几分钟，超时就调大"),
        ("force_takeover", "true/false", "true",
         "true=所有普通消息都由本地 Ollama 回答（即使主程序填了 API Key）；\n"
         "                  false=只有在主程序没配内置 AI（或 ai.enabled=false）时才接管"),
        ("max_sessions", "整数", "200",
         "最多同时记住多少个用户/群的会话（超出后丢掉最久没说话的）；0=不限制\n"
         "                  （记忆只在内存里，重启清空，不占磁盘）"),
        ("temperature", "数字 0~1", "0.7",
         "回答的随机程度：越小越稳定保守，越大越发散有创意"),
        ("system_prompt", "文本", "你是一个乐于助人的 AI 助手，请用简洁清晰的中文回答问题。",
         "系统提示词（本插件回答时的人设）。留空则使用默认值"),
    ]
    out = ["【配置项一览】（改完 ollama.json 保存即可，下一条消息自动生效）\n"]
    for key, typ, default, desc in rows:
        out.append(f"  {key}")
        out.append(f"      类型/可选值 : {typ}")
        out.append(f"      默认值      : {default}")
        out.append(f"      说明        : {desc}")
        out.append("")
    return "\n".join(out)


_cfg_lock = threading.Lock()
_cfg_mtime = None
CFG = dict(DEFAULT_CONFIG)


def _load_config():
    """读 ollama.json（缺项用默认值）→ 应用环境变量覆盖 → 返回最终配置"""
    global _last_error
    raw = {}
    _last_error = ""                     # 每次都重置：读成功就不该残留上次的错误
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw = data
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        # JSON 写坏了也不能让插件崩：用默认值继续，并提示用户
        _last_error = f"配置文件读取失败（{e}），本次使用默认配置"
    cfg = dict(DEFAULT_CONFIG)
    for k in DEFAULT_CONFIG:
        if k in raw and raw[k] is not None:
            cfg[k] = raw[k]

    def env_of(key):
        for name, k in ENV_KEYS.items():
            if k == key and _env(name):
                return _env(name)
        return ""

    host = env_of("host") or _env("OLLAMA_HOST") or cfg["host"]
    return {
        "host": _normalize_host(host),
        "model": str(env_of("model") or cfg["model"] or "").strip() or DEFAULT_CONFIG["model"],
        "think": _as_think(env_of("think") if env_of("think") else cfg["think"],
                           DEFAULT_CONFIG["think"]),
        "history": max(0, _as_int(env_of("history") or cfg["history"], DEFAULT_CONFIG["history"])),
        "timeout": max(5, _as_int(env_of("timeout") or cfg["timeout"], DEFAULT_CONFIG["timeout"])),
        "force_takeover": _as_bool(env_of("force_takeover") if env_of("force_takeover")
                                   else cfg["force_takeover"], DEFAULT_CONFIG["force_takeover"]),
        "max_sessions": max(0, _as_int(env_of("max_sessions") or cfg["max_sessions"],
                                       DEFAULT_CONFIG["max_sessions"])),
        "temperature": min(2.0, max(0.0, _as_float(env_of("temperature") or cfg["temperature"],
                                                   DEFAULT_CONFIG["temperature"]))),
        "system_prompt": str(env_of("system_prompt") or cfg["system_prompt"]
                             or DEFAULT_CONFIG["system_prompt"]),
    }


_last_error = ""


def refresh_config(bot=None, force: bool = False) -> bool:
    """重新加载配置（文件没变就跳过）。返回是否真的重载了。

    改完 ollama.json 保存后，下一条消息就会自动生效，不用重启机器人。
    """
    global CFG, _cfg_mtime, _last_error
    with _cfg_lock:
        try:
            mtime = os.path.getmtime(CONFIG_FILE)
        except OSError:
            mtime = None
        if not force and mtime is not None and mtime == _cfg_mtime:
            return False
        CFG = _load_config()
        _cfg_mtime = mtime
        err = _last_error
    if bot is not None:
        if err:
            bot.error(f"Ollama {err}；文件：{os.path.abspath(CONFIG_FILE)}")
        elif not force:
            bot.log(f"Ollama 配置已重新加载：模型={CFG['model']} 地址={CFG['host']}")
    return True


def config_paths():
    """配置文件与说明文件的绝对路径（供控制台提示）"""
    return os.path.abspath(CONFIG_FILE), os.path.abspath(DOC_FILE)


# 加载插件时先建好配置文件（首次运行自动生成），并读取一次
_first_run_created = _ensure_files()     # 记住是不是本次首次生成，on_start 里要提示用户
refresh_config(force=True)

# 记录服务端是否支持 think 参数（不支持时自动降级，避免每条消息都失败重试）
_think_ok = True

# 简易会话记忆（按用户 openid 存，进程内，重启清空）
# 用 OrderedDict 按"最近使用"排序：超过 max_sessions 时从最久没用的开始丢，
# 否则机器人跑几个月、用户很多时这份 dict 会一直变大（内存只增不减）。
_histories = collections.OrderedDict()
_hist_lock = threading.Lock()

# 锁：Ollama 生成较慢，避免并发打爆本地服务
_gen_lock = threading.Lock()


def match(msg):
    """只接管普通文字消息；任何以 / 开头的指令（内置或其他插件的）都不拦截"""
    content = (msg.get("content") or "").strip()
    if not content:
        return False
    # 以 / 开头的都是指令：内置的交给主程序，非内置的交给其他插件/主程序
    if content.startswith("/"):
        return False
    return True


def _ask_ollama(messages):
    """调用 Ollama /api/chat（非流式），返回 (回复文本, 错误信息)

    think 参数（等价 CLI 的 --think=false）：False 让思考型模型跳过思考直接回答；
    老版本 Ollama 不认识该字段时会返回 400，这里会自动去掉该参数重试一次并记住。
    """
    global _think_ok
    host, model, think, timeout = CFG["host"], CFG["model"], CFG["think"], CFG["timeout"]
    url = host.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": CFG["temperature"]},
    }
    if think is not None and _think_ok:
        payload["think"] = bool(think)
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        # 服务端不认 think（老版本）→ 去掉后重试一次，并记下来
        if resp.status_code == 400 and "think" in payload and "think" in resp.text.lower():
            _think_ok = False
            payload.pop("think", None)
            resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code != 200:
            return None, f"Ollama 返回错误 {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        msg = data.get("message") or {}
        # 思考内容在 message.thinking：不发给用户，只取正式回答 message.content
        text = (msg.get("content") or "").strip()
        if not text:
            if (msg.get("thinking") or "").strip():
                return None, ("模型只输出了思考内容（未给出回答）；"
                              "建议把 ollama.json 里的 think 设为 false，或换用非思考型模型")
            return None, "Ollama 没有返回内容"
        return text, None
    except requests.exceptions.ConnectionError:
        return None, (f"连接不上 Ollama（当前地址 {host}）。"
                      "请检查 ollama.json 里的 host：Docker 部署时机器人在宿主机→容器需映射端口"
                      "（-p 11434:11434）并填 http://127.0.0.1:11434；"
                      "机器人和 Ollama 都在容器内→填容器名，如 http://ollama:11434")
    except requests.exceptions.ReadTimeout:
        return None, (f"Ollama 响应超时（{timeout}s，模型 {model}）；"
                      "可把 ollama.json 里的 timeout 调大，或换更小的模型")
    except Exception as e:
        return None, f"调用 Ollama 出错: {e}"


def _get_history(uid):
    with _hist_lock:
        msgs = _histories.get(uid)
        if msgs is None:
            return []
        _histories.move_to_end(uid)          # 标记为"最近用过"
        return list(msgs)


def _save_history(uid, messages):
    with _hist_lock:
        keep = CFG["history"]
        _histories[uid] = messages[-keep * 2:] if keep > 0 else []
        _histories.move_to_end(uid)
        # 只保留最近活跃的 max_sessions 个会话（0 = 不限制）
        limit = CFG["max_sessions"]
        if limit > 0:
            while len(_histories) > limit:
                _histories.popitem(last=False)


def _ai_usable_by_main(bot) -> bool:
    """主程序内置 AI 是否可用（ai.enabled 打开 且 api_key/base_url/model 填全）。

    注意：内置 AI 可用时，只要本插件不接管，消息就会由内置 AI 回答；
    force_takeover=true（默认）时我们会强制接管，所以这里只在非强制模式下用。
    """
    try:
        cfg = getattr(bot, 'config', None) or {}
        if not bool((cfg.get('ai') or {}).get('enabled', True)):
            return False
        return all(str(cfg.get(k) or '').strip() for k in ('api_key', 'base_url', 'model'))
    except Exception:
        return False


def on_start(bot):
    """启动时：告诉用户配置文件在哪 + 当前生效的设置"""
    global _first_run_created
    created = _ensure_files(bot) or _first_run_created   # 加载时生成过也算首次
    _first_run_created = False
    refresh_config(force=True)
    cfg_path, doc_path = config_paths()
    try:
        if bot is None:
            return
        if created:
            bot.log(f"Ollama 插件：首次运行，已自动生成配置文件 → {cfg_path}")
        else:
            bot.log(f"Ollama 插件：配置文件 → {cfg_path}")
        bot.log(f"Ollama 插件：配置说明 → {doc_path}")
        bot.log("Ollama 插件：当前设置 模型={model} 地址={host} 强制接管={force} "
                "记忆={history}轮 超时={timeout}s 随机度={temperature}".format(
                    model=CFG['model'], host=CFG['host'],
                    force='是' if CFG['force_takeover'] else '否',
                    history=CFG['history'], timeout=CFG['timeout'],
                    temperature=CFG['temperature']))
        if CFG['force_takeover']:
            bot.log("Ollama 插件已接管普通消息（force_takeover=true）："
                    "即使主程序填了 API Key，也强制由本地 Ollama 回答")
        else:
            bot.log("Ollama 插件为非强制模式（force_takeover=false）："
                    "仅当主程序没配内置 AI（或 ai.enabled=false）时才接管")
    except Exception:
        pass


def on_message(msg, bot):
    """普通消息 → 用 Ollama 回答。

    接管规则（插件在内置 AI 之前被分发）：
      force_takeover=true （默认）：所有普通消息都由本插件回答，**即使主程序填了 API Key**；
                                   出错也返回错误提示文本，绝不让内置 AI 顶替回答。
      force_takeover=false        ：主程序内置 AI 可用时不接管（返回 None，交给内置 AI），
                                   没配 AI 或 ai.enabled=false 时才接管。
    """
    try:
        refresh_config(bot)          # 改了 ollama.json 保存后，下一条消息自动生效
        return _handle(msg, bot)
    except Exception as e:
        # 兜底：绝不让异常冒出去（否则主程序会当作“插件没回复”而转给内置 AI）
        try:
            if bot is not None:
                bot.error(f"Ollama 插件内部错误: {e}")
        except Exception:
            pass
        return f"⚠️ Ollama 插件内部错误：{e}"


def _handle(msg, bot):
    content = (msg.get("content") or "").strip()
    if not content:
        return None
    # 非强制模式：内置 AI 能用就把这条消息让给内置 AI
    if not CFG["force_takeover"] and _ai_usable_by_main(bot):
        return None

    uid = msg.get("unified_openid") or msg.get("user_openid") or "unknown"

    history = _get_history(uid)
    messages = [{"role": "system", "content": CFG["system_prompt"]}]
    messages.extend(history)
    messages.append({"role": "user", "content": content})

    if bot is not None:
        bot.log(f"Ollama 回答({CFG['model']}): {content[:40]}")

    with _gen_lock:
        try:
            text, err = _ask_ollama(messages)
        except Exception as e:
            text, err = None, str(e)

    if err:
        if bot is not None:
            bot.error(f"Ollama 出错: {err}")
        return (f"⚠️ {err}\n\n检查：1) Ollama 是否已启动  2) 模型名是否正确（ollama list 查看）"
                f"\n配置：{config_paths()[0]}")

    if not text:
        # 强制接管模式下也不能返回空（空会被当作“插件不回复”而转给内置 AI）
        return "⚠️ Ollama 没有返回内容，请稍后再试。"

    if CFG["history"] > 0:
        history.append({"role": "user", "content": content})
        history.append({"role": "assistant", "content": text})
        _save_history(uid, history)

    return text
