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
        # 上下文配置
        "context": {
            "max_history": 20,                # 每个用户最大上下文条数
            "enabled": True                   # 是否启用上下文
        },
        # 日志配置
        "log": {
            "max_size_mb": 10                 # 单个日志文件最大大小（MB）
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

【上下文配置】context

  max_history        : 每个用户最多保存的上下文条数
  enabled            : True=启用上下文记忆，False=不启用

【日志配置】log

  max_size_mb        : 单个日志文件最大大小（MB），超过自动分割（整数，默认10）

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
        """获取AI服务配置（简化版）"""
        return {
            "api_key": self.config.get("api_key", ""),
            "base_url": self.config.get("base_url", ""),
            "model": self.config.get("model", "")
        }