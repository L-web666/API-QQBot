"""
消息过滤模块 - 关键词匹配、无意义消息过滤
"""

import re
from typing import Dict, List, Tuple, Optional, Any


class MessageFilter:
    """消息过滤器"""
    
    # 无意义消息的正则模式
    MEANINGLESS_PATTERNS = [
        r'^[\d\s]+$',                        # 纯数字
        r'^[\W_]+$',                         # 纯符号/标点
        r'^[a-zA-Z0-9]$',                    # 单个字母或数字（不含单个中文，避免误伤）
        r'^[\s]*$',                          # 空白
        r'^[.。，,、；;：:！!？?…·~\s]+$',    # 纯标点符号
        r'^[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+$',  # 纯表情（覆盖主流emoji区块）
    ]
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.exact_match_keywords = config.get('exact_match_keywords', [])
        self.fuzzy_match_keywords = config.get('fuzzy_match_keywords', [])
        self.exact_match_responses = config.get('exact_match_responses', {})
        self.fuzzy_match_responses = config.get('fuzzy_match_responses', {})
        self.filter_meaningless = config.get('filter_meaningless', True)
    
    def is_meaningless(self, text: str) -> bool:
        """判断消息是否为无意义消息"""
        if not self.filter_meaningless:
            return False
        
        text = text.strip()
        if not text:
            return True

        # 命中任一"无意义"模式即过滤（纯数字、纯符号、单个字母/数字、空白、纯表情等）。
        # 注意：不能提前用"含中文/字母/数字即有意义"短路判断，
        # 否则上面的纯数字、单字符等模式永远无法命中。
        for pattern in self.MEANINGLESS_PATTERNS:
            if re.match(pattern, text, re.UNICODE):
                return True
        
        return False
    
    def match_exact(self, text: str) -> Optional[str]:
        """精确匹配关键词回复（完全一致才触发）"""
        text = text.strip()
        for keyword in self.exact_match_keywords:
            if text == keyword:
                return self.exact_match_responses.get(keyword)
        return None
    
    def match_fuzzy(self, text: str) -> Optional[str]:
        """模糊匹配关键词回复（包含即触发）"""
        text = text.strip().lower()
        for keyword in self.fuzzy_match_keywords:
            if keyword.lower() in text:
                return self.fuzzy_match_responses.get(keyword)
        return None
    
    def match_keyword(self, text: str) -> Optional[str]:
        """匹配关键词（先精确后模糊）"""
        # 精确匹配
        response = self.match_exact(text)
        if response is not None:
            return response
        
        # 模糊匹配
        response = self.match_fuzzy(text)
        if response is not None:
            return response
        
        return None
    
    def filter_mention(self, text: str) -> str:
        """过滤掉@机器人的名称，只保留用户发送的消息"""
        # QQ机器人API的content字段已自动去除@机器人的前缀[reference:27]
        # 这里做一些额外的清理
        text = re.sub(r'@[^\s]+', '', text)
        return text.strip()
    
    def should_reply_in_group(self, is_mentioned: bool, require_mention: bool) -> bool:
        """判断群聊中是否应该回复"""
        if require_mention:
            return is_mentioned
        return True