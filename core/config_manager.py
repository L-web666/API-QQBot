"""
配置管理模块 - 负责config.json的读取、写入和初次创建
"""

import json
import os
from typing import Any, Dict


class ConfigManager:
    """配置管理器"""
    
    CONFIG_FILE = "config.json"
    CONFIG_DOC_FILE = "config配置说明文件.txt"
    
    # 默认配置模板（简化AI配置）
    DEFAULT_CONFIG = {
        # QQ机器人配置
        "qq": {
            "app_id": "请填写你的APPID",
            "app_secret": "请填写你的APPSECRET",
            "sandbox": False,
            "reconnect_attempts": 5,
            "reconnect_interval": 10
        },
        # AI服务配置（简化）
        # 说明：这三项可以留空/不填——程序照样能启动，插件、关键词回复、内置指令都不受影响；
        #       只有"没有被插件接管"的普通消息才会因为缺少 AI 而无法回答。
        "api_key": "请填写你的API密钥",
        "base_url": "请填写API基础地址（如 https://api.openai.com/v1）",
        "model": "请填写模型名称（如 gpt-3.5-turbo）",
        # 未配置 AI 时给用户的兜底回复：留空=不回复（适合由插件完全接管回复的场景）
        "no_ai_reply": "抱歉，我暂时没办法回答这个问题。",
        # 内置 AI 总开关：false = 即使填了 API Key 也不用内置 AI，
        # 普通消息完全交给插件/关键词回复处理（适合用 Ollama 等插件接管回复）
        "ai": {
            "enabled": True
        },
        # AI全局人设
        "system_prompt": "你是一个智能、友好的AI助手，请用中文回复用户的问题。",
        # 消息过滤配置
        "filter": {
            "exact_match_keywords": [],      # 精确匹配关键词列表
            "fuzzy_match_keywords": [],      # 模糊匹配关键词列表
            "exact_match_responses": {},     # 精确匹配回复映射
            "fuzzy_match_responses": {}      # 模糊匹配回复映射
        },
        # 群聊配置
        "group": {
            "require_mention": True           # 群聊是否只有被@时才回复
        },
        # 消息处理配置
        "message": {
            "max_segment_length": 2000,       # 单条消息最大长度（超过则分段）
            "max_queue_size": 10,             # 最大排队数
            "filter_meaningless": True        # 是否过滤无意义消息
        },
        # 回复限速：同一用户短时间内频繁提问自动降频
        "rate_limit": {
            "enabled": True,                  # 是否启用回复限速
            "interval_seconds": 3             # 同一用户两次AI回复的最小间隔（秒）
        },
        # 敏感词过滤：命中敏感词的消息/回复会被替换或拦截
        "sensitive_words": {
            "enabled": False,                 # 是否启用敏感词过滤
            "list": [],                       # 敏感词列表（每项一个词，支持子串匹配）
            "replacement": "***",             # 替换符号
            "block_input": False              # True=用户消息含敏感词直接不回复；False=仅把词打码后正常回复
        },
        # 上下文配置
        "context": {
            "max_history": 20,                # 每个用户最大上下文条数
            "enabled": True                   # 是否启用上下文
        },
        # 日志配置
        "log": {
            "max_size_mb": 10,                # 单个日志文件最大大小（MB）
            "console_color": True             # 控制台彩色输出（时间灰、WARNING黄、ERROR红）；日志文件始终无颜色
        },
        # 配置热更新
        "hot_reload": {
            "enabled": True,                  # 是否启用配置热更新（编辑 config.json 保存后自动生效）
            "interval_seconds": 10            # 检查配置文件变化的间隔（秒）
        },
        # 云同步（可选）：把 data/ 下的用户数据同步到 Cloudflare D1，换服务器不丢数据
        # 绝不上云：config.json（含密钥）、bot.lock / bot.pid；日志默认也不上云
        "cloud_sync": {
            "enabled": False,                 # 是否启用云同步
            "provider": "cloudflare_d1",      # 目前支持 Cloudflare D1
            "account_id": "",                 # Cloudflare 账户 ID
            "database_id": "",                # D1 数据库 ID
            "api_token": "",                  # Cloudflare API 令牌（需 D1 编辑权限）
            "interval_seconds": 60,           # 后台同步间隔（秒）
            "pull_on_start": True,            # 启动时先把云端数据拉回本地
            "upload_logs": False,             # 日志是否也上云（量大，默认关闭）
            "max_file_mb": 2,                 # 单文件超过此大小不同步
            "tombstone_days": 30,             # 删除标记（墓碑）保留天数，0=永久保留
            "apply_remote_deletes": True,     # 云端删除标记比本地新时，同步删除本地文件
            "error_pause_minutes": 30,        # 同一文件连续失败3次→暂停同步多久（0=需手动恢复）
            # 启动写缓存：第一次云同步完成前，data/ 下的改动先缓存在内存里，等云端数据
            # 拉下来之后再落盘（避免刚启动时写的旧数据/默认值把云端数据覆盖掉）
            "startup_buffer": True,           # 是否启用启动写缓存（不启用云同步时本项无意义）
            "startup_buffer_minutes": 3,      # 等待首次同步的上限（分钟），超时先落盘；0=一直等
            "startup_buffer_max_mb": 8,       # 缓存容量上限（MB），超过就立刻落盘；0=不限制
            "startup_buffer_keep_remote": True  # 冲突时以云端为准：刚被首次同步恢复的文件，放弃启动期间的本地修改
        },
        # 管理员与告警
        "admin": {
            "openids": []                     # 管理员用户 openid 列表（可多个）
        },
        "alert": {
            "enabled": True,                  # 出错时是否私聊通知主人
            "owner_openid": ""                # 主人 openid（接收告警）
        },
        # 定时任务（每日推送）
        "schedule": {
            "enabled": True,                  # 是否启用定时任务
            "tasks": []                       # 任务列表：[{"time":"08:00","target_type":"group","target_id":"群ID","content":"早安"}]
        },
        # Web 管理后台（轻量只读面板）
        "web_admin": {
            "enabled": True,                  # 是否启用
            "host": "127.0.0.1",              # 监听地址（本机访问用 127.0.0.1，手机访问用 0.0.0.0）
            "port": 8666,                     # 端口
            "token": ""                       # 访问令牌（留空则不校验，仅建议本机使用）
        },
        # 指令面板（QQ 聊天界面中用户可见的指令列表）
        "command_panel": {
            "enabled": True,                  # 是否向 QQ 注册指令面板
            "commands": [                     # 指令列表（type=command/link，name=名称，desc/link=描述或链接）
                {"type": "command", "name": "/帮助", "desc": "显示指令帮助"},
                {"type": "command", "name": "/clear", "desc": "清空当前场景对话历史"},
                {"type": "command", "name": "/转移私聊到群聊", "desc": "把私聊上下文合并到当前群"},
                {"type": "command", "name": "/生成转移码", "desc": "生成身份绑定码（私聊）"},
                {"type": "command", "name": "/绑定转移码", "desc": "绑定身份并转移上下文（群聊）"}
            ],
            "c2c": {                          # 私聊面板
                "target_type": "all",         # all=所有用户私聊可见；specific=仅指定用户
                "user_openids": [],           # target_type=specific 时填用户 openid
                "remark": "QQ AI Bot 私聊指令面板"
            },
            "group": {                        # 群聊面板
                "target_type": "specific",    # all=所有群可见；specific=仅指定群
                "group_openids": [],          # target_type=specific 时填群 openid
                "remark": "QQ AI Bot 群聊指令面板"
            }
        },
        # 插件系统：把 .py 插件文件放进 plugins/ 目录即可扩展功能，无需改代码
        "plugins": {
            "enabled": True,                  # 是否加载插件
            "dir": "plugins"                  # 插件目录（相对程序所在目录）
        }
    }
    
    # 配置说明文档内容（更新AI部分）
    CONFIG_DOC = """
═══════════════════════════════════════════════════════════════
                    QQ AI Bot 配置文件说明
═══════════════════════════════════════════════════════════════

【QQ机器人配置】qq

  app_id             : 机器人的AppID，在QQ开放平台获取
  app_secret         : 机器人的AppSecret，在QQ开放平台获取
  sandbox            : 是否使用沙箱环境（True/False）
  reconnect_attempts : WebSocket断开后的最大重连尝试次数（整数，默认5）
  reconnect_interval : 每次重连之间的等待间隔（秒，整数，默认10）

【AI服务配置】（顶层字段）

  api_key            : 你的API密钥
  base_url           : API基础地址（如 https://api.openai.com/v1）
  model              : 使用的模型名称（如 gpt-3.5-turbo）

  以上三项均为可选：不填也能正常启动（插件/关键词回复/内置指令照常工作），
  只是"没被插件接管"的普通消息无法回答，会使用 no_ai_reply 作为兜底回复。

  no_ai_reply        : 未配置 AI 时给用户的兜底回复；留空=不回复

【内置 AI 总开关】ai.enabled

  true （默认）: 正常使用内置 AI（需填好上面三项）
  false        : 即使填了 API Key 也不使用内置 AI，普通消息完全交给插件/关键词回复

  用途：用本地 Ollama 等插件接管回复时，把它设为 false 就能保证内置 AI 不会参与回复
  （插件本身也可能自带 FORCE 开关，两者互不影响）。

【AI全局人设】system_prompt

  设置AI的全局人设，所有对话都会使用这个系统提示词。

【消息过滤配置】filter

  exact_match_keywords     : 精确匹配关键词列表，完全一致才触发
  fuzzy_match_keywords     : 模糊匹配关键词列表，包含即触发
  exact_match_responses    : 精确匹配对应的回复（键为关键词，值为回复内容）
  fuzzy_match_responses    : 模糊匹配对应的回复

  填写示例：
  "filter": {
    "exact_match_keywords": ["关键词1", "关键词2", ...],
    "fuzzy_match_keywords": ["关键词A", "关键词B", ...],
    "exact_match_responses": {
        "关键词1": "回复内容1",
        "关键词2": "回复内容2"
    },
    "fuzzy_match_responses": {
        "关键词A": "回复内容A",
        "关键词B": "回复内容B"
    }
  }

【群聊配置】group

  require_mention    : True=只有被@时才回复，False=回复所有群消息

【消息处理配置】message

  max_segment_length : AI回复超过此长度时自动分段（字符数）
  max_queue_size     : 最大排队请求数，超过则拒绝新请求
  filter_meaningless : True=过滤无意义消息（纯数字、符号、表情等）

【回复限速】rate_limit

  enabled            : True=启用回复限速，防止同一用户刷屏
  interval_seconds   : 同一用户两次AI回复的最小间隔（秒，默认3）

【敏感词过滤】sensitive_words

  enabled            : True=启用敏感词过滤
  list               : 敏感词列表（每项一个词，消息或回复中包含即命中）
  replacement        : 命中后替换成的符号（默认 ***）
  block_input        : True=用户消息含敏感词直接不回复；False=仅打码后正常回复

【上下文配置】context

  max_history        : 每个用户最多保存的上下文条数
  enabled            : True=启用上下文记忆，False=不启用

【日志配置】log

  max_size_mb        : 单个日志文件最大大小（MB），超过自动分割（整数，默认10）
  console_color      : True=控制台彩色输出（时间灰色、WARNING 黄色、ERROR 红色，正文不着色）
                       False=纯文本；日志文件永远不带颜色（避免出现 ANSI 乱码）
                       仅在终端下生效：重定向到文件/管道或无控制台时自动不上色
                       也可用环境变量控制：NO_COLOR=1 强制关闭、FORCE_COLOR=1 强制开启

【配置热更新】hot_reload

  enabled            : True=启用热更新，编辑 config.json 保存后自动生效（无需重启）
  interval_seconds   : 检查配置文件变化的间隔（秒，默认10）

【云同步】cloud_sync（可选）

  把 data/ 下的用户数据同步到 Cloudflare D1 云数据库，机器人换服务器后可把数据拉回来。

  enabled            : True=启用云同步（默认 False）
  provider           : cloudflare_d1（目前支持 Cloudflare D1）
  account_id         : Cloudflare 账户 ID（控制台右侧 / URL 里可见）
  database_id        : D1 数据库 ID（创建 D1 后可见）
  api_token          : Cloudflare API 令牌，权限需要「D1 → 编辑」
  interval_seconds   : 后台多久写入一次云数据库（秒，默认60）
                       填 300 就是每 5 分钟一次；间隔 ≥60 秒时会对齐到整点倍数
                       （300 秒 → 每小时 00/05/10… 分整），每个周期只写有改动的文件
  pull_on_start      : True=启动时先把云端数据拉回本地（换服务器就靠这个恢复数据）
  upload_logs        : 日志是否也上云（日志量大，默认 False）
  max_file_mb        : 单个文件超过此大小不同步（默认2MB）
  tombstone_days     : 删除标记（墓碑）保留天数，默认30；0=永久保留
                       删除也会作为一条带时间戳的记录写入云端，其它服务器/新机器据此
                       删除本地副本，且不会把已删除的数据"复活"；过期标记会自动清理
  apply_remote_deletes : True（默认）=云端删除标记比本地文件新时，同步删除本地文件
                       False=只记录不删本地（本地数据永不因云端删除而消失）
  error_pause_minutes: 同一个文件连续失败 3 次（上传/下载/写入/删除）时：
                       升级为 ERROR 并暂停云同步，单位分钟（默认30，0=只能手动恢复）
                       暂停期间不再访问云端；到期自动恢复并重置失败计数；
                       后台「⬆️ 立即同步一次」可强制重试（成功即恢复）

  startup_buffer     : True（默认）=启用「启动写缓存」。程序刚启动到第一次云同步完成
                       这段时间里，对 data/ 下数据文件的改动（写入/删除/改名）先记在
                       内存里，等第一次同步把云端数据拉下来之后再写回磁盘。
                       为什么需要：插件/核心模块启动时往往会立刻写数据文件（补默认值、
                       整理格式），此时本地数据可能比云端旧，这些写入一旦先落盘，
                       云同步就会认为本地更新 → 反而把云端数据覆盖掉。
                       只影响会被同步的数据文件：日志、config.json 等不受影响。
  startup_buffer_minutes : 等待首次同步的上限（分钟，默认3）。超时就把缓存的内容先落盘，
                       避免数据一直不写；0=一直等到同步完成（不推荐，云端不通时会一直等）
                       另外：云同步进入暂停状态、程序退出时，缓存内容都会立即落盘。
  startup_buffer_max_mb : 缓存容量上限（MB，默认8）。机器人很忙时缓存可能攒下不少内容，
                       超过上限就立刻把已缓存的内容写回磁盘并停止缓存（之后的改动直接写磁盘），
                       避免无限占内存；0=不限制（仍有 128MB 兜底上限）。
  startup_buffer_keep_remote : True（默认）=冲突时以云端为准：某个文件刚被第一次同步
                       从云端恢复（说明云端版本更新），就放弃启动期间对它的本地修改，
                       并在控制台 WARNING 里列出来；False=本地修改优先（照旧覆盖云端）
                       注意：不启用云同步（enabled=false 或凭据不完整）时，以上三项都不生效，
                       程序行为与没有这个功能时完全一样，也不会有任何云同步相关提示。

  同步内容：user_context/**（上下文+身份绑定）、stats.json、plugins_data/**、
            plugins_disabled.json、command_panel.json
  永不上云：config.json（含 AppSecret/API Key）、config配置说明文件.txt、
            data/bot.lock、data/bot.pid；日志在 upload_logs=false 时也不上云

  删除机制（墓碑 tombstone）：
    删除某个上下文（如 /clear）时，程序不是把云端记录删掉，而是写入一条"删除标记"
    （key + 删除时间 + deleted=1）。其它服务器/新机器看到标记后会：
      · 不再下载该文件（不会"复活"）
      · 若本地副本更旧 → 同步删除本地副本（apply_remote_deletes=true 时）
      · 若本地副本更新（删除后又改过）→ 保留并重新上传，覆盖掉标记
    标记默认保留 30 天（tombstone_days），过期自动清理。

  冲突规则（同一条记录，以时间戳更新的一方为准）：
    · 云端副本更新（云端时间 > 本地文件修改时间）→ 用云端版本覆盖本地，
      并且本轮**不上传**本地副本（避免本地旧数据把云端新数据覆盖掉），
      控制台会 WARNING 提示"有 N 个文件的云端版本比本地新…"
    · 本地副本更新 → 上传，覆盖云端
    · 时间戳相同（同一毫秒）→ 以本地为准
    判断依据是文件修改时间（毫秒级），所以请保证各台机器系统时间准确（建议开启自动校时）。

  注意：同一时间只应运行一台机器人（云数据库按 key 覆盖，多台会互相打架）。

  可热更新的项：api_key / base_url / model、system_prompt、关键词过滤、
  max_segment_length、context(enabled/max_history)、require_mention、filter_meaningless、
  rate_limit、sensitive_words
  注意：app_id / app_secret / sandbox / 日志配置等连接类配置修改后仍需重启生效。

【管理员与告警】admin / alert

  admin.openids      : 管理员用户 openid 列表（用于指令权限判断）
  alert.enabled      : 出错时是否私聊通知主人
  alert.owner_openid : 主人 openid（接收告警，如连接失败提醒）

【定时任务】schedule

  enabled : 是否启用定时任务
  tasks   : 任务列表，格式：
    [
      {"time": "08:00", "target_type": "group", "target_id": "群openid", "content": "早安！"},
      {"time": "12:30", "target_type": "c2c",   "target_id": "用户openid", "content": "记得吃午饭"}
    ]
  time 为北京时间 HH:MM，到点自动发送 content（目前为固定文本）。

【Web 管理后台】web_admin

  enabled : 是否启用（轻量只读面板：状态/配置/日志/上下文）
  host    : 监听地址（本机用 127.0.0.1；手机访问同一局域网时用 0.0.0.0）
  port    : 端口（默认 8666）
  token   : 访问令牌（URL 加 ?token=xxx 访问；留空则不校验，建议仅本机使用）

【指令面板】command_panel

  enabled  : 是否向 QQ 注册指令面板（用户在聊天界面可见/可点的指令列表）
  commands : 指令列表，格式：
    [
      {"type": "command", "name": "/帮助", "desc": "显示指令帮助"},
      {"type": "link", "name": "官网", "link": "https://example.com"}
    ]
    type=command 时填 desc（描述）；type=link 时填 link（链接地址）
  c2c      : 私聊面板。target_type: all=所有用户私聊可见；specific=仅指定用户（填 user_openids）
  group    : 群聊面板。target_type: all=所有群可见；specific=仅指定群（填 group_openids）
  remark   : 面板备注（可选）

  官方接口：创建 POST /v2/panels（请求体含 scope/target_type/group_openids/panel，
  其中 panel.items 每项为 {"type":"command","name":..,"desc":..} 或 {"type":"link","name":..,"link":..}），
  修改 PUT /v2/panels/{panel_id}，查询 GET /v2/panels/{panel_id}。
  面板 ID 保存在 data/command_panel.json（c2c/group 各一个），首次创建后后续为更新；
  接口失败不影响机器人其它功能。

【插件系统】plugins

  enabled            : True=加载插件，False=禁用全部插件
  dir                : 插件目录（默认 plugins，相对程序所在目录）

  插件使用说明：把 .py 文件放进 plugins/ 目录即可，无需改任何源代码。
  插件格式见 plugins/README.md（或 Web 后台「插件」页）。

═══════════════════════════════════════════════════════════════
"""
    
    def __init__(self):
        self.config = {}
        self._ensure_config_exists()
        self.load()
    
    def _ensure_config_exists(self):
        """确保配置文件存在，不存在则创建"""
        if not os.path.exists(self.CONFIG_FILE):
            with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        
        # 创建配置说明文档
        if not os.path.exists(self.CONFIG_DOC_FILE):
            with open(self.CONFIG_DOC_FILE, 'w', encoding='utf-8') as f:
                f.write(self.CONFIG_DOC)
    
    def load(self) -> Dict[str, Any]:
        """加载配置文件"""
        with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        return self.config
    
    def reload(self) -> Dict[str, Any]:
        """重新加载配置文件（配置热更新用）"""
        with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        return self.config
    
    def save(self):
        """保存配置文件"""
        with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)
    
    def get(self, key: str, default=None) -> Any:
        """获取配置项（支持点号分隔的嵌套键）"""
        keys = key.split('.')
        value = self.config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value
    
    def set(self, key: str, value: Any):
        """设置配置项（支持点号分隔的嵌套键）"""
        keys = key.split('.')
        config = self.config
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value
        self.save()
    
    def get_ai_config(self) -> Dict[str, str]:
        """获取AI服务配置"""
        return {
            "api_key": self.config.get("api_key", ""),
            "base_url": self.config.get("base_url", ""),
            "model": self.config.get("model", ""),
        }