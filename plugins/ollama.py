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

配置（两种方式，环境变量优先；改代码后需在 Web 后台「插件」页重新加载）：
  1) 环境变量（推荐用于 Ubuntu Server / Docker 部署，无需改代码）：
       BOT_OLLAMA_HOST     Ollama 地址，支持 127.0.0.1:11434 / http://ollama:11434 写法
                            · 机器人跑在宿主机、Ollama 在容器  → http://127.0.0.1:11434
                            · 机器人和 Ollama 都在容器里       → http://ollama:11434
       BOT_OLLAMA_MODEL    模型名（默认 qwen2.5）
       BOT_OLLAMA_THINK    false(默认，关闭思考) / true / none(不发送该参数)
       BOT_OLLAMA_HISTORY  每个用户记忆轮数（默认 6，0=单轮）
       BOT_OLLAMA_TIMEOUT  单次请求超时秒数（默认 300；CPU 跑大模型可调大）
       BOT_OLLAMA_FORCE    true(默认)=强制接管所有普通消息（**即使主程序填了 API Key**）
                           false=只有主程序没配内置 AI（或 ai.enabled=false）时才接管
  2) 直接改下面的常量（本地简单使用）

  兼容说明：若系统里已有 OLLAMA_HOST 环境变量（ollama CLI 用的 "主机:端口" 格式），
  本插件也会读取并自动补上 http:// 前缀。
"""

import collections
import os
import threading
import requests

PLUGIN = {
    "name": "Ollama 本地 AI",
    "description": "用本地 Ollama 接管普通消息回复（默认强制接管，不受主程序 API Key 影响）",
    "version": "2.3.0",
    "author": "你",
}


def _env(name: str) -> str:
    v = os.environ.get(name)
    return v.strip() if v else ""


def _normalize_host(h: str) -> str:
    """把 11434 / 127.0.0.1:11434 / http://ollama:11434 统一成 http://主机:端口"""
    h = (h or "").strip().rstrip("/")
    if not h:
        return "http://127.0.0.1:11434"
    if "://" not in h:
        h = "http://" + h
    body = h.split("://", 1)[1]
    if "/" not in body and ":" not in body:      # 只写了主机名 → 补默认端口
        h += ":11434"
    return h


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name).lower()
    if v in ("1", "true", "yes", "on", "enable", "enabled"):
        return True
    if v in ("0", "false", "no", "off", "disable", "disabled"):
        return False
    return default


def _env_think(default=False):
    v = _env("BOT_OLLAMA_THINK").lower()
    if v in ("none", "null", "-"):
        return None
    if v in ("0", "false", "no", "off", "disable", "disabled"):
        return False
    if v in ("1", "true", "yes", "on", "enable", "enabled"):
        return True
    return default


# ====== 配置（环境变量优先，其次用这里的默认值） ======
OLLAMA_HOST = _normalize_host(_env("BOT_OLLAMA_HOST") or _env("OLLAMA_HOST"))
OLLAMA_MODEL = _env("BOT_OLLAMA_MODEL") or "qwen2.5"   # 先运行 ollama list 看有哪些模型
HISTORY_LEN = _env_int("BOT_OLLAMA_HISTORY", 6)        # 每个用户记住最近 6 轮对话（0=单轮）
_TIMEOUT = _env_int("BOT_OLLAMA_TIMEOUT", 300)         # 单次请求超时（秒）
# 是否思考（等价于 CLI 的 `ollama run 模型 --think=false`）：
#   False=关闭思考（推荐） True=开启 None=不带此参数
THINK = _env_think(False)
# 强制接管回复：True（默认）= 所有普通消息都由本插件回答，**即使主程序填了 API Key**；
#               False = 只有主程序没配内置 AI（或 ai.enabled=false）时才接管。
FORCE_TAKEOVER = _env_bool("BOT_OLLAMA_FORCE", True)
# 最多同时记住多少个用户/群的会话（超过就丢掉最久没说话的）
MAX_SESSIONS = _env_int("BOT_OLLAMA_MAX_SESSIONS", 200)
# ====== 结束配置 ======

# 记录服务端是否支持 think 参数（不支持时自动降级，避免每条消息都失败重试）
_think_ok = True

# 简易会话记忆（按用户 openid 存，进程内，重启清空）
# 用 OrderedDict 按"最近使用"排序：超过 MAX_SESSIONS 时从最久没用的开始丢，
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


def _system_prompt():
    return "你是一个乐于助人的 AI 助手，请用简洁清晰的中文回答问题。"


def _ask_ollama(messages):
    """调用 Ollama /api/chat（非流式），返回 (回复文本, 错误信息)

    think 参数（等价 CLI 的 --think=false）：False 让思考型模型跳过思考直接回答；
    老版本 Ollama 不认识该字段时会返回 400，这里会自动去掉该参数重试一次并记住。
    """
    global _think_ok
    url = OLLAMA_HOST.rstrip("/") + "/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.7},
    }
    if THINK is not None and _think_ok:
        payload["think"] = bool(THINK)
    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT)
        # 服务端不认 think（老版本）→ 去掉后重试一次，并记下来
        if resp.status_code == 400 and "think" in payload and "think" in resp.text.lower():
            _think_ok = False
            payload.pop("think", None)
            resp = requests.post(url, json=payload, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None, f"Ollama 返回错误 {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        msg = data.get("message") or {}
        # 思考内容在 message.thinking：不发给用户，只取正式回答 message.content
        text = (msg.get("content") or "").strip()
        if not text:
            if (msg.get("thinking") or "").strip():
                return None, ("模型只输出了思考内容（未给出回答）；"
                              "建议把 THINK 设为 False，或换用非思考型模型")
            return None, "Ollama 没有返回内容"
        return text, None
    except requests.exceptions.ConnectionError:
        return None, (f"连接不上 Ollama（当前地址 {OLLAMA_HOST}）。"
                      "Docker 部署注意：机器人在宿主机→容器需映射端口（-p 11434:11434）"
                      "并填 http://127.0.0.1:11434；机器人和 Ollama 都在容器内→"
                      "填容器名，如 http://ollama:11434")
    except requests.exceptions.ReadTimeout:
        return None, (f"Ollama 响应超时（{_TIMEOUT}s，模型 {OLLAMA_MODEL}）；"
                      "可调大环境变量 BOT_OLLAMA_TIMEOUT，或换更小的模型")
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
        _histories[uid] = messages[-HISTORY_LEN * 2:] if HISTORY_LEN > 0 else []
        _histories.move_to_end(uid)
        # 只保留最近活跃的 MAX_SESSIONS 个会话（0 或负数 = 不限制）
        if MAX_SESSIONS > 0:
            while len(_histories) > MAX_SESSIONS:
                _histories.popitem(last=False)


def _ai_usable_by_main(bot) -> bool:
    """主程序内置 AI 是否可用（ai.enabled 打开 且 api_key/base_url/model 填全）。

    注意：内置 AI 可用时，只要本插件不接管，消息就会由内置 AI 回答；
    FORCE_TAKEOVER=True（默认）时我们会强制接管，所以这里只在非强制模式下用。
    """
    try:
        cfg = getattr(bot, 'config', None) or {}
        if not bool((cfg.get('ai') or {}).get('enabled', True)):
            return False
        return all(str(cfg.get(k) or '').strip() for k in ('api_key', 'base_url', 'model'))
    except Exception:
        return False


def on_start(bot):
    """启动时说明接管方式（插件分发给插件的日志）"""
    try:
        if bot is not None:
            if FORCE_TAKEOVER:
                bot.log("Ollama 插件已接管普通消息（FORCE_TAKEOVER=True）："
                        "即使主程序填了 API Key，也强制由本地 Ollama 回答")
            else:
                bot.log("Ollama 插件为非强制模式（FORCE_TAKEOVER=False）："
                        "仅当主程序没配内置 AI（或 ai.enabled=false）时才接管")
    except Exception:
        pass


def on_message(msg, bot):
    """普通消息 → 用 Ollama 回答。

    接管规则（插件在内置 AI 之前被分发）：
      FORCE_TAKEOVER=True （默认）：所有普通消息都由本插件回答，**即使主程序填了 API Key**；
                                    出错也返回错误提示文本，绝不让内置 AI 顶替回答。
      FORCE_TAKEOVER=False        ：主程序内置 AI 可用时不接管（返回 None，交给内置 AI），
                                    没配 AI 或 ai.enabled=false 时才接管。
    """
    try:
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
    if not FORCE_TAKEOVER and _ai_usable_by_main(bot):
        return None

    uid = msg.get("unified_openid") or msg.get("user_openid") or "unknown"

    history = _get_history(uid)
    messages = [{"role": "system", "content": _system_prompt()}]
    messages.extend(history)
    messages.append({"role": "user", "content": content})

    if bot is not None:
        bot.log(f"Ollama 回答({OLLAMA_MODEL}): {content[:40]}")

    with _gen_lock:
        try:
            text, err = _ask_ollama(messages)
        except Exception as e:
            text, err = None, str(e)

    if err:
        if bot is not None:
            bot.error(f"Ollama 出错: {err}")
        return f"⚠️ {err}\n\n检查：1) Ollama 是否已启动  2) 模型名是否正确（ollama list 查看）"

    if not text:
        # 强制接管模式下也不能返回空（空会被当作“插件不回复”而转给内置 AI）
        return "⚠️ Ollama 没有返回内容，请稍后再试。"

    if HISTORY_LEN > 0:
        history.append({"role": "user", "content": content})
        history.append({"role": "assistant", "content": text})
        _save_history(uid, history)

    return text
