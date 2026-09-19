"""
每日签到插件 - 签到领积分、连签加成、今日签到序号、趣味抽奖
================================================================
指令：
  /签到            当天签到，显示今天第几位签到并领积分
  /积分            查询积分、护盾等
  /抽奖            消耗 10 积分抽一次趣味奖（整活文案 / 连签护盾道具）

抽奖奖项与概率配置文件：
  <DATA_DIR>/lottery.json （首次自动生成，可直接编辑后重新加载插件生效）
  权重越大越容易中：某奖概率 = 该奖 weight ÷ 全部 weight 之和。建议 1~1000。

数据存储（插件系统规范）：
  - 签到数据：DATA_FILE（data/plugins_data/每日签到.json）
  - 抽奖配置：DATA_DIR/lottery.json（本插件专属目录内）
"""

import json
import os
import random
import threading
from datetime import datetime, timezone, timedelta

PLUGIN = {
    "name": "每日签到",
    "description": "/签到 领积分（今日第几个）、/积分 查询、/抽奖 趣味抽奖（奖项概率可配置）",
    "version": "6.0.0",
    "author": "你",
}

COMMANDS = ["/签到", "/积分", "/抽奖"]

# 数据文件：插件系统注入（data/plugins_data/每日签到.json），插件内直接用
DATA_FILE = globals().get('DATA_FILE', os.path.join('data', 'plugins_data', '每日签到.json'))
# 抽奖配置文件：DATA_DIR 是插件专属目录（系统已自动创建）
DATA_DIR = globals().get('DATA_DIR', os.path.join('data', 'plugins_data', '每日签到'))
LOTTERY_CONFIG_FILE = os.path.join(DATA_DIR, 'lottery.json')
# 旧版本数据位置（迁移用）
_LEGACY_FILE = os.path.join('data', 'plugins_data', 'checkin.json')

LOTTERY_COST = 10   # 每次抽奖消耗积分
_META_KEY = '_meta'  # 保存当日签到序号 {date, seq}（openid 不会撞这个键）

_lock = threading.Lock()
_data = {}          # {openid: {nick, last, streak, total, points, shield, today_order}}
_lottery = []       # 抽奖奖项列表（从配置文件读）
_migrated = False


def _today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')


# ==================== 数据读写 ====================

def _load():
    global _data, _migrated
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, encoding='utf-8') as f:
                _data = json.load(f) or {}
            return
    except Exception:
        pass
    if not _migrated:
        _migrated = True
        try:
            if os.path.exists(_LEGACY_FILE):
                with open(_LEGACY_FILE, encoding='utf-8') as f:
                    _data = json.load(f) or {}
                os.remove(_LEGACY_FILE)
                _save()
                return
        except Exception:
            pass
    _data = {}


def _save():
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _streak_bonus(streak: int) -> int:
    """连续签到加成：连签第 2 天起每天 +1，最多 +5"""
    if streak <= 1:
        return 0
    return min(streak - 1, 5)


def _next_today_order(today: str) -> int:
    """返回今天下一个签到序号（跨天自动重置）"""
    meta = _data.get(_META_KEY) or {}
    if meta.get('date') != today:
        seq = 1
    else:
        seq = int(meta.get('seq', 0)) + 1
    _data[_META_KEY] = {'date': today, 'seq': seq}
    return seq


# ==================== 抽奖配置（可编辑 JSON） ====================

# 默认抽奖奖项模板（第一次运行自动写入 lottery.json，之后以文件为准）
# effect: "nothing"=整活文案（无持久） / "shield"=连签护盾道具
_DEFAULT_LOTTERY = [
    # —— 保底 ——
    {"name": "空气奖", "icon": "🍃", "weight": 120,
     "text": "抽到了新鲜空气一份…含量 100%，环保又有益健康！", "effect": "nothing"},

    # —— 无厘头整活 ——
    {"name": "原地转三圈", "icon": "💫", "weight": 60,
     "text": "获得「原地转三圈」体验券：现在、立刻、马上转！晕了别找我～", "effect": "nothing"},
    {"name": "夸夸卡", "icon": "🌈", "weight": 50,
     "text": "获得「群主夸夸卡」一张：可在群里点名让群主夸你一句（群主不认账概不负责）", "effect": "nothing"},
    {"name": "摸鱼许可", "icon": "🐟", "weight": 55,
     "text": "获得「今日摸鱼许可」：有效期至下班，被抓包请出示本卡（无效版）", "effect": "nothing"},
    {"name": "表情包自由", "icon": "😂", "weight": 50,
     "text": "获得今日「表情包自由权」：快去群里斗图，把收藏夹清空！", "effect": "nothing"},
    {"name": "免打扰一小时", "icon": "🔕", "weight": 40,
     "text": "获得「免打扰一小时」特权…虽然只是嘴上说说，消息该回还得回", "effect": "nothing"},
    {"name": "主角光环", "icon": "✨", "weight": 35,
     "text": "接下来 10 分钟你自带主角光环：走路带风、泡面必香、抢红包手气 +0.0001%", "effect": "nothing"},
    {"name": "隐形斗篷", "icon": "🫥", "weight": 35,
     "text": "获得「隐形斗篷」一件…可惜是电子的，群主照样看得见你", "effect": "nothing"},
    {"name": "好运倒计时", "icon": "⏳", "weight": 30,
     "text": "你的好运正在路上：预计 3 个群消息内送达，请保持在线", "effect": "nothing"},
    {"name": "神秘锦囊", "icon": "🎐", "weight": 30,
     "text": "获得「神秘锦囊」：里面装着一句话——'多喝水，少熬夜，明天会更好'", "effect": "nothing"},
    {"name": "一键暴富券", "icon": "💸", "weight": 15,
     "text": "获得「一键暴富券」…使用方式：把它放进钱包，每天看一眼，心情好也算财富！", "effect": "nothing"},

    # —— 梗向小剧场 ——
    {"name": "鸽子精附体", "icon": "🕊️", "weight": 40,
     "text": "你被「鸽子精」附体了：今天说'马上到'时，请自行-1小时", "effect": "nothing"},
    {"name": "真香警告", "icon": "🍜", "weight": 40,
     "text": "触发「真香定律」：今天若说'我不吃/我不玩'，结局大概率是真香", "effect": "nothing"},
    {"name": "网抑云时刻", "icon": "🌧️", "weight": 30,
     "text": "今晚 22:00 你将进入短暂「网抑云」：记得带伞，心里那种", "effect": "nothing"},
    {"name": "赛博饺子", "icon": "🥟", "weight": 30,
     "text": "获得「赛博饺子」一笼：吃了不顶饱，但能回 1% 的精神力", "effect": "nothing"},
    {"name": "打工魂觉醒", "icon": "🧱", "weight": 35,
     "text": "「打工魂」觉醒：今天的你搬砖效率 +50%，摸鱼被抓概率也 +50%", "effect": "nothing"},
    {"name": "社牛附体", "icon": "🎤", "weight": 25,
     "text": "今日「社牛」附体：路过任何群都可以大胆开麦，反正隔着一块屏幕", "effect": "nothing"},
    {"name": "早睡提醒器", "icon": "🛌", "weight": 35,
     "text": "获得「早睡提醒器」：今晚 23:00 它会在你心里响——响不响看缘分", "effect": "nothing"},

    # —— 小确幸向 ——
    {"name": "免费续杯", "icon": "☕", "weight": 45,
     "text": "获得「今日份快乐续杯」：再忙也记得给自己倒杯水，慢慢喝", "effect": "nothing"},
    {"name": "好运反弹", "icon": "🪃", "weight": 30,
     "text": "抽到「好运反弹」：今天别人向你丢的烦恼，都会变成双倍好运弹回去", "effect": "nothing"},
    {"name": "天气之子", "icon": "🌤️", "weight": 25,
     "text": "今日起你与天气系统绑定：心情晴，外面的天就晴（玄学，勿当真）", "effect": "nothing"},
    {"name": "锦鲤路过", "icon": "🎏", "weight": 20,
     "text": "一条锦鲤刚好路过你头顶：接下来 24 小时许愿灵验度 +1", "effect": "nothing"},

    # —— 道具（真实效果）——
    {"name": "连签护盾", "icon": "🛡️", "weight": 30,
     "text": "获得「连签护盾」×1：明天忘签也不断连签！（在 /积分 可见）", "effect": "shield", "amount": 1},
]


def _load_lottery_config():
    """读取抽奖配置；不存在则写入默认模板"""
    global _lottery
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(LOTTERY_CONFIG_FILE):
            with open(LOTTERY_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump({"note": "抽奖奖项配置：修改后点 Web 后台「插件」页的🔄刷新（或重启）生效。\n"
                                    "weight 为权重（建议 1~1000，正整数）：某奖概率 = 该奖 weight ÷ 全部 weight 之和，越大越容易中。\n"
                                    "effect 说明：nothing=整活文案（无持久效果）；shield=连签护盾道具（真实生效）。",
                           "items": _DEFAULT_LOTTERY}, f, ensure_ascii=False, indent=2)
            _lottery = [dict(x) for x in _DEFAULT_LOTTERY]
            return
        with open(LOTTERY_CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f) or {}
        items = cfg.get('items') or []
        _lottery = [dict(x) for x in items if isinstance(x, dict) and x.get('name')]
    except Exception:
        _lottery = [dict(x) for x in _DEFAULT_LOTTERY]


def _lottery_draw():
    """按权重抽奖，返回奖项 dict"""
    weights = [max(1, int(x.get('weight', 1))) for x in _lottery]
    r = random.randint(1, sum(weights))
    acc = 0
    for item, w in zip(_lottery, weights):
        acc += w
        if r <= acc:
            return item
    return _lottery[-1]


# ==================== 指令处理 ====================

def on_message(msg, bot=None):
    content = (msg.get("content") or "").strip()
    uid = msg.get("unified_openid") or msg.get("user_openid") or ""
    if not uid:
        return None
    nick = msg.get("user_name") or "群友"

    with _lock:
        _load()
        today = _today()
        rec = _data.get(uid)

        # ---- /积分 ----
        if content == "/积分":
            if not rec:
                return f"💰 {nick} 还没有积分，先去发 /签到 领 1 分吧～"
            lines = [f"💰 {nick} 的积分",
                     f"积分余额：{rec.get('points', 0)} 分",
                     f"累计签到：{rec.get('total', 0)} 次 · 当前连签 {rec.get('streak', 0)} 天"]
            if rec.get('shield', 0):
                lines.append(f"🛡️ 连签护盾：{rec.get('shield', 0)} 个（断签不重置连签）")
            lines.append("可用 /抽奖 消耗 10 分碰碰运气～")
            return "\n".join(lines)

        # ---- /抽奖 ----
        if content == "/抽奖":
            if not _lottery:
                _load_lottery_config()   # 兜底：确保配置已加载
            if not _lottery:
                return "⚠️ 抽奖配置为空，请检查 lottery.json"
            if not rec or rec.get('points', 0) < LOTTERY_COST:
                return f"💸 抽奖需要 {LOTTERY_COST} 分，{nick} 当前余额不足。先发 /签到 攒分吧～"
            prize = _lottery_draw()
            eff = prize.get('effect', 'nothing')
            new_points = rec.get('points', 0) - LOTTERY_COST
            got = ""
            if eff == 'shield':
                n = int(prize.get('amount', 1) or 1)
                rec['shield'] = rec.get('shield', 0) + n
                got = f"\n✨ 连签护盾 +{n}（当前 {rec['shield']} 个）"
            rec['points'] = new_points
            _save()
            text = str(prize.get('text') or f"抽中：{prize.get('name')}")
            lines = [f"🎰 {nick} 消耗 {LOTTERY_COST} 分抽奖",
                     f"{prize.get('icon','🎁')} {text}"]
            if got:
                lines.append(got.strip())
            lines.append(f"💳 剩余积分：{new_points} 分")
            return "\n".join(lines)

        # ---- 非 /签到 主指令，交给别的插件 ----
        if content != "/签到":
            return None

        # ---- /签到 主逻辑 ----
        if rec and rec.get('last') == today:
            order = rec.get('today_order')
            order_txt = f"\n🎯 你是今天第 {order} 位签到的" if order else ""
            return (f"🟡 今天已经签过啦！\n"
                    f"📅 当前已连续签到 {rec.get('streak', 0)} 天\n"
                    f"💰 累计 {rec.get('points', 0)} 分 · 共签到 {rec.get('total', 0)} 次"
                    f"{order_txt}\n"
                    f"明天再来，连签奖励等着你～")

        yesterday = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=1)).strftime('%Y-%m-%d')
        shield = rec.get('shield', 0) if rec else 0
        if rec and rec.get('last') == yesterday:
            streak = rec.get('streak', 0) + 1
        elif rec and shield > 0:
            streak = rec.get('streak', 0) + 1
            rec['shield'] = shield - 1
        else:
            streak = 1
        total = (rec.get('total', 0) if rec else 0) + 1
        bonus = _streak_bonus(streak)
        points = (rec.get('points', 0) if rec else 0) + 1 + bonus
        today_order = _next_today_order(today)

        _data[uid] = {
            'nick': nick, 'last': today, 'streak': streak,
            'total': total, 'points': points,
            'shield': rec.get('shield', 0) if rec else 0,
            'today_order': today_order,
        }
        _save()

        lines = [f"✅ 签到成功！{nick}"]
        lines.append(f"🎯 你是今天第 {today_order} 位签到的")
        if streak == 1 and not (rec and rec.get('last') == yesterday) and not shield:
            lines.append(f"🔥 连续签到 1 天（今天重新起算）")
        else:
            lines.append(f"🔥 连续签到 {streak} 天！")
        if shield and rec and rec.get('last') != yesterday:
            lines.append(f"🛡️ 使用了 1 个护盾，连签未中断")
        if bonus:
            lines.append(f"🎁 连签加成 +{bonus}")
        lines.append(f"💰 本次 +{1 + bonus} 分，当前共 {points} 分")
        lines.append(f"（累计签到 {total} 次）")
        if streak in (3, 7, 14, 30):
            lines.append(f"🎉 达成 {streak} 天连签成就！")
        return "\n".join(lines)


def on_start(bot=None):
    _load()
    _load_lottery_config()
