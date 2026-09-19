"""
Web 管理后台 - 轻量管理面板（纯标准库实现，无外部依赖）
功能：运行状态查看、配置文件在线编辑（保存后热应用）、日志查看、上下文查看、使用说明
面向非技术用户：表单带中文标签、说明与引导
"""

import json
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from core.deferred_writes import startup_buffer_state

MAX_BODY = 1024 * 1024  # POST 最大 1MB
# 读日志时最多从文件尾部回看多少字节（日志单文件上限 10MB，整份读进内存没有必要）
MAX_LOG_SCAN = 4 * 1024 * 1024


def _mask_secret(value: str) -> str:
    if not value or len(value) <= 8:
        return '******'
    return value[:4] + '****' + value[-4:]


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并：保留 base 中缺失的键，防止前端误删配置项"""
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# 敏感字段路径：面板中留空显示，留空保存=保持不变，填写新值=修改
SECRET_PATHS = (('api_key',), ('qq', 'app_secret'), ('web_admin', 'token'),
                ('cloud_sync', 'api_token'))


def _blank_secrets(cfg: dict) -> dict:
    """把敏感字段置空（不回传给浏览器）"""
    cfg = json.loads(json.dumps(cfg, ensure_ascii=False))
    for path in SECRET_PATHS:
        node = cfg
        for k in path[:-1]:
            if not isinstance(node, dict) or k not in node:
                break
            node = node[k]
        else:
            if isinstance(node, dict) and path[-1] in node:
                node[path[-1]] = ''
    return cfg


def _get_by_path(d: dict, path: tuple):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _merge_with_secrets(base: dict, override: dict) -> dict:
    """深度合并，且敏感字段：
    - 传空值(''/None) = 保留原值（用户留空不改）
    - 传 '__CLEAR__' = 显式清空（用户勾选"清空此值"）"""
    merged = _deep_merge(base, override)
    for path in SECRET_PATHS:
        ov = override
        for k in path:
            if not isinstance(ov, dict) or k not in ov:
                ov = None
                break
            ov = ov[k]
        if ov == '__CLEAR__':
            node = merged
            for k in path[:-1]:
                node = node[k]
            node[path[-1]] = ''
        elif ov in ('', None):
            base_val = _get_by_path(base, path)
            if base_val is not None:
                node = merged
                for k in path[:-1]:
                    node = node[k]
                node[path[-1]] = base_val
    return merged


def _fmt_uptime(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d > 0:
        return f"{d}天{h}小时{m}分"
    if h > 0:
        return f"{h}小时{m}分{s}秒"
    return f"{m}分{s}秒"


def _fmt_size(n) -> str:
    """文件大小显示（B/KB/MB）"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return '-'
    if n < 1024:
        return '%d B' % n
    if n < 1024 * 1024:
        return '%.1f KB' % (n / 1024)
    return '%.2f MB' % (n / 1024 / 1024)


# 配置编辑器的展示顺序与字段说明（中文，小白友好）
# type: text/secret/bool/number/textarea/list/kv/tasklist
RENDER_FIELDS = {
    'qq.app_id': {'label': 'AppID', 'tip': 'QQ开放平台的机器人 AppID（改后需重启）', 'type': 'text'},
    'qq.app_secret': {'label': 'AppSecret', 'tip': '留空=保持不变；填写新值可修改（改后需重启）', 'type': 'secret'},
    'qq.sandbox': {'label': '沙箱模式', 'tip': 'true=测试环境，false=正式环境（改后需重启）', 'type': 'bool'},
    'qq.reconnect_attempts': {'label': '最大重连次数', 'tip': '断线后最多尝试重连几次', 'type': 'number'},
    'qq.reconnect_interval': {'label': '重连间隔(秒)', 'tip': '每次重连之间等待的秒数', 'type': 'number'},
    'api_key': {'label': 'API 密钥', 'tip': '留空=保持不变；填写新值可修改', 'type': 'secret'},
    'base_url': {'label': 'API 地址', 'tip': '如 https://api.deepseek.com', 'type': 'text'},
    'model': {'label': '模型名称', 'tip': '如 deepseek-v4-flash', 'type': 'text'},
    'no_ai_reply': {'label': '未配置 AI 时的兜底回复',
                    'tip': '上面三项留空时，普通消息回这句；留空=不回复（适合插件接管回复）',
                    'type': 'text'},
    'ai.enabled': {'label': '启用内置 AI',
                   'tip': '关掉=即使填了 API Key 也不用内置 AI，普通消息全部交给插件/关键词回复',
                   'type': 'bool'},
    'system_prompt': {'label': '人设内容', 'tip': 'AI 的角色设定与说话风格', 'type': 'textarea'},
    'filter.exact': {'label': '精确匹配关键词回复',
                     'tip': '消息与关键词完全一致时，直接回复内容（不消耗 AI）。点 ➕ 添加',
                     'type': 'kv', 'key_path': 'filter.exact_match_keywords',
                     'value_path': 'filter.exact_match_responses',
                     'key_label': '关键词', 'value_label': '回复内容'},
    'filter.fuzzy': {'label': '模糊匹配关键词回复',
                     'tip': '消息包含关键词时，直接回复内容（不消耗 AI）。点 ➕ 添加',
                     'type': 'kv', 'key_path': 'filter.fuzzy_match_keywords',
                     'value_path': 'filter.fuzzy_match_responses',
                     'key_label': '关键词', 'value_label': '回复内容'},
    'group.require_mention': {'label': '仅@时回复', 'tip': 'true=只有被@才回复，false=回复全部', 'type': 'bool'},
    'rate_limit.enabled': {'label': '启用回复限速', 'tip': '同一用户短时间内频繁提问时提示稍候，防止刷屏', 'type': 'bool'},
    'rate_limit.interval_seconds': {'label': '限速间隔(秒)', 'tip': '同一用户两次AI回复的最小间隔', 'type': 'number'},
    'sensitive_words.enabled': {'label': '启用敏感词过滤', 'tip': '命中敏感词的消息/回复会被打码或拦截', 'type': 'bool'},
    'sensitive_words.list': {'label': '敏感词列表', 'tip': '每行一个词，点 ➕ 添加；消息或回复中包含即命中', 'type': 'list', 'item_label': '敏感词'},
    'sensitive_words.replacement': {'label': '打码符号', 'tip': '命中后替换成的符号，默认 ***', 'type': 'text'},
    'sensitive_words.block_input': {'label': '拦截含敏感词的消息', 'tip': 'true=用户消息含敏感词直接不回复；false=仅打码后正常回复', 'type': 'bool'},
    'message.max_segment_length': {'label': '单条最大长度', 'tip': '回复超过此长度会自动分段', 'type': 'number'},
    'message.max_queue_size': {'label': '最大排队数', 'tip': '同时排队超过此数会提示稍后再试', 'type': 'number'},
    'message.filter_meaningless': {'label': '过滤无意义消息', 'tip': '过滤纯数字/符号/表情等', 'type': 'bool'},
    'context.enabled': {'label': '启用上下文', 'tip': '是否记住对话历史', 'type': 'bool'},
    'context.max_history': {'label': '历史条数', 'tip': '每个用户/群最多记住多少条', 'type': 'number'},
    'log.max_size_mb': {'label': '日志大小(MB)', 'tip': '单个日志文件超过此大小自动分割（改后需重启）', 'type': 'number'},
    'log.console_color': {'label': '控制台彩色输出',
                          'tip': '时间灰、WARNING 黄、ERROR 红（正文不着色）；日志文件始终无颜色（改后需重启）',
                          'type': 'bool'},
    'hot_reload.enabled': {'label': '启用热更新', 'tip': '保存 config.json 后自动生效', 'type': 'bool'},
    'hot_reload.interval_seconds': {'label': '检查间隔(秒)', 'tip': '多久检查一次配置变化', 'type': 'number'},
    'cloud_sync.enabled': {'label': '启用云同步',
                           'tip': '把用户上下文/统计/插件数据同步到 Cloudflare D1，换服务器不丢数据（改后需重启）',
                           'type': 'bool'},
    'cloud_sync.account_id': {'label': 'Cloudflare 账户 ID', 'tip': 'Cloudflare 控制台右侧或网址里可见', 'type': 'text'},
    'cloud_sync.database_id': {'label': 'D1 数据库 ID', 'tip': '创建 D1 数据库后可见', 'type': 'text'},
    'cloud_sync.api_token': {'label': 'Cloudflare API 令牌',
                             'tip': '权限需包含「D1 → 编辑」；留空=保持不变', 'type': 'secret'},
    'cloud_sync.interval_seconds': {'label': '写入间隔(秒)',
                                    'tip': '后台多久写入一次云数据库（默认60；填 300 = 每 5 分钟一次，'
                                           '间隔≥60秒会对齐整点倍数；无改动的文件不会重复写）',
                                    'type': 'number'},
    'cloud_sync.pull_on_start': {'label': '启动时拉取云端数据',
                                 'tip': '换服务器后靠它自动恢复用户数据（建议开启）', 'type': 'bool'},
    'cloud_sync.upload_logs': {'label': '日志也上云',
                               'tip': '日志量大，默认关闭；开启后 data/logs 也会同步', 'type': 'bool'},
    'cloud_sync.max_file_mb': {'label': '单文件大小上限(MB)', 'tip': '超过此大小的文件不同步（默认2）', 'type': 'number'},
    'cloud_sync.tombstone_days': {'label': '删除标记保留(天)',
                                  'tip': '删除会写成带时间戳的“删除标记”，其它机器据此删除本地副本且不会复活；'
                                         '标记保留天数（默认30，0=永久保留）',
                                  'type': 'number'},
    'cloud_sync.apply_remote_deletes': {'label': '应用云端删除（删本地）',
                                        'tip': '开启：云端删除标记比本地文件新时，同步删除本地文件；'
                                               '关闭：只记录不删本地（本地数据绝不因云端删除而消失）',
                                        'type': 'bool'},
    'cloud_sync.error_pause_minutes': {'label': '连续失败暂停(分钟)',
                                       'tip': '同一文件连续失败 3 次 → 记 ERROR 并暂停云同步；'
                                              '这里填自动恢复的分钟数（0=只能手动恢复，默认30）',
                                       'type': 'number'},
    'cloud_sync.startup_buffer': {'label': '启动写缓存',
                                  'tip': '开启：程序刚启动到第一次云同步完成前，data/ 下的数据改动先存在内存里，'
                                         '等云端数据拉下来之后再写盘，避免启动时写的旧数据把云端数据覆盖掉'
                                         '（日志与 config.json 不受影响；未启用云同步时本项无意义）',
                                  'type': 'bool'},
    'cloud_sync.startup_buffer_minutes': {'label': '启动缓存等待上限(分钟)',
                                          'tip': '等待首次同步的上限（默认3）：超时就把缓存内容先落盘，'
                                                 '避免数据一直不写；0=一直等到同步完成（云端不通时会一直等）',
                                          'type': 'number'},
    'cloud_sync.startup_buffer_max_mb': {'label': '启动缓存容量上限(MB)',
                                         'tip': '缓存内容超过此大小就立刻写回磁盘并停止缓存（默认8，0=不限制），'
                                                '避免机器人很忙时缓存无限占内存；为安全起见即使填 0 也有 128MB 兜底',
                                         'type': 'number'},
    'cloud_sync.startup_buffer_keep_remote': {'label': '冲突时以云端为准',
                                              'tip': '开启（默认）：某个文件刚被第一次同步从云端恢复，'
                                                     '就放弃启动期间对它的本地修改，并在控制台 WARNING 里列出；'
                                                     '关闭：本地修改优先（照旧覆盖云端）',
                                              'type': 'bool'},
    'admin.openids': {'label': '管理员 openid 列表', 'tip': '每行一个 openid，点 ➕ 添加', 'type': 'list', 'item_label': 'openid'},
    'alert.enabled': {'label': '启用告警', 'tip': '出错时私聊通知主人', 'type': 'bool'},
    'alert.owner_openid': {'label': '主人 openid', 'tip': '接收错误告警的用户', 'type': 'text'},
    'schedule.enabled': {'label': '启用定时任务', 'tip': '是否执行定时推送', 'type': 'bool'},
    'schedule.tasks': {'label': '定时任务', 'tip': '每行一个任务：时间 + 发送到 + 目标ID + 内容，点 ➕ 添加',
                       'type': 'tasklist'},
    'web_admin.enabled': {'label': '启用本面板', 'tip': '关闭后本页面不可访问', 'type': 'bool'},
    'web_admin.host': {'label': '监听地址', 'tip': '本机用 127.0.0.1；手机访问用 0.0.0.0（改后需重启）', 'type': 'text'},
    'web_admin.port': {'label': '端口', 'tip': '浏览器访问 http://地址:端口/（改后需重启）', 'type': 'number'},
    'web_admin.token': {'label': '访问令牌', 'tip': '留空=保持不变；填写新值可修改', 'type': 'secret'},
    'command_panel.enabled': {'label': '启用指令面板', 'tip': '向 QQ 注册指令，用户在聊天界面可见/可点', 'type': 'bool'},
    'command_panel.commands': {'label': '指令面板指令', 'tip': '每行：类型(指令/链接) + 名称 + 描述或链接地址；指令即斜杠命令，链接为可点击的外部地址，点 ➕ 添加',
                               'type': 'cmdlist', 'name_label': '指令名(如 /帮助)', 'desc_label': '描述(指令)或链接地址(链接)'},
    'command_panel.c2c.target_type': {'label': '私聊面板范围', 'tip': 'all=所有用户私聊可见；specific=仅指定用户（填下方用户 openid）',
                                      'type': 'text', 'path': 'command_panel.c2c.target_type'},
    'command_panel.c2c.user_openids': {'label': '私聊面板-指定用户', 'tip': 'target_type 为 specific 时，每行一个用户 openid',
                                       'type': 'list', 'item_label': '用户 openid', 'path': 'command_panel.c2c.user_openids'},
    'command_panel.group.target_type': {'label': '群聊面板范围', 'tip': 'all=所有群可见；specific=仅指定群（填下方群 openid）',
                                        'type': 'text', 'path': 'command_panel.group.target_type'},
    'command_panel.group.group_openids': {'label': '群聊面板-指定群', 'tip': 'target_type 为 specific 时，每行一个群 openid',
                                          'type': 'list', 'item_label': '群 openid', 'path': 'command_panel.group.group_openids'},
    'plugins.enabled': {'label': '启用插件系统', 'tip': '是否加载 plugins/ 目录下的插件', 'type': 'bool'},
    'plugins.dir': {'label': '插件目录', 'tip': '默认 plugins；一般不用改', 'type': 'text'},
}

RENDER_SECTIONS = [
    {'title': '🤖 QQ 机器人配置', 'tip': '改 AppID/密钥/沙箱后需要重启才能生效',
     'fields': ['qq.app_id', 'qq.app_secret', 'qq.sandbox', 'qq.reconnect_attempts', 'qq.reconnect_interval']},
    {'title': '🧠 AI 服务配置', 'tip': '改完点保存即可生效，无需重启；API 三项留空=不启用内置 AI，'
                                     '「启用内置 AI」关掉=即使填了 Key 也不用内置 AI（插件接管回复时用）',
     'fields': ['api_key', 'base_url', 'model', 'ai.enabled', 'no_ai_reply']},
    {'title': '📝 AI 全局人设', 'tip': '给 AI 的角色设定，保存后立即生效',
     'fields': ['system_prompt']},
    {'title': '🔑 关键词回复', 'tip': '命中关键词直接回复，不消耗 AI；左边填关键词、右边填回复，点 ➕ 添加',
     'fields': ['filter.exact', 'filter.fuzzy']},
    {'title': '👥 群聊配置', 'tip': '', 'fields': ['group.require_mention']},
    {'title': '⏱️ 回复限速', 'tip': '防止同一用户刷屏', 'fields': ['rate_limit.enabled', 'rate_limit.interval_seconds']},
    {'title': '🛡️ 敏感词过滤', 'tip': '命中敏感词的消息/回复自动打码或拦截', 'fields': ['sensitive_words.enabled', 'sensitive_words.list', 'sensitive_words.replacement', 'sensitive_words.block_input']},
    {'title': '📨 消息处理', 'tip': '', 'fields': ['message.max_segment_length', 'message.max_queue_size', 'message.filter_meaningless']},
    {'title': '💬 上下文', 'tip': '', 'fields': ['context.enabled', 'context.max_history']},
    {'title': '📄 日志', 'tip': '改后需重启', 'fields': ['log.max_size_mb', 'log.console_color']},
    {'title': '🔄 配置热更新', 'tip': '', 'fields': ['hot_reload.enabled', 'hot_reload.interval_seconds']},
    {'title': '☁️ 云同步（Cloudflare D1）',
     'tip': '把用户上下文 / 身份绑定 / 统计 / 插件数据同步到云数据库，换服务器自动恢复；'
            'config.json 等敏感文件永不上云（改后需重启）',
     'fields': ['cloud_sync.enabled', 'cloud_sync.account_id', 'cloud_sync.database_id',
                'cloud_sync.api_token', 'cloud_sync.interval_seconds',
                'cloud_sync.pull_on_start', 'cloud_sync.upload_logs', 'cloud_sync.max_file_mb',
                'cloud_sync.tombstone_days', 'cloud_sync.apply_remote_deletes',
                'cloud_sync.error_pause_minutes',
                'cloud_sync.startup_buffer', 'cloud_sync.startup_buffer_minutes',
                'cloud_sync.startup_buffer_max_mb',
                'cloud_sync.startup_buffer_keep_remote']},
    {'title': '👑 管理员与告警', 'tip': '', 'fields': ['admin.openids', 'alert.enabled', 'alert.owner_openid']},
    {'title': '⏰ 定时任务', 'tip': '每行一个任务：时间(如 08:00)、发送到、目标ID、内容；点 ➕ 添加',
     'fields': ['schedule.enabled', 'schedule.tasks']},
    {'title': '🌐 Web 管理后台', 'tip': '改端口/监听地址后需重启',
     'fields': ['web_admin.enabled', 'web_admin.host', 'web_admin.port', 'web_admin.token']},
    {'title': '🎮 指令面板', 'tip': '用户在 QQ 聊天界面能看到/点选的指令列表；私聊/群聊面板分别配置可见范围，改完保存后自动重新注册',
     'fields': ['command_panel.enabled', 'command_panel.commands',
                'command_panel.c2c.target_type', 'command_panel.c2c.user_openids',
                'command_panel.group.target_type', 'command_panel.group.group_openids']},
    {'title': '🧩 插件系统', 'tip': '插件放在 plugins/ 目录，无需改代码；改完在「插件」页点重新加载',
     'fields': ['plugins.enabled', 'plugins.dir']},
]


def _infer_type(value):
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, (dict, list)):
        return 'json'
    if isinstance(value, str) and len(value) > 60:
        return 'textarea'
    return 'text'


PAGE_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QQ AI Bot 管理面板</title>
<style>
*{box-sizing:border-box;margin:0}
:root{
  --primary:#4f46e5;--primary-2:#7c3aed;--primary-dark:#3730a3;
  --bg:#f1f4fb;--card:#ffffff;--line:#e3e8f2;--text:#1f2733;--muted:#7a8499;
  --ok:#16a34a;--err:#dc2626;--warn:#d97706;--radius:14px;
  --shadow:0 1px 2px rgba(23,32,64,.05),0 8px 24px -12px rgba(23,32,64,.14);
}
body{font-family:"Segoe UI",system-ui,"Microsoft YaHei",sans-serif;background:
  radial-gradient(1200px 500px at 85% -10%,#e7ecff 0%,transparent 60%),
  radial-gradient(900px 420px at -10% 0%,#f3eaff 0%,transparent 55%),
  var(--bg);color:var(--text);min-height:100vh}
header{background:linear-gradient(120deg,#232d5e 0%,#3b4b9e 55%,#5b4baf 100%);color:#fff;
  padding:16px 22px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;
  box-shadow:0 4px 18px -6px rgba(35,45,94,.55);position:relative;z-index:5}
header h1{font-size:18px;letter-spacing:.3px;display:flex;align-items:center;gap:9px}
header h1 .logo{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#818cf8,#c084fc);
  display:inline-flex;align-items:center;justify-content:center;font-size:16px;box-shadow:0 2px 8px rgba(0,0,0,.25)}
#conn{font-size:12.5px;padding:5px 13px;border-radius:999px;background:rgba(255,255,255,.14);
  backdrop-filter:blur(4px);border:1px solid rgba(255,255,255,.22);font-weight:600}
#conn.on{background:rgba(34,197,94,.85);border-color:transparent}
#conn.off{background:rgba(239,68,68,.8);border-color:transparent}
nav{position:sticky;top:0;z-index:4;background:rgba(255,255,255,.85);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line);padding:10px 18px;display:flex;gap:8px;flex-wrap:wrap;box-shadow:0 2px 10px -8px rgba(23,32,64,.25)}
nav button{padding:8px 18px;border:1px solid var(--line);background:#fff;border-radius:999px;cursor:pointer;
  font-size:13.5px;color:var(--text);transition:all .18s;font-weight:500}
nav button:hover{border-color:#c7cdf5;color:var(--primary);transform:translateY(-1px)}
nav button.active{background:linear-gradient(120deg,var(--primary),var(--primary-2));color:#fff;
  border-color:transparent;box-shadow:0 4px 12px -4px rgba(79,70,229,.55)}
main{padding:20px;max-width:1100px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px;
  margin-bottom:16px;box-shadow:var(--shadow);transition:box-shadow .2s}
.card:hover{box-shadow:0 2px 4px rgba(23,32,64,.06),0 14px 34px -14px rgba(23,32,64,.22)}
.card h3{margin:0 0 12px;font-size:15px;display:flex;align-items:center;gap:8px;color:#2b3350}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.kv{background:linear-gradient(180deg,#f8faff,#f2f5fd);border:1px solid var(--line);border-radius:11px;
  padding:11px 14px;position:relative;overflow:hidden}
.kv::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;
  background:linear-gradient(180deg,var(--primary),var(--primary-2))}
.kv .k{font-size:12px;color:var(--muted);font-weight:500}
.kv .v{font-size:14.5px;margin-top:4px;word-break:break-all;font-weight:600;color:#28304d}
label{display:block;margin:10px 0}
label .lbl{font-weight:600;font-size:13px;color:#333c56}
label .tip{display:block;color:var(--muted);font-size:12px;margin:3px 0 5px}
input[type=text],input[type=number],input[type=password],textarea,select{width:100%;padding:8px 11px;
  border:1px solid #d4daea;border-radius:9px;font-size:13.5px;font-family:inherit;background:#fbfcff;
  transition:border-color .15s,box-shadow .15s;color:var(--text)}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--primary);
  box-shadow:0 0 0 3px rgba(79,70,229,.14);background:#fff}
textarea{min-height:80px;resize:vertical;line-height:1.6}
input[type=checkbox]{width:16px;height:16px;accent-color:var(--primary);margin:0 6px 0 0;vertical-align:-3px;cursor:pointer}
button{font-family:inherit}
button.primary{background:linear-gradient(120deg,var(--primary),var(--primary-2));color:#fff;border:none;
  padding:11px 28px;border-radius:11px;font-size:15px;cursor:pointer;box-shadow:0 6px 16px -6px rgba(79,70,229,.6);
  transition:all .18s;font-weight:600}
button.primary:hover{transform:translateY(-1px);box-shadow:0 10px 22px -8px rgba(79,70,229,.7)}
button.primary:active{transform:translateY(0)}
button.ghost{background:#fff;border:1px solid #d4daea;padding:7px 16px;border-radius:9px;cursor:pointer;
  font-size:13.5px;color:#3d4663;transition:all .15s;font-weight:500}
button.ghost:hover{border-color:var(--primary);color:var(--primary);background:#f7f8ff}
#msg{padding:11px 16px;border-radius:10px;margin:12px 0;display:none;font-size:14px;animation:fadeIn .25s}
#msg.ok{display:block;background:#e9f9ef;color:#15803d;border:1px solid #b9e8c8}
#msg.err{display:block;background:#fef0f0;color:#b91c1c;border:1px solid #f6c6c6}
@keyframes fadeIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.warn{background:linear-gradient(90deg,#fffbeb,#fffdf5);border:1px solid #f3d9a4;color:#8a5a08;
  padding:11px 15px;border-radius:11px;margin-bottom:16px;font-size:13px;line-height:1.7}
.hint{background:linear-gradient(90deg,#f4f7ff,#fafcff);border:1px solid #dbe3f7;color:#4a5478;
  padding:10px 14px;border-radius:11px;margin:0 0 12px;font-size:12.5px;line-height:1.75}
.hint code{background:#eef2fd;padding:1px 5px;border-radius:5px;font-family:Consolas,monospace;font-size:12px}
.log-head{font-size:12.5px;color:var(--muted);margin:0 0 6px;line-height:1.6;word-break:break-all}
pre{background:linear-gradient(180deg,#111827,#0d1322);color:#d7e3f4;padding:14px 16px;border-radius:12px;
  overflow:auto;font-size:12.5px;line-height:1.7;white-space:pre-wrap;word-break:break-all;margin:0;
  border:1px solid #1e293b;font-family:Consolas,"Courier New",monospace}
table{border-collapse:separate;border-spacing:0;width:100%;background:#fff;font-size:13px;
  border:1px solid var(--line);border-radius:11px;overflow:hidden}
td,th{border-bottom:1px solid var(--line);padding:9px 12px;text-align:left;
  word-break:break-word;overflow-wrap:anywhere;vertical-align:top}
tr:last-child td{border-bottom:none}
th{background:linear-gradient(180deg,#f4f6fc,#eaeef9);color:#3b4466;font-weight:600;font-size:12.5px}
/* 表格外层容器：内容太长时横向滚动，而不是撑破卡片或让文字互相盖住 */
.tbl-wrap{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:11px}
.tbl-wrap table{min-width:560px}
.tbl-wrap.narrow table{min-width:0}
td .badge{margin-left:0}
td.cell-status,td.cell-act{white-space:nowrap}
.nowrap{white-space:nowrap}
tbody tr{transition:background .12s}
tbody tr:hover{background:#f7f9ff}
.toolbar{margin-bottom:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.sticky{position:sticky;bottom:12px;background:rgba(255,255,255,.92);backdrop-filter:blur(8px);
  border:1px solid var(--line);border-radius:var(--radius);padding:12px 16px;display:flex;align-items:center;
  justify-content:space-between;gap:10px;box-shadow:0 8px 30px -10px rgba(23,32,64,.35)}
.tab-content{display:none}
.tab-content.active{display:block;animation:fadeIn .2s}
li{margin:5px 0;line-height:1.7}
.kv-editor{margin:6px 0}
.kv-row{display:flex;gap:8px;margin:6px 0;align-items:center;flex-wrap:wrap}
.kv-row input,.kv-row select{flex:1;min-width:110px}
.kv-row .t-time{flex:0 0 92px;min-width:92px}
.kv-row .t-type{flex:0 0 92px;min-width:92px}
.row-del{border:none;background:#fdecec;color:#c0392b;border-radius:8px;cursor:pointer;padding:8px 12px;flex:0 0 auto;font-weight:600;transition:all .15s}
.row-del:hover{background:#f9d5d5}
.row-add{border:1.5px dashed #b9c2de;background:#f8faff;border-radius:9px;padding:8px 16px;cursor:pointer;
  margin-top:6px;font-size:13px;color:#4a5478;transition:all .15s;font-weight:500}
.row-add:hover{border-color:var(--primary);color:var(--primary);background:#f4f5ff}
.flabel{margin:10px 0 4px}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#c6cde2;border-radius:6px}
::-webkit-scrollbar-thumb:hover{background:#a8b1cf}
select{padding:7px 10px;width:auto;min-width:110px;cursor:pointer}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:700;margin-left:6px;vertical-align:1px;white-space:nowrap}
.badge.ok{background:#e9f9ef;color:#15803d}
.badge.err{background:#fef0f0;color:#b91c1c}
.badge.warn{background:#fffbeb;color:#b45309}

/* ============ 手机端适配（<=820px） ============ */
@media (max-width:820px){
  header{padding:12px 14px;gap:8px}
  header h1{font-size:16px}
  header h1 .logo{width:26px;height:26px;border-radius:8px;font-size:14px}
  #conn{font-size:12px;padding:4px 11px}
  /* 导航横向滚动，避免换行挤压/遮挡 */
  nav{padding:8px 10px;gap:6px;flex-wrap:nowrap;overflow-x:auto}
  nav button{padding:7px 14px;font-size:12.5px;white-space:nowrap;flex:0 0 auto}
  main{padding:12px}
  .card{padding:14px;border-radius:12px}
  .card h3{font-size:14px}
  .grid{grid-template-columns:1fr;gap:9px}
  .kv{padding:10px 12px}
  .kv .v{font-size:13.5px}
  .toolbar{gap:8px}
  .toolbar > span{font-size:12px}
  /* 表格：字号缩小 + 外层横向滚动（不再挤压重叠） */
  table{font-size:12.5px}
  td,th{padding:8px 9px}
  .tbl-wrap table{min-width:520px}
  pre{font-size:11.5px;padding:11px 12px;line-height:1.6}
  /* 输入框字号 >=16px，避免 iOS 聚焦时页面被放大 */
  input[type=text],input[type=number],input[type=password],textarea,select{font-size:16px}
  button.primary{width:100%;padding:12px 18px;font-size:15px}
  .sticky{flex-direction:column;align-items:stretch;gap:8px}
  .sticky span{font-size:12.5px}
  .kv-row .t-time,.kv-row .t-type{flex:1 1 43%;min-width:0}
  .kv-row input,.kv-row select{min-width:0}
  select{min-width:0;max-width:100%}
}
@media (max-width:480px){
  .grid{grid-template-columns:1fr}
  table{font-size:12px}
  td,th{padding:7px 8px}
  .tbl-wrap table{min-width:460px}
}
</style>
</head>
<body>
<header>
  <h1><span class="logo">🤖</span>QQ AI Bot 管理面板</h1>
  <span id="conn" class="off">连接状态检测中…</span>
</header>
<nav>
  <button id="b_status" onclick="show('status')">📊 状态</button>
  <button id="b_stats" onclick="show('stats')">📈 统计</button>
  <button id="b_config" onclick="show('config')">⚙️ 配置</button>
  <button id="b_plugins" onclick="show('plugins')">🧩 插件</button>
  <button id="b_logs" onclick="show('logs')">📜 日志</button>
  <button id="b_context" onclick="show('context')">📁 上下文</button>
  <button id="b_help" onclick="show('help')">❓ 使用说明</button>
</nav>
<main>
<div id="msg"></div>

<div id="tab_status" class="tab-content">
  <div class="toolbar">
    <button class="ghost" onclick="loadStatus()">🔄 刷新</button>
    <label style="margin:0"><input type="checkbox" id="auto_status" onchange="toggleAuto()"> 每 5 秒自动刷新</label>
  </div>
  <div class="card"><h3>运行状态</h3><div class="grid" id="status_grid"></div></div>
  <div class="card"><h3>☁️ 云同步</h3>
    <div class="toolbar">
      <button class="ghost" onclick="syncCloudNow()">⬆️ 立即同步一次</button>
      <button class="ghost" onclick="testCloudNow()">🔌 测试连接</button>
      <button class="ghost" onclick="resumeCloudNow()">▶️ 解除暂停</button>
      <span style="font-size:12.5px;color:var(--muted)">按设定间隔自动写入云数据库；没有改动的文件不会重复写；
        同一文件连续失败 3 次会自动暂停并记 ERROR（可在此手动重试/解除）</span>
    </div>
    <div class="grid" id="cloud_grid"></div>
  </div>
  <div class="card"><h3>最近日志（20 条）</h3><pre id="status_logs"></pre></div>
</div>

<div id="tab_stats" class="tab-content">
  <div class="toolbar">
    <button class="ghost" onclick="loadStats()">🔄 刷新</button>
  </div>
  <div class="card">
    <h3>📅 今日统计 <span class="badge ok" id="stats_date"></span></h3>
    <div class="grid" id="stats_today"></div>
  </div>
  <div class="card">
    <h3>📈 最近 7 天趋势</h3>
    <div id="stats_chart"></div>
  </div>
  <div class="card">
    <h3>🗂️ 历史累计</h3>
    <div class="grid" id="stats_total"></div>
  </div>
</div>

<div id="tab_config" class="tab-content">
  <div class="warn">
    🔒 <b>安全说明</b>：API 密钥、AppSecret、访问令牌等敏感字段在本页面<b>留空显示</b>（不会显示真实值）。
    想修改就在对应行填写新值；<b>留空保存 = 保持不变</b>；勾选「<b>清空此值</b>」可真正删除该密钥/令牌。
    保存后页面会自动刷新，敏感行恢复空白。
  </div>
  <div id="config_form"></div>
  <div class="card" id="panel_mgmt">
    <h3>🎛️ 指令面板管理（删除旧面板释放额度）</h3>
    <div class="toolbar">
      <button class="ghost" onclick="loadPanels()">🔄 刷新面板列表</button>
      <button class="ghost" onclick="reloadPanels()">🔁 按当前配置重新注册面板</button>
    </div>
    <div id="panels_view"><p style="color:#888">点击「刷新面板列表」查看当前已创建的指令面板；若创建面板报"超出数量限制"，先删除旧面板，再点「重新注册面板」重试。</p></div>
  </div>
  <div class="sticky">
    <span style="font-size:13px;color:#667">改完点击保存，<b>10 秒内自动生效</b>；AppID/密钥/沙箱等连接配置需重启生效</span>
    <button class="primary" onclick="saveConfig()">💾 保存配置</button>
  </div>
</div>

<div id="tab_plugins" class="tab-content">
  <div class="card">
    <h3>🧩 插件管理</h3>
    <div class="toolbar">
      <button class="ghost" onclick="refreshPlugins()">🔄 刷新（扫描插件目录并重新加载）</button>
      <button class="primary" id="plugins_save_btn" onclick="applyPlugins()">💾 保存（应用启用/停用修改）</button>
      <span id="plugins_pending" style="display:none;padding:6px 12px;background:#fff7e6;border:1px solid #f5d28e;border-radius:9px;font-size:12.5px;color:#8a6d1a">
      </span>
    </div>
    <div id="plugins_view"><p style="color:#888">加载中...</p></div>
    <div style="margin-top:12px;padding:11px 14px;background:#f4f5ff;border:1px solid #d8dcf8;border-radius:10px;font-size:12.5px;color:#4a5478;line-height:1.8">
      📖 <b>怎么加插件</b>：把插件放进程序的 <code>plugins/</code> 目录——单个 <code>.py</code> 文件，
      或多个文件时用<b>一个文件夹</b>（入口 <code>__init__.py</code> 或 <code>main.py</code>，其余 .py 是辅助模块）。
      然后点「🔄 刷新」即可，<b>不用改任何源代码</b>。<br>
      启用/停用插件后点「💾 保存」生效；状态会保存，重启后保持。<br>
      插件格式说明见 <code>plugins/README.md</code>，或参考目录里的示例插件。
    </div>
  </div>
</div>

<div id="tab_logs" class="tab-content">
  <div class="card">
    <h3>📜 运行日志</h3>
    <div class="toolbar">
      <span style="font-size:13px;color:var(--muted)">日志级别：</span>
      <select id="log_level" onchange="loadLogs()">
        <option value="">全部</option>
        <option value="DEBUG">DEBUG（调试）</option>
        <option value="INFO">INFO（信息）</option>
        <option value="WARNING">WARNING（警告）</option>
        <option value="ERROR">ERROR（错误）</option>
        <option value="CRITICAL">CRITICAL（严重）</option>
      </select>
      <span style="font-size:13px;color:var(--muted)">行数：</span>
      <input type="number" id="log_lines" value="200" min="10" max="500" style="width:85px" onchange="loadLogs()">
      <button class="ghost" onclick="loadLogs()">🔄 刷新</button>
      <label style="margin:0;font-size:13px;color:#3d4663"><input type="checkbox" id="auto_logs" onchange="toggleAuto()"> 每 5 秒自动刷新</label>
    </div>
    <div class="toolbar">
      <span style="font-size:13px;color:var(--muted)">下载日志文件：</span>
      <select id="log_file" style="min-width:220px;max-width:100%"
              title="这里的选择只用于下载，不会改变下方显示的内容"></select>
      <button class="ghost" onclick="loadLogFiles()">🗂️ 刷新文件列表</button>
      <button class="ghost" onclick="downloadLog()">⬇️ 下载选中的日志</button>
    </div>
    <div class="hint">
      ℹ️ 这一行是<b>下载用</b>的：选择某个文件后点「下载选中的日志」，会把这个 <code>.txt</code> 下载到电脑上查看/转发。
      <b>它不会改变下方显示的内容</b> —— 下方「运行日志」始终显示<b>最新</b>的那个日志文件（可用上面的「级别 / 行数」筛选）。
    </div>
    <div class="log-head" id="logs_current">当前显示：最新日志文件</div>
    <pre id="logs_view"></pre>
  </div>
</div>

<div id="tab_context" class="tab-content">
  <div class="toolbar"><button class="ghost" onclick="loadContext()">🔄 刷新</button></div>
  <div id="context_view"></div>
</div>

<div id="tab_help" class="tab-content">
  <div class="card"><h3>❓ 这是什么</h3>
    <p>这是你的 QQ AI 机器人管理面板，用来<b>查看机器人状态</b>和<b>修改配置</b>。所有操作都是点击按钮，不需要会写代码。</p>
  </div>
  <div class="card"><h3>⚙️ 怎么修改配置</h3>
    <ol>
      <li>点上方「<b>配置</b>」标签</li>
      <li>在表单里找到要改的项（每项下面有灰色小字说明）</li>
      <li>改完点右下角蓝色「<b>💾 保存配置</b>」按钮</li>
      <li>看到绿色提示「保存成功」即可；大部分配置 <b>10 秒内自动生效</b></li>
    </ol>
    <p style="color:#888;font-size:13px">注意：AppID、AppSecret、沙箱模式、日志大小这类"连接类"配置，保存后需要重启机器人（关闭后重新启动，或用 start.bat / start.sh 自动重启）才生效。</p>
  </div>
  <div class="card"><h3>💬 机器人常用指令（在 QQ 里发送）</h3>
    <table>
      <tr><th>指令</th><th>作用</th></tr>
      <tr><td>/帮助</td><td>显示指令列表</td></tr>
      <tr><td>/clear</td><td>清空当前对话记忆</td></tr>
      <tr><td>/转移私聊到群聊</td><td>把私聊记忆合并到当前群</td></tr>
      <tr><td>/生成转移码</td><td>生成身份绑定码（私聊里发）</td></tr>
      <tr><td>/绑定转移码 123456</td><td>绑定身份并转移记忆（群里发）</td></tr>
    </table>
  </div>
  <div class="card"><h3>🖼️ 图片</h3>
    <ol>
      <li><b>图片</b>：直接发图片给机器人，AI 会看图并回复（需要 AI 模型支持图片识别）</li>
    </ol>
  </div>
  <div class="card"><h3>🔑 关键词回复怎么填（示例）</h3>
    <p>在「配置」→「关键词回复」里填写，<b>不用懂 JSON</b>：</p>
    <ol>
      <li><b>精确匹配</b>：左边框填关键词、右边框填回复（消息与关键词<b>完全一致</b>才回复）</li>
      <li><b>模糊匹配</b>：同上（消息<b>包含</b>关键词就回复）</li>
      <li>填完一组点「➕ 添加一行」继续填下一组</li>
      <li>最后点右下角「💾 保存配置」，程序会自动转成 JSON</li>
    </ol>
    <p>示例（照此格式填）：</p>
    <table>
      <tr><th>精确匹配</th><th>回复</th></tr>
      <tr><td>你好</td><td>你好呀！</td></tr>
      <tr><td>在吗</td><td>在的~</td></tr>
      <tr><th>模糊匹配</th><th>回复</th></tr>
      <tr><td>天气</td><td>今天天气不错哦</td></tr>
      <tr><td>价格</td><td>想了解哪个商品的价格呢？</td></tr>
    </table>
    <p style="color:#888;font-size:13px">作用：命中关键词时<b>直接回复预设内容，不消耗 AI</b>。</p>
  </div>
  <div class="card"><h3>⏰ 定时任务怎么填</h3>
    <p>在「配置」→「定时任务」里，每行填四个格子，点「➕ 添加一行」加更多：</p>
    <ul>
      <li><b>时间</b>：北京时间 HH:MM（如 08:00），到点自动发送</li>
      <li><b>发送到</b>：群聊 或 私聊</li>
      <li><b>目标ID</b>：群 openid 或用户 openid（可看上下文页面的文件名）</li>
      <li><b>发送内容</b>：到点发送的文字</li>
    </ul>
  </div>
  <div class="card"><h3>🌐 手机访问本面板</h3>
    <p>把「配置」→「Web 管理后台」里的 host 改为 <b>0.0.0.0</b> 并设置一个访问令牌，保存后重启机器人；手机浏览器访问 <b>http://电脑IP:端口/?token=令牌</b> 即可。</p>
  </div>
  <div class="card"><h3>❓ 常见问题</h3>
    <ul>
      <li><b>机器人没反应？</b> 看「状态」页连接是否正常、日志有没有报错</li>
      <li><b>改了配置没生效？</b> 先确认点了保存；连接类配置需要重启</li>
      <li><b>忘了访问令牌？</b> 在「配置」→「Web 管理后台」的访问令牌行勾选「清空此值」并保存，即可去掉令牌；或直接改 config.json 的 web_admin.token</li>
      <li><b>不想用令牌直接进面板？</b> 把访问令牌清空（勾选「清空此值」保存）即可；注意此时任何能访问该端口的人都能打开面板，建议保持 host 为 127.0.0.1（仅本机）</li>
    </ul>
  </div>
</div>
</main>
<script>
var TOKEN = new URLSearchParams(location.search).get('token') || '';
function url(p){ return p + (TOKEN ? (p.indexOf('?')>=0?'&':'?')+'token='+TOKEN : ''); }
function msg(text, ok){ var m=document.getElementById('msg'); m.className=ok?'ok':'err'; m.textContent=text; setTimeout(function(){m.style.display='none';}, 6000); }
async function getJSON(p){
  var r = await fetch(url(p));
  if(!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}
function show(name){
  document.querySelectorAll('nav button').forEach(function(b){ b.className = b.id==='b_'+name ? 'active':''; });
  document.querySelectorAll('.tab-content').forEach(function(t){ t.className = 'tab-content'+(t.id==='tab_'+name?' active':''); });
  if(name==='status'){ loadStatus(); }
  else if(name==='stats'){ loadStats(); }
  else if(name==='config'){ loadConfig(); }
  else if(name==='plugins'){ loadPlugins(); }
  else if(name==='logs'){ loadLogs(); loadLogFiles(); }
  else if(name==='context'){ loadContext(); }
}
/* ---------- 状态 ---------- */
async function loadStatus(){
  var c = document.getElementById('conn');
  try{
    var s = await getJSON('/api/status');
    c.textContent = s.ws_connected ? '● 已连接' : '○ 未连接';
    c.className = s.ws_connected ? 'on' : 'off';
    var grid = document.getElementById('status_grid');
    var items = [
      ['运行时长', s.uptime_text],
      ['WebSocket 连接', s.ws_connected ? '✅ 已连接' : '❌ 未连接'],
      ['会话 ID', s.session_id || '-'],
      ['消息序号', s.last_seq],
      ['AI 模型', s.model],
      ['队列占用', s.queue_size + ' / ' + s.queue_max],
      ['心跳(秒)', s.heartbeat_interval],
      ['当前北京时间', s.beijing_time],
      ['私聊上下文文件', s.ctx_private],
      ['群聊上下文文件', s.ctx_group],
      ['日志文件数', s.log_count],
    ];
    grid.innerHTML = items.map(function(x){ return '<div class="kv"><div class="k">'+x[0]+'</div><div class="v">'+x[1]+'</div></div>'; }).join('');
    var cg = document.getElementById('cloud_grid');
    var cs = s.cloud_sync || {enabled:false, note:'未启用'};
    var citems;
    if(!cs.enabled){
      citems = [['云同步', cs.note || '未启用'],
                ['开启方式', '「配置」→「☁️ 云同步」填 Cloudflare D1 信息并启用']];
    }else if(cs.paused){
      citems = [
        ['状态', '⛔ 已暂停同步（连续失败 ' + 3 + ' 次触发）'],
        ['暂停原因', cs.pause_reason || '-'],
        ['恢复方式', cs.resume_text || '请点「立即同步一次」或重启程序'],
        ['失败文件', (cs.fail_files && cs.fail_files.length) ? cs.fail_files.join('、') : '-'],
        ['自动写入间隔', cs.interval_text || (cs.interval + ' 秒')],
      ];
    }else{
      citems = [
        ['状态', '✅ 已启用'],
        ['自动写入间隔', cs.interval_text || (cs.interval + ' 秒')],
        ['上次写入时间', cs.last_time_text || '尚未同步'],
        ['上次上传', cs.uploaded + ' 个文件'],
        ['上次恢复', cs.downloaded + ' 个文件'],
        ['上次删除标记', cs.deleted + ' 个'],
        ['应用云端删除', (cs.applied_remote_deletes || 0) + ' 个文件'],
        ['本地更新复活', (cs.revived || 0) + ' 个文件'],
        ['云端较新(改用云端)', (cs.cloud_newer || 0) + ' 个文件'],
        ['日志是否上云', cs.upload_logs ? '是' : '否（默认）'],
      ];
      if(cs.first_sync_done === false){
        citems.unshift(['首次同步', '⏳ 尚未执行（数据可能不是最新，连上 QQ 后自动同步）']);
      }
      if(cs.startup_buffer){
        citems.unshift(['启动写缓存', (cs.startup_buffer_text || '⏳ 缓存中') +
          '（启动期间的数据改动等第一次同步完成后再写盘，避免覆盖云端数据）']);
      }
    }
    cg.innerHTML = citems.map(function(x){ return '<div class="kv"><div class="k">'+x[0]+'</div><div class="v">'+x[1]+'</div></div>'; }).join('');
    document.getElementById('status_logs').textContent = s.recent_logs || '(无日志)';
  }catch(e){
    // 出错时直接把原因显示在徽章上，便于排查
    c.textContent = '⚠️ 状态加载失败';
    c.className = 'off';
    msg('状态加载失败: '+e.message, false);
  }
}
async function syncCloudNow(){
  try{
    var r = await fetch(url('/api/cloud_sync/sync'), {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    var res = await r.json();
    if(res.ok){
      var x = res.result || {};
      msg('☁️ 同步完成：上传 ' + (x.uploaded||0) + ' 个，恢复 ' + (x.downloaded||0) + ' 个，删除 ' + (x.deleted||0) + ' 个', true);
    }else{
      msg('❌ 同步失败: ' + (res.message||''), false);
    }
    loadStatus();
  }catch(e){ msg('同步失败: '+e.message, false); }
}
async function testCloudNow(){
  // 用「配置 → 云同步」里已保存的账户 ID / 数据库 ID / API 令牌测一次连接
  try{
    var r = await fetch(url('/api/cloud_sync/test'), {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    var res = await r.json();
    msg((res.ok ? '✅ ' : '❌ ') + (res.message || ''), !!res.ok);
  }catch(e){ msg('测试失败: '+e.message, false); }
}
async function resumeCloudNow(){
  try{
    var r = await fetch(url('/api/cloud_sync/resume'), {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    var res = await r.json();
    msg((res.ok ? '▶️ ' : '❌ ') + (res.message || ''), !!res.ok);
    loadStatus();
  }catch(e){ msg('解除暂停失败: '+e.message, false); }
}
/* ---------- 统计 ---------- */
var STAT_LABELS = {
  'messages': '收到消息', 'messages_c2c': '私聊消息', 'messages_group': '群聊消息',
  'messages_with_file': '含文件消息', 'ai_calls': 'AI 调用', 'ai_errors': 'AI 出错',
  'keyword_hits': '关键词命中', 'commands': '指令使用', 'filtered': '过滤消息',
  'replies': '回复条数', 'busy_replies': '繁忙提示', 'process_errors': '处理异常'
};
function statKV(obj){
  var keys = Object.keys(obj);
  if(!keys.length) return '<p style="color:var(--muted)">暂无数据</p>';
  return '<div class="grid">' + keys.map(function(k){
    var label = STAT_LABELS[k] || k;
    return '<div class="kv"><div class="k">'+esc(label)+'</div><div class="v">'+obj[k]+'</div></div>';
  }).join('') + '</div>';
}
async function loadStats(){
  try{
    var s = await getJSON('/api/stats');
    document.getElementById('stats_date').textContent = s.today || '';
    document.getElementById('stats_today').innerHTML = statKV(s.today_data);
    document.getElementById('stats_total').innerHTML = statKV(s.totals);
    // 7 天趋势图（纯 CSS 柱状图）
    var chart = document.getElementById('stats_chart');
    var days = s.recent || [];
    var max = 1;
    days.forEach(function(d){ var m = Math.max(d.messages||0, d.ai_calls||0, d.keyword_hits||0, d.replies||0, d.commands||0); if(m>max) max=m; });
    var html = '<div style="display:flex;gap:14px;align-items:flex-end;min-height:180px;padding-top:10px">';
    days.forEach(function(d){
      var h1 = Math.round((d.messages||0)/max*120), h2 = Math.round((d.ai_calls||0)/max*120), h3 = Math.round((d.replies||0)/max*120);
      html += '<div style="flex:1;text-align:center">' +
        '<div style="display:flex;gap:3px;justify-content:center;align-items:flex-end;height:130px">' +
        '<div title="消息 '+ (d.messages||0) +'" style="width:12px;background:linear-gradient(180deg,#4f46e5,#818cf8);border-radius:3px 3px 0 0;height:'+h1+'px"></div>' +
        '<div title="AI '+ (d.ai_calls||0) +'" style="width:12px;background:linear-gradient(180deg,#7c3aed,#c084fc);border-radius:3px 3px 0 0;height:'+h2+'px"></div>' +
        '<div title="回复 '+ (d.replies||0) +'" style="width:12px;background:linear-gradient(180deg,#16a34a,#86efac);border-radius:3px 3px 0 0;height:'+h3+'px"></div>' +
        '</div><div style="font-size:11px;color:var(--muted);margin-top:6px">'+esc(d.date)+'</div></div>';
    });
    html += '</div><div style="display:flex;gap:16px;justify-content:center;margin-top:10px;font-size:12px;color:var(--muted)">' +
      '<span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#4f46e5;margin-right:4px"></span>消息</span>' +
      '<span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#7c3aed;margin-right:4px"></span>AI 调用</span>' +
      '<span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#16a34a;margin-right:4px"></span>回复</span></div>';
    chart.innerHTML = html;
  }catch(e){ msg('统计加载失败: '+e.message, false); }
}
/* ---------- 插件 ---------- */
var PLUGIN_CHANGES = {};        // 暂存的修改：name -> 目标 disabled(布尔)
var PLUGIN_SERVER_STATE = {};   // 服务器上真实的 disabled 状态（判断是否属于“取消修改”）
async function loadPlugins(){
  var v = document.getElementById('plugins_view');
  v.innerHTML = '加载中...';
  try{
    var data = await getJSON('/api/plugins');
    var list = data.plugins || [];
    // 先记录服务器真实状态，再用暂存修改覆盖显示，并标记“待保存”
    PLUGIN_SERVER_STATE = {};
    list.forEach(function(p){ PLUGIN_SERVER_STATE[p.name] = !!p.disabled; });
    list.forEach(function(p){
      p._staged = (p.name in PLUGIN_CHANGES);
      if(p._staged){ p.disabled = PLUGIN_CHANGES[p.name]; }
    });
    updatePluginsPending();
    if(!list.length){
      v.innerHTML = '<p style="color:#888">暂无插件（把插件放进 plugins/ 目录后点「重新加载」）</p>';
      return;
    }
    v.innerHTML = '<div class="tbl-wrap"><table style="table-layout:fixed"><colgroup>' +
        '<col style="width:20%"><col style="width:28%"><col style="width:9%"><col style="width:16%"><col style="width:11%"><col style="width:16%">' +
        '</colgroup><tr><th>插件名</th><th>说明</th><th>类型</th><th>匹配规则</th><th>状态</th><th>操作</th></tr>' +
      list.map(function(p){
        var rules = [];
        if(p.commands && p.commands.length) rules.push('指令: ' + p.commands.join(', '));
        if(p.keywords && p.keywords.length) rules.push('关键词: ' + p.keywords.join(', '));
        if(p.match_custom) rules.push('自定义 match');
        var kind = (p.kind === 'dir') ? '<span class="badge warn">多文件</span>' : '<span class="badge">单文件</span>';
        // 有未保存修改时只显示一个“待启用/待停用”徽章：两个徽章并排会超出状态列，
        // 溢出后被右侧“启用/停用”按钮盖住，看起来像被遮挡。
        var badge = p._staged
          ? '<span class="badge warn" title="修改尚未保存，点上方「💾 保存」后生效">' +
            (p.disabled ? '待停用' : '待启用') + '</span>'
          : (p.disabled ? '<span class="badge err">已停用</span>'
                        : '<span class="badge ok">运行中</span>');
        var rowstyle = p._staged ? ' style="background:#fffdf3"' : '';
        var btn = p.disabled
          ? '<button class="ghost" data-pname="'+esc(p.name)+'" onclick="togglePlugin(this, false)">▶️ 启用</button>'
          : '<button class="ghost" data-pname="'+esc(p.name)+'" onclick="togglePlugin(this, true)">⏸️ 停用</button>';
        // 固定列宽 + 任意位置换行：长插件名/说明会在单元格内换行，不再溢出去压到旁边的列
        return '<tr'+rowstyle+'>' +
          '<td style="word-break:break-all;overflow-wrap:anywhere">'+esc(p.title)+
            ' <span style="color:#999;font-size:11px">('+esc(p.file)+')</span></td>'+
          '<td style="word-break:break-all;overflow-wrap:anywhere">'+esc(p.description||'-')+'</td>'+
          '<td class="cell-status">'+kind+'</td>'+
          '<td style="word-break:break-all;overflow-wrap:anywhere">'+esc(rules.join('; ')||'-')+'</td>'+
          '<td class="cell-status">'+badge+'</td>'+
          '<td class="cell-act">'+btn+'</td></tr>';
      }).join('') + '</table></div>';
  }catch(e){ v.innerHTML = '<p class="error">加载失败: '+e.message+'</p>'; }
}
function updatePluginsPending(){
  var n = Object.keys(PLUGIN_CHANGES).length;
  var tip = document.getElementById('plugins_pending');
  if(tip){
    tip.style.display = n ? 'inline-block' : 'none';
    tip.textContent = n ? ('⚠️ 有 ' + n + ' 项修改待保存，点左边「💾 保存」生效') : '';
  }
  var b = document.getElementById('plugins_save_btn');
  if(b){
    b.textContent = n ? ('💾 保存（' + n + ' 项待应用）') : '💾 保存（应用启用/停用修改）';
    b.style.boxShadow = n ? '0 0 0 3px rgba(245,210,142,.6)' : '';
  }
}
function togglePlugin(btn, disabled){
  // 只暂存修改，点「💾 保存」后才真正生效。
  // 关键：必须以「服务器真实状态」为基准判断，否则对已停用的插件点「启用」会被
  // 当成“取消修改”而立刻弹回原状，看起来像点了没反应。
  var name = btn.getAttribute('data-pname');
  if(!name){ return; }
  var base = !!PLUGIN_SERVER_STATE[name];
  if(disabled === base){
    delete PLUGIN_CHANGES[name];        // 目标与服务器当前一致 → 取消这条修改
  } else {
    PLUGIN_CHANGES[name] = disabled;    // 暂存目标状态（启用=false / 停用=true）
  }
  var n = Object.keys(PLUGIN_CHANGES).length;
  if(typeof msg === 'function'){
    msg(n ? ('已暂存修改（共 ' + n + ' 项），点「💾 保存」后生效') : '已取消未保存的修改', true);
  }
  loadPlugins();
}
async function applyPlugins(){
  var names = Object.keys(PLUGIN_CHANGES);
  if(!names.length){ msg('没有需要保存的修改', true); return; }
  try{
    var r = await fetch(url('/api/plugins/apply'), {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({changes: PLUGIN_CHANGES})});
    var res = await r.json();
    if(res.ok){
      PLUGIN_CHANGES = {};
      updatePluginsPending();
      msg('✅ 已保存插件设置，共 ' + (res.count||0) + ' 个插件运行中', true);
    } else {
      msg('❌ 保存失败: ' + (res.message||''), false);
    }
    loadPlugins();
  }catch(e){ msg('保存失败: '+e.message, false); }
}
async function refreshPlugins(){
  // 刷新 = 清掉暂存的启用/停用修改 + 重新扫描插件目录并加载
  PLUGIN_CHANGES = {};
  updatePluginsPending();
  try{
    var r = await fetch(url('/api/plugins/reload'), {method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'});
    var res = await r.json();
    msg(res.ok ? ('🔄 已刷新，共 ' + (res.count||0) + ' 个插件') : ('❌ 刷新失败: ' + (res.message||'')), res.ok);
    loadPlugins();
  }catch(e){ msg('刷新失败: '+e.message, false); }
}
/* ---------- 配置 ---------- */
var CONFIG_SPEC = null;
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function getPath(o, p){ try{ p.split('.').forEach(function(k){ o = o[k]; }); return o; }catch(e){ return undefined; } }
function cfgVal(p){ var v = getPath(CONFIG_SPEC.config, p); return (v === undefined || v === null) ? '' : v; }
function setPath(o, p, v){ var a=p.split('.'), n=o; for(var i=0;i<a.length-1;i++){ n = n[a[i]] || (n[a[i]]={}); } n[a[a.length-1]] = v; }
function rowDel(btn){
  var box = btn.closest('.kv-editor');
  btn.closest('.kv-row').remove();
  if(!box.querySelector('.kv-row')) addRow(box);
}
function addRow(box){
  var f = CONFIG_SPEC.fields[box.getAttribute('data-field')];
  var h = '<div class="kv-row">';
  if(f.type==='list'){ h += '<input type="text" class="item" placeholder="'+esc(f.item_label||'填写一项')+'">'; }
  else if(f.type==='kv'){ h += '<input type="text" class="kv-key" placeholder="'+esc(f.key_label||'关键词')+'">'; h += '<input type="text" class="kv-val" placeholder="'+esc(f.value_label||'回复内容')+'">'; }
  else if(f.type==='tasklist'){
    h += '<input type="text" class="t-time" placeholder="08:00">';
    h += '<select class="t-type"><option value="group">群聊</option><option value="c2c">私聊</option></select>';
    h += '<input type="text" class="t-id" placeholder="群/用户 openid">';
    h += '<input type="text" class="t-content" placeholder="发送内容">';
  }
  else if(f.type==='cmdlist'){
    h += '<select class="cmd-type"><option value="command">指令</option><option value="link">链接</option></select>';
    h += '<input type="text" class="cmd-name" placeholder="'+esc(f.name_label||'指令名/链接名')+'">';
    h += '<input type="text" class="cmd-desc" placeholder="'+esc(f.desc_label||'描述(指令)或链接地址(链接)')+'">';
  }
  h += '<button class="row-del" onclick="rowDel(this)">✕</button></div>';
  box.insertAdjacentHTML('beforeend', h);
}
function renderField(fid){
  var f = CONFIG_SPEC.fields[fid];
  if(!f) return '';
  var lbl = '<span class="lbl">'+esc(f.label)+'</span>'+(f.tip?'<span class="tip">'+esc(f.tip)+'</span>':'');
  if(f.type==='bool'){
    return '<label class="field" data-field="'+fid+'"><input type="checkbox" data-bool '+(getPath(CONFIG_SPEC.config,(f.path||fid))?'checked':'')+'> '+lbl+'</label>';
  }
  if(f.type==='number'){
    return '<label class="field" data-field="'+fid+'">'+lbl+'<input type="number" step="any" data-number value="'+cfgVal(f.path||fid)+'"></label>';
  }
  if(f.type==='secret'){
    return '<label class="field" data-field="'+fid+'">'+lbl+
      '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'+
      '<input type="password" data-text value="" placeholder="留空则保持不变" autocomplete="new-password" style="flex:1;min-width:200px">'+
      '<label style="margin:0;white-space:nowrap;font-size:12px;color:#8a6d1a"><input type="checkbox" data-clear> 清空此值（不再需要时勾选）</label>'+
      '</div></label>';
  }
  if(f.type==='textarea'){
    return '<label class="field" data-field="'+fid+'">'+lbl+'<textarea data-text>'+esc(String(cfgVal(f.path||fid)))+'</textarea></label>';
  }
  if(f.type==='list' || f.type==='kv' || f.type==='tasklist' || f.type==='cmdlist'){
    var h = '<div class="field" data-field="'+fid+'"><div class="flabel">'+lbl+'</div><div class="kv-editor" data-field="'+fid+'">';
    if(f.type==='list'){
      (getPath(CONFIG_SPEC.config,(f.path||fid))||[]).forEach(function(v){
        h += '<div class="kv-row"><input type="text" class="item" placeholder="'+esc(f.item_label||'')+'" value="'+esc(v)+'"><button class="row-del" onclick="rowDel(this)">✕</button></div>';
      });
      if(!(getPath(CONFIG_SPEC.config,(f.path||fid))||[]).length) h += '<div class="kv-row"><input type="text" class="item" placeholder="'+esc(f.item_label||'')+'"><button class="row-del" onclick="rowDel(this)">✕</button></div>';
    } else if(f.type==='kv'){
      var keys = getPath(CONFIG_SPEC.config, f.key_path)||[];
      var vals = getPath(CONFIG_SPEC.config, f.value_path)||{};
      var max = Math.max(keys.length, 1);
      for(var i=0;i<max;i++){
        var k = keys[i] || '';
        var v = vals[k] || '';
        h += '<div class="kv-row"><input type="text" class="kv-key" placeholder="'+esc(f.key_label)+'" value="'+esc(k)+'"><input type="text" class="kv-val" placeholder="'+esc(f.value_label)+'" value="'+esc(v)+'"><button class="row-del" onclick="rowDel(this)">✕</button></div>';
      }
    } else if(f.type==='tasklist'){
      var tasks = getPath(CONFIG_SPEC.config,(f.path||fid))||[];
      if(!tasks.length){ tasks = [{}]; }
      tasks.forEach(function(t){
        t = t || {};
        h += '<div class="kv-row"><input type="text" class="t-time" placeholder="08:00" value="'+esc(t.time||'')+'">'+
          '<select class="t-type"><option value="group"'+(t.target_type==='c2c'?'':' selected')+'>群聊</option><option value="c2c"'+(t.target_type==='c2c'?' selected':'')+'>私聊</option></select>'+
          '<input type="text" class="t-id" placeholder="群/用户 openid" value="'+esc(t.target_id||'')+'">'+
          '<input type="text" class="t-content" placeholder="发送内容" value="'+esc(t.content||'')+'">'+
          '<button class="row-del" onclick="rowDel(this)">✕</button></div>';
      });
    } else if(f.type==='cmdlist'){
      var cmds = getPath(CONFIG_SPEC.config,(f.path||fid))||[];
      if(!cmds.length){ cmds = [{}]; }
      cmds.forEach(function(c){
        c = c || {};
        var ctype = (c.type==='link') ? 'link' : 'command';
        var cval = (ctype==='link') ? (c.link||'') : (c.desc||'');
        h += '<div class="kv-row"><select class="cmd-type">'+
          '<option value="command"'+(ctype==='command'?' selected':'')+'>指令</option>'+
          '<option value="link"'+(ctype==='link'?' selected':'')+'>链接</option></select>'+
          '<input type="text" class="cmd-name" placeholder="'+esc(f.name_label||'指令名/链接名')+'" value="'+esc(c.name||'')+'">'+
          '<input type="text" class="cmd-desc" placeholder="'+esc(f.desc_label||'描述(指令)或链接地址(链接)')+'" value="'+esc(cval)+'">'+
          '<button class="row-del" onclick="rowDel(this)">✕</button></div>';
      });
    }
    h += '</div><button class="row-add" onclick="addRow(this.previousElementSibling)">➕ 添加一行</button></div>';
    return h;
  }
  return '<label class="field" data-field="'+fid+'">'+lbl+'<input type="text" data-text value="'+esc(String(cfgVal(f.path||fid)))+'"></label>';
}
async function loadConfig(){
  try{
    CONFIG_SPEC = await getJSON('/api/config');
    var html = '';
    CONFIG_SPEC.sections.forEach(function(sec){
      html += '<div class="card"><h3>'+esc(sec.title)+'</h3>'+(sec.tip?'<div class="tip" style="color:#888;font-size:12px;margin-bottom:8px">'+esc(sec.tip)+'</div>':'');
      sec.fields.forEach(function(fid){ html += renderField(fid); });
      html += '</div>';
    });
    document.getElementById('config_form').innerHTML = html;
  }catch(e){ msg('配置加载失败: '+e.message, false); }
}
function collectField(fid, newCfg){
  var f = CONFIG_SPEC.fields[fid];
  var el = document.querySelector('.field[data-field="'+fid+'"]');
  if(!el) return;
  if(f.type==='bool'){ setPath(newCfg, (f.path||fid), el.querySelector('[data-bool]').checked); }
  else if(f.type==='number'){ var nv = el.querySelector('[data-number]').value; setPath(newCfg, (f.path||fid), nv===''?'':Number(nv)); }
  else if(f.type==='secret'){
    var clearEl = el.querySelector('[data-clear]');
    var v = el.querySelector('[data-text]').value;
    if(clearEl && clearEl.checked) v = '__CLEAR__';   // 勾选清空 = 真正删掉该密钥
    setPath(newCfg, (f.path||fid), v);
  }
  else if(f.type==='textarea'){ setPath(newCfg, (f.path||fid), el.querySelector('[data-text]').value); }
  else if(f.type==='text'){ setPath(newCfg, (f.path||fid), el.querySelector('[data-text]').value); }
  else if(f.type==='list'){
    var arr = [];
    el.querySelectorAll('.kv-row .item').forEach(function(inp){ var v = inp.value.trim(); if(v) arr.push(v); });
    setPath(newCfg, (f.path||fid), arr);
  }
  else if(f.type==='kv'){
    var keys = [], vals = {};
    el.querySelectorAll('.kv-row').forEach(function(row){
      var k = row.querySelector('.kv-key').value.trim();
      var v = row.querySelector('.kv-val').value.trim();
      if(!k && !v) return;
      keys.push(k); vals[k] = v;
    });
    setPath(newCfg, f.key_path, keys);
    setPath(newCfg, f.value_path, vals);
  }
  else if(f.type==='tasklist'){
    var tasks = [];
    el.querySelectorAll('.kv-row').forEach(function(row){
      var t = row.querySelector('.t-time').value.trim();
      var ty = row.querySelector('.t-type').value;
      var id = row.querySelector('.t-id').value.trim();
      var c = row.querySelector('.t-content').value.trim();
      if(!t && !id && !c) return;
      tasks.push({time:t, target_type:ty, target_id:id, content:c});
    });
    setPath(newCfg, (f.path||fid), tasks);
  }
  else if(f.type==='cmdlist'){
    var cmds = [];
    el.querySelectorAll('.kv-row').forEach(function(row){
      var ty = row.querySelector('.cmd-type').value;
      var n = row.querySelector('.cmd-name').value.trim();
      var d = row.querySelector('.cmd-desc').value.trim();
      if(!n) return;
      if(ty === 'link') cmds.push({type:'link', name:n, link:d});
      else cmds.push({type:'command', name:n, desc:d});
    });
    setPath(newCfg, (f.path||fid), cmds);
  }
}
async function saveConfig(){
  if(!CONFIG_SPEC) return;
  var newCfg = {};
  var err = null;
  Object.keys(CONFIG_SPEC.fields).forEach(function(fid){
    if(err) return;
    try{ collectField(fid, newCfg); }
    catch(e){ err = '「' + fid + '」填写有误: ' + e.message; }
  });
  if(err){ msg('❌ ' + err, false); return; }
  try{
    var r = await fetch(url('/api/config/save'), {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(newCfg)});
    var res = await r.json();
    if(res.ok){ msg('✅ ' + res.message, true); loadConfig(); }
    else { msg('❌ ' + res.message, false); }
  }catch(e){ msg('保存失败: '+e.message, false); }
}
/* ---------- 日志 ---------- */
async function loadLogs(){
  try{
    var n = document.getElementById('log_lines').value || 200;
    var lv = document.getElementById('log_level').value || '';
    var r = await fetch(url('/api/logs?lines='+n+'&level='+encodeURIComponent(lv)));
    var text = await r.text();
    var view = document.getElementById('logs_view');
    view.textContent = text.trim() ? text : '(该级别暂无日志)';
  }catch(e){ msg('日志加载失败: '+e.message, false); }
}
async function loadLogFiles(){
  try{
    var d = await getJSON('/api/logs/list');
    var sel = document.getElementById('log_file');
    var files = d.files || [];
    var keep = sel.value;
    sel.innerHTML = files.length
      ? files.map(function(f){
          return '<option value="'+esc(f.name)+'">'+esc(f.name)+'（'+esc(f.size_text||'')+'）</option>';
        }).join('')
      : '<option value="">（暂无日志文件）</option>';
    if(keep && files.some(function(f){ return f.name === keep; })) sel.value = keep;
    // 明确写出"下方显示的是最新日志"，避免误以为切换下拉会改变这里的内容
    var head = document.getElementById('logs_current');
    if(head){
      head.innerHTML = files.length
        ? ('当前显示：<b>最新</b>日志文件 <b>' + esc(d.current || files[0].name) + '</b>' +
           '（共 ' + files.length + ' 个；上方下拉仅用于下载，不影响这里）')
        : '当前显示：暂无日志文件';
    }
  }catch(e){ /* 文件列表失败不影响看日志 */ }
}
function downloadLog(){
  var sel = document.getElementById('log_file');
  var name = sel ? sel.value : '';
  if(!name){ msg('请先选择一个日志文件', false); return; }
  msg('开始下载 ' + name + '（页面下方显示的内容不受影响）…', true);
  window.location.href = url('/api/logs/download?name=' + encodeURIComponent(name));
}
/* ---------- 上下文 ---------- */
async function loadContext(){
  try{
    var d = await getJSON('/api/context');
    var html = '<div class="card"><h3>私聊上下文 ('+d.private.length+' 个)</h3>' + tableHTML(d.private) + '</div>';
    html += '<div class="card"><h3>群聊上下文 ('+d.group.length+' 个)</h3>' + tableHTML(d.group) + '</div>';
    document.getElementById('context_view').innerHTML = html;
  }catch(e){ msg('上下文加载失败: '+e.message, false); }
}
function tableHTML(rows){
  if(!rows.length) return '<p style="color:#888">暂无文件</p>';
  // 文件名（openid）很长且没有空格，必须允许任意位置换行，否则会超出卡片；
  // 外层 .tbl-wrap 负责在屏幕很窄时横向滚动，避免挤压变形。
  return '<div class="tbl-wrap"><table><tr><th style="width:46%">文件</th>' +
    '<th style="width:16%">消息条数</th><th style="width:18%">大小</th><th style="width:20%">最后修改</th></tr>' +
    rows.map(function(r){
      return '<tr>' +
        '<td style="word-break:break-all;overflow-wrap:anywhere;font-size:12.5px">'+esc(r.name)+'</td>' +
        '<td>'+esc(r.entries)+'</td><td class="nowrap">'+esc(r.size)+'</td><td class="nowrap">'+esc(r.mtime)+'</td>' +
        '</tr>';
    }).join('') + '</table></div>';
}
/* ---------- 指令面板管理 ---------- */
async function loadPanels(){
  var v = document.getElementById('panels_view');
  v.innerHTML = '加载中...';
  try{
    var data = await getJSON('/api/panels');
    var list = Array.isArray(data) ? data : (data && data.panels ? data.panels : []);
    var diag = (!Array.isArray(data) && data && data.diag) ? data.diag : null;
    if(!list.length){
      var d = '';
      if(diag){
        d = '<div style="margin-top:10px;font-size:12px;color:#888;white-space:pre-wrap;word-break:break-all">接口原始返回：<br>' +
          esc('私聊: ' + (diag.c2c||'(空)')) + '<br>' + esc('群聊: ' + (diag.group||'(空)')) + '</div>';
      }
      v.innerHTML = '<p style="color:#888">暂无指令面板（或未开通权限）</p>' + d;
      return;
    }
    v.innerHTML = '<div class="tbl-wrap"><table><tr><th style="width:14%">生效场景</th>' +
      '<th style="width:34%">面板ID</th><th style="width:38%">备注/内容</th><th style="width:14%">操作</th></tr>' +
      list.map(function(p){
        var id = p.panel_id || p.id || '';
        var sc = (p.scope === 'group') ? '群聊' : ((p.scope === 'c2c') ? '私聊' : (p.scope || '?'));
        var meta = p.remark || (p.panel && p.panel.remark) || JSON.stringify(p).substring(0, 60);
        return '<tr data-pid="'+esc(id)+'">' +
          '<td class="nowrap">'+esc(sc)+'</td>' +
          '<td style="word-break:break-all;overflow-wrap:anywhere">'+esc(id)+'</td>' +
          '<td style="word-break:break-all;overflow-wrap:anywhere">'+esc(meta)+'</td>' +
          '<td class="nowrap"><button class="row-del" onclick="delPanelRow(this)">删除</button></td></tr>';
      }).join('') + '</table></div>';
  }catch(e){ v.innerHTML = '<p class="error">加载失败: '+e.message+'</p>'; }
}
function delPanelRow(btn){
  var id = btn.closest('tr').getAttribute('data-pid');
  if(id) delPanel(id);
}
async function delPanel(id){
  if(!confirm('确认删除指令面板 ' + id + ' ？（删除后腾出额度，可重新创建）')) return;
  try{
    var r = await fetch(url('/api/panels/delete'), {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({panel_id:id})});
    var res = await r.json();
    msg(res.ok ? '✅ 面板已删除' : '❌ 删除失败: ' + (res.message||''), res.ok);
    loadPanels();
  }catch(e){ msg('删除失败: '+e.message, false); }
}
async function reloadPanels(){
  try{
    var r = await fetch(url('/api/panels/reload'), {method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'});
    var res = await r.json();
    msg(res.ok ? '✅ 已按当前配置重新注册' : '❌ 重新注册失败: ' + (res.message||''), res.ok);
    loadPanels();
  }catch(e){ msg('重新注册失败: '+e.message, false); }
}
/* ---------- 自动刷新 ---------- */
var autoTimer = null;
function toggleAuto(){
  if(autoTimer){ clearInterval(autoTimer); autoTimer=null; }
  var on = document.getElementById('auto_status').checked || document.getElementById('auto_logs').checked;
  if(on) autoTimer = setInterval(function(){
    if(document.getElementById('tab_status').className.indexOf('active')>=0) loadStatus();
    if(document.getElementById('tab_logs').className.indexOf('active')>=0) loadLogs();
  }, 5000);
}
show('status');
</script>
</body>
</html>
"""


class WebAdmin:
    """轻量 Web 管理后台（含配置在线编辑）"""

    def __init__(self, host: str, port: int, token: str, hub, logger=None):
        self.host = host
        self.port = int(port)
        self.token = token or ''
        self.hub = hub
        self.logger = logger
        self.start_time = time.time()
        self._server = None
        self._thread = None

    def start(self):
        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        if self.logger:
            self.logger.info(f"Web管理后台已启动: http://{self.host}:{self.port}/")

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass

    # ---------- 数据 ----------
    def get_status(self) -> dict:
        hub = self.hub
        ws = hub.qq_client
        mp = hub.message_processor
        connected = bool(ws and ws.ws and ws.ws.sock and ws.ws.sock.connected)
        recent_logs = ''.join(self.get_logs(20)).rstrip()
        ctx = self.get_context()
        return {
            'uptime_seconds': int(time.time() - self.start_time),
            'uptime_text': _fmt_uptime(time.time() - self.start_time),
            'ws_connected': connected,
            'session_id': ws.session_id if ws else None,
            'last_seq': ws.last_seq if ws else None,
            'heartbeat_interval': getattr(ws, 'heartbeat_interval', None),
            'model': hub.ai_client.model if hub.ai_client else None,
            'queue_size': mp._task_queue.qsize() if mp else -1,
            'queue_max': mp.max_queue_size if mp else 0,
            'beijing_time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600)),
            'ctx_private': len(ctx.get('private', [])),
            'ctx_group': len(ctx.get('group', [])),
            'log_count': self._log_count(),
            'recent_logs': recent_logs,
            'cloud_sync': self.get_cloud_sync_status(),
        }

    def get_cloud_sync_status(self) -> dict:
        """云同步状态（供状态页展示：上次写入时间、本次上传/恢复/删除数量）"""
        cs = getattr(self.hub, 'cloud_sync', None)
        if cs is None:
            cs_cfg = (self.hub.config or {}).get('cloud_sync') or {}
            if not cs_cfg.get('enabled'):
                return {'enabled': False, 'note': '未启用'}
            return {'enabled': False, 'note': '已启用但配置不完整（account_id / database_id / api_token）'}
        res = dict(getattr(cs, 'last_result', {}) or {})
        ts = res.get('time')
        pause = {}
        try:
            pause = cs.pause_info()
        except Exception:
            pause = {}
        try:
            buf = startup_buffer_state()
        except Exception:
            buf = {}                       # 启动写缓存状态（首次同步完成前才有内容）
        return {
            'enabled': True,
            'interval': cs.interval,
            'interval_text': ('%d 秒（对齐整点倍数）' % cs.interval) if cs.interval >= 60
                             else ('%d 秒' % cs.interval),
            'uploaded': int(res.get('uploaded', 0)),
            'downloaded': int(res.get('downloaded', 0)),
            'deleted': int(res.get('deleted', 0)),
            'applied_remote_deletes': int(res.get('applied_remote_deletes', 0)),
            'revived': int(res.get('revived', 0)),
            'cloud_newer': int(res.get('cloud_newer', 0)),
            'skipped': int(res.get('skipped', 0)),
            'errors': int(res.get('errors', 0)),
            'first_sync_done': bool(getattr(cs, 'first_sync_done', True)),
            'first_sync_hint': ('尚未执行首次同步：刚启动的这段时间数据可能不是最新的，'
                                '成功连接 QQ 后会自动同步'),
            'startup_buffer': bool(buf.get('active')),
            'startup_buffer_text': ('⏳ 缓存中：%d 个文件改动还没落盘（%s / 上限 %s，等第一次同步完成）'
                                    % (int(buf.get('files', 0)), buf.get('size_text', '0 字节'),
                                       buf.get('max_text', '不限制'))) if buf.get('active') else '',
            'paused': bool(pause.get('paused')),
            'pause_reason': pause.get('reason', ''),
            'resume_text': pause.get('resume_text', ''),
            'fail_files': pause.get('fail_files', []),
            'last_time': ts,
            'last_time_text': (time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts + 8 * 3600))
                               if ts else ''),
            'upload_logs': bool(getattr(cs, 'upload_logs', False)),
        }

    def get_config(self) -> dict:
        """返回配置 + 编辑器渲染信息（敏感字段置空，不显示真实密钥）

        先用默认值补齐缺失项再交给页面：否则"配置里没有这个键"（例如升级后新增的
        选项）会显示成关闭/空值，用户一保存就把默认值写成了 false/0，功能被意外关掉。
        """
        try:
            live = self.hub.config_manager.config or {}
            cfg = _deep_merge(self.hub.config_manager.DEFAULT_CONFIG, live)
        except Exception:
            cfg = self.hub.config_manager.config
        return {
            'config': _blank_secrets(cfg),
            'sections': RENDER_SECTIONS,
            'fields': RENDER_FIELDS,
        }

    def save_config(self, new_config: dict) -> dict:
        """保存配置：深度合并（敏感字段留空则保留原值）→ 写回 config.json（原子）→ 热应用"""
        if not isinstance(new_config, dict):
            return {'ok': False, 'message': '配置格式错误：必须是 JSON 对象'}
        current = self.hub.config_manager.config
        merged = _merge_with_secrets(current, new_config)
        path = self.hub.config_manager.CONFIG_FILE
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            # 更新热更新监视线程记录的 mtime，避免保存后又被监视线程重复应用一次
            try:
                self.hub._last_config_mtime = os.path.getmtime(path)
            except Exception:
                pass
        except Exception as e:
            return {'ok': False, 'message': f'写入 config.json 失败: {e}'}
        # 热应用
        try:
            changed = self.hub._apply_hot_config(source='Web后台')
        except Exception as e:
            changed = None
        hot = (', '.join(changed) if changed else '无热更新项') if changed is not None else '（应用异常）'
        return {'ok': True, 'message': f'保存成功，已热应用: {hot}。连接类配置（AppID/密钥/沙箱等）需重启生效。'}

    def list_log_files(self) -> list:
        """列出 data/logs 下的日志文件（按时间倒序，新的在前）"""
        d = os.path.join('data', 'logs')
        out = []
        try:
            for fn in os.listdir(d):
                if not fn.endswith('.txt'):
                    continue
                full = os.path.join(d, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                out.append({
                    'name': fn,
                    'size': int(st.st_size),
                    'size_text': _fmt_size(st.st_size),
                    'mtime': int(st.st_mtime),
                    'mtime_text': time.strftime('%Y-%m-%d %H:%M:%S',
                                                time.gmtime(st.st_mtime + 8 * 3600)),
                })
        except Exception:
            pass
        # 按修改时间倒序；同一秒内创建的再按文件名倒序（文件名里就是时间戳）保证顺序稳定
        out.sort(key=lambda x: (x['mtime'], x['name']), reverse=True)
        return out

    def log_file_path(self, name: str):
        """把前端传来的日志文件名解析成安全路径（拦住目录穿越/非 .txt）"""
        name = (name or '').strip()
        if not name or not name.endswith('.txt'):
            return None
        if name != os.path.basename(name) or '/' in name or '\\' in name or name.startswith('.'):
            return None
        full = os.path.join('data', 'logs', name)
        return full if os.path.isfile(full) else None

    def read_log_file(self, name: str):
        """读取指定日志文件内容（返回 (文件名, bytes) 或 (None, None)）"""
        full = self.log_file_path(name)
        if not full:
            return None, None
        try:
            with open(full, 'rb') as f:
                return os.path.basename(full), f.read()
        except Exception:
            return None, None

    def get_logs(self, lines: int = 100, level: str = '') -> list:
        """读取最新日志文件（只认 .txt，避免把 logs 目录里的图片等文件当文本读）。

        level 可选：DEBUG / INFO / WARNING / ERROR / CRITICAL（留空 = 全部）。
        按级别过滤时，返回最近 lines 条匹配该级别的日志行。

        ⚠️ 返回值里每行都**带结尾换行符**（和 readlines() 一致）——调用方直接用
        ''.join(...) 拼成文本即可；不要去掉换行符，否则页面上的日志会挤成一行。
        """
        log_dir = os.path.join('data', 'logs')
        try:
            files = sorted(f for f in os.listdir(log_dir) if f.endswith('.txt'))
        except Exception:
            return []
        if not files:
            return []
        newest = os.path.join(log_dir, files[-1])
        limit = max(1, min(int(lines), 500))
        marker = f" - {level} - " if level else ''
        try:
            # 从文件尾部往前逐块读取（最多回看 MAX_LOG_SCAN）：
            # 状态页每 5 秒刷新一次，把整份日志（最大 10MB）读进内存没有必要，也浪费内存。
            out = []                       # 按"新 → 旧"收集
            with open(newest, 'rb') as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                scanned = 0
                while pos > 0 and scanned < MAX_LOG_SCAN and len(out) < limit:
                    size = int(min(64 * 1024, pos, MAX_LOG_SCAN - scanned))
                    pos -= size
                    f.seek(pos)
                    chunk = f.read(size).decode('utf-8', errors='replace')
                    scanned += size
                    chunk_lines = chunk.splitlines()
                    if pos > 0 and chunk_lines:
                        chunk_lines = chunk_lines[1:]     # 块首可能是半行，丢掉
                    for ln in reversed(chunk_lines):
                        if marker and marker not in ln:
                            continue
                        out.append(ln + '\n')      # 调用方用 ''.join() 拼接，必须带换行
                        if len(out) >= limit:
                            break
            return list(reversed(out))
        except Exception:
            return []

    def _log_count(self) -> int:
        try:
            return len([f for f in os.listdir(os.path.join('data', 'logs')) if f.endswith('.txt')])
        except Exception:
            return 0

    def get_stats(self) -> dict:
        """返回统计数据：今日明细、最近 7 天、历史累计"""
        stats = getattr(getattr(self.hub, 'message_processor', None), 'stats', None)
        if stats is None:
            return {'today': '', 'today_data': {}, 'recent': [], 'totals': {}}
        recent = []
        for d, data in stats.get_recent(7).items():
            row = {'date': d}
            row.update(data)
            recent.append(row)
        return {
            'today': stats._today(),
            'today_data': stats.get_today(),
            'recent': recent,
            'totals': stats.totals(),
        }

    def get_plugins(self) -> dict:
        """返回插件状态列表（供 Web 展示，含已停用的插件）"""
        pm = getattr(self.hub, 'plugin_manager', None)
        if pm is None:
            return {'enabled': False, 'plugins': []}
        plugins = pm.list_plugins()
        # 补上 match_custom 标志（仅已加载的插件有该字段）
        with getattr(pm, '_lock', threading.RLock()):
            loaded = {p['name']: p for p in getattr(pm, 'plugins', []) or []}
        for item in plugins:
            entry = loaded.get(item['name'])
            item['match_custom'] = bool(entry and entry.get('match_fn') is not None)
        return {'enabled': True, 'plugins': plugins}

    def get_context(self) -> dict:
        ctx_dir = os.path.join('data', 'user_context')
        result = {'private': [], 'group': []}
        try:
            for root, _dirs, files in os.walk(ctx_dir):
                for fn in sorted(files):
                    if not fn.endswith('.json'):
                        continue
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, ctx_dir)
                    try:
                        with open(full, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        entries = len(data.get('history', [])) if isinstance(data, dict) else 0
                    except Exception:
                        entries = -1
                    try:
                        size = os.path.getsize(full)
                        mtime = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(full)))
                    except Exception:
                        size, mtime = 0, '-'
                    if rel.startswith('private'):
                        section = 'private'
                    elif rel.startswith('group'):
                        section = 'group'
                    else:
                        continue  # bindings.json 等非上下文文件不展示
                    result.setdefault(section, []).append({
                        'name': rel, 'entries': entries, 'size': f'{size} B', 'mtime': mtime,
                    })
        except Exception:
            pass
        return result

    # ---------- HTTP ----------
    def _check_token(self, query: str) -> bool:
        if not self.token:
            return True
        return parse_qs(query).get('token', [''])[0] == self.token

    def _make_handler(self):
        admin = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, body, ctype: str = 'application/json; charset=utf-8',
                      extra_headers: dict = None):
                data = body if isinstance(body, bytes) else body.encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                for _k, _v in (extra_headers or {}).items():
                    self.send_header(_k, _v)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass

            def _auth(self, query: str) -> bool:
                if not admin._check_token(query):
                    self._send(403, json.dumps({'ok': False, 'message': 'token 无效'}, ensure_ascii=False))
                    return False
                return True

            def do_GET(self):
                parsed = urlparse(self.path)
                path, query = parsed.path, parsed.query
                if not self._auth(query):
                    return
                try:
                    if path in ('/', '/index.html'):
                        self._send(200, PAGE_HTML, 'text/html; charset=utf-8')
                    elif path == '/api/status':
                        self._send(200, json.dumps(admin.get_status(), ensure_ascii=False, indent=2))
                    elif path == '/api/config':
                        self._send(200, json.dumps(admin.get_config(), ensure_ascii=False, indent=2))
                    elif path == '/api/logs':
                        q = parse_qs(query)
                        n = q.get('lines', ['100'])[0]
                        lv = (q.get('level', [''])[0] or '').strip().upper()
                        self._send(200, ''.join(admin.get_logs(n, lv)), 'text/plain; charset=utf-8')
                    elif path == '/api/logs/list':
                        files = admin.list_log_files()
                        self._send(200, json.dumps(
                            {'files': files,
                             'current': (files[0]['name'] if files else ''),
                             'readonly_note': '列表仅用于下载；页面下方的运行日志始终显示最新的日志文件'},
                            ensure_ascii=False, indent=2))
                    elif path == '/api/logs/download':
                        name = parse_qs(query).get('name', [''])[0]
                        fname, data = admin.read_log_file(name)
                        if not fname:
                            self._send(404, json.dumps(
                                {'ok': False, 'message': '日志文件不存在或名称不合法'},
                                ensure_ascii=False))
                        else:
                            from urllib.parse import quote
                            ascii_name = fname.encode('ascii', 'ignore').decode() or 'log.txt'
                            self._send(200, data, 'text/plain; charset=utf-8',
                                       {'Content-Disposition':
                                        "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                                        % (ascii_name, quote(fname))})
                    elif path == '/api/stats':
                        self._send(200, json.dumps(admin.get_stats(), ensure_ascii=False, indent=2))
                    elif path == '/api/plugins':
                        self._send(200, json.dumps(admin.get_plugins(), ensure_ascii=False, indent=2))
                    elif path == '/api/context':
                        self._send(200, json.dumps(admin.get_context(), ensure_ascii=False, indent=2))
                    elif path == '/api/panels':
                        try:
                            panels = []
                            diag = {}
                            for sc in ('c2c', 'group'):
                                sc_panels, raw = admin.hub.qq_client.list_command_panels(sc, with_raw=True)
                                for p in sc_panels:
                                    if isinstance(p, dict) and not p.get('scope'):
                                        p['scope'] = sc
                                    panels.append(p)
                                diag[sc] = (raw or '')[:800]
                            self._send(200, json.dumps(
                                {'panels': panels, 'diag': diag}, ensure_ascii=False, indent=2))
                        except Exception as e:
                            self._send(500, json.dumps({'error': str(e)}, ensure_ascii=False))
                    else:
                        self._send(404, json.dumps({'ok': False, 'message': 'not found'}, ensure_ascii=False))
                except Exception as e:
                    self._send(500, json.dumps({'ok': False, 'message': str(e)}, ensure_ascii=False))

            def do_POST(self):
                parsed = urlparse(self.path)
                path, query = parsed.path, parsed.query
                if not self._auth(query):
                    return
                if path != '/api/config/save':
                    if path == '/api/panels/delete':
                        try:
                            length = int(self.headers.get('Content-Length', 0))
                            raw = self.rfile.read(length).decode('utf-8') if length else '{}'
                            panel_id = (json.loads(raw) or {}).get('panel_id', '')
                            if not panel_id:
                                self._send(400, json.dumps({'ok': False, 'message': '缺少 panel_id'}, ensure_ascii=False))
                                return
                            ok = admin.hub.qq_client.delete_command_panel(panel_id)
                            self._send(200, json.dumps({'ok': ok, 'message': '删除成功' if ok else '删除失败（见日志）'}, ensure_ascii=False))
                        except Exception as e:
                            self._send(500, json.dumps({'ok': False, 'message': str(e)}, ensure_ascii=False))
                        return
                    if path == '/api/panels/reload':
                        try:
                            admin.hub._register_command_panel()
                            self._send(200, json.dumps({'ok': True, 'message': '已按当前配置重新注册（结果见日志）'}, ensure_ascii=False))
                        except Exception as e:
                            self._send(500, json.dumps({'ok': False, 'message': str(e)}, ensure_ascii=False))
                        return
                    if path == '/api/cloud_sync/test':
                        try:
                            from core.cloud_sync import test_connection
                            cs_cfg = (admin.hub.config or {}).get('cloud_sync') or {}
                            err = test_connection(cs_cfg.get('account_id', ''),
                                                  cs_cfg.get('database_id', ''),
                                                  cs_cfg.get('api_token', ''))
                            self._send(200 if not err else 400,
                                       json.dumps({'ok': not err,
                                                   'message': err or '连接成功：令牌与数据库可用，数据表已就绪'},
                                                  ensure_ascii=False))
                        except Exception as e:
                            self._send(500, json.dumps({'ok': False, 'message': str(e)},
                                                       ensure_ascii=False))
                        return
                    if path == '/api/cloud_sync/resume':
                        try:
                            cs = getattr(admin.hub, 'cloud_sync', None)
                            if cs is None:
                                self._send(400, json.dumps(
                                    {'ok': False, 'message': '云同步未启用或配置不完整'},
                                    ensure_ascii=False))
                                return
                            cs.resume('手动解除暂停')
                            self._send(200, json.dumps({'ok': True, 'message': '已解除暂停，'
                                                                              '下个周期（或立即同步）将继续'},
                                                       ensure_ascii=False))
                        except Exception as e:
                            self._send(500, json.dumps({'ok': False, 'message': str(e)},
                                                       ensure_ascii=False))
                        return
                    if path == '/api/cloud_sync/sync':
                        try:
                            cs = getattr(admin.hub, 'cloud_sync', None)
                            if cs is None:
                                self._send(400, json.dumps(
                                    {'ok': False, 'message': '云同步未启用或配置不完整（配置 → 云同步）'},
                                    ensure_ascii=False))
                                return
                            result = cs.sync_once(force=True)   # 手动同步：即使暂停也强制试一次
                            self._send(200, json.dumps({'ok': True, 'result': result},
                                                       ensure_ascii=False))
                        except Exception as e:
                            self._send(500, json.dumps({'ok': False, 'message': str(e)},
                                                       ensure_ascii=False))
                        return
                    if path == '/api/plugins/reload':
                        try:
                            pm = getattr(admin.hub, 'plugin_manager', None)
                            if pm is None:
                                self._send(500, json.dumps({'ok': False, 'message': '插件系统未启用'}, ensure_ascii=False))
                                return
                            plugins = pm.reload()
                            self._send(200, json.dumps({'ok': True, 'count': len(plugins)}, ensure_ascii=False))
                        except Exception as e:
                            self._send(500, json.dumps({'ok': False, 'message': str(e)}, ensure_ascii=False))
                        return
                    if path == '/api/plugins/apply':
                        try:
                            length = int(self.headers.get('Content-Length', 0))
                            raw = self.rfile.read(length).decode('utf-8') if length else '{}'
                            body = json.loads(raw) or {}
                            changes = body.get('changes') or {}
                            pm = getattr(admin.hub, 'plugin_manager', None)
                            if pm is None:
                                self._send(500, json.dumps({'ok': False, 'message': '插件系统未启用'}, ensure_ascii=False))
                                return
                            if not isinstance(changes, dict):
                                self._send(400, json.dumps({'ok': False, 'message': 'changes 必须是对象'}, ensure_ascii=False))
                                return
                            # 只接受真实存在的插件名（已加载的 + 已禁用未加载的，来自 list_plugins）：
                            # 避免有人（或误操作）往禁用列表里塞任意名字，让列表无限变大
                            known = {str(p.get('name')) for p in (pm.list_plugins() or [])}
                            unknown = [str(n) for n in changes if str(n) not in known]
                            if unknown:
                                self._send(400, json.dumps(
                                    {'ok': False,
                                     'message': '未知插件名（未在 plugins/ 目录找到）: '
                                                + '、'.join(unknown[:5])},
                                    ensure_ascii=False))
                                return
                            for name, disabled in changes.items():
                                pm.set_disabled(str(name), bool(disabled))
                            count = pm.apply_changes()
                            self._send(200, json.dumps({'ok': True, 'count': count}, ensure_ascii=False))
                        except Exception as e:
                            self._send(500, json.dumps({'ok': False, 'message': str(e)}, ensure_ascii=False))
                        return
                    self._send(404, json.dumps({'ok': False, 'message': 'not found'}, ensure_ascii=False))
                    return
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if length <= 0 or length > MAX_BODY:
                        self._send(400, json.dumps({'ok': False, 'message': '请求体过大或为空'}, ensure_ascii=False))
                        return
                    raw = self.rfile.read(length).decode('utf-8')
                    new_config = json.loads(raw)
                    result = admin.save_config(new_config)
                    self._send(200, json.dumps(result, ensure_ascii=False))
                except json.JSONDecodeError:
                    self._send(400, json.dumps({'ok': False, 'message': 'JSON 解析失败'}, ensure_ascii=False))
                except Exception as e:
                    self._send(500, json.dumps({'ok': False, 'message': str(e)}, ensure_ascii=False))

        return _Handler
