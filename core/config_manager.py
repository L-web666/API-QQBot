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
        "api_key": "请填写你的API密钥",
        "base_url": "请填写API基础地址（如 https://api.openai.com/v1）",
        "model": "请填写模型名称（如 gpt-3.5-turbo）",
        # 语音识别（ASR）配置：留空则语音消息无法转文字；填了才能识别语音
        # 使用 OpenAI 兼容的 /audio/transcriptions 接口（如 OpenAI Whisper、Groq 等）
        "asr_base_url": "",     # 语音识别服务地址，留空=用上面的 base_url；一般也留空即可
        "asr_api_key": "",      # 语音识别密钥，留空=用上面的 api_key
        "asr_model": "whisper-1",  # 语音识别模型名
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
            "max_size_mb": 10                 # 单个日志文件最大大小（MB）
        },
        # 配置热更新
        "hot_reload": {
            "enabled": True,                  # 是否启用配置热更新（编辑 config.json 保存后自动生效）
            "interval_seconds": 10            # 检查配置文件变化的间隔（秒）
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
  asr_base_url       : 语音识别服务地址（可选，留空=用 base_url）
  asr_api_key        : 语音识别密钥（可选，留空=用 api_key）
  asr_model          : 语音识别模型（如 whisper-1）

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

【配置热更新】hot_reload

  enabled            : True=启用热更新，编辑 config.json 保存后自动生效（无需重启）
  interval_seconds   : 检查配置文件变化的间隔（秒，默认10）

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
        """获取AI服务配置（简化版，含语音识别参数）"""
        return {
            "api_key": self.config.get("api_key", ""),
            "base_url": self.config.get("base_url", ""),
            "model": self.config.get("model", ""),
            "asr_base_url": self.config.get("asr_base_url", ""),
            "asr_api_key": self.config.get("asr_api_key", ""),
            "asr_model": self.config.get("asr_model", "whisper-1"),
        }