"""
用户上下文管理模块 - 每个用户的上下文使用不同的文件存储
"""

import os
import json
from typing import List, Dict, Any, Optional


class ContextManager:
    """用户上下文管理器"""
    
    CONTEXT_DIR = "data/user_context"
    MAX_HISTORY = 20  # 默认最大上下文条数
    
    def __init__(self, max_history: int = 20):
        self.MAX_HISTORY = max_history
        self._ensure_context_dir()
    
    def _ensure_context_dir(self):
        """确保上下文目录存在"""
        if not os.path.exists(self.CONTEXT_DIR):
            os.makedirs(self.CONTEXT_DIR)
    
    def _get_context_file_path(self, openid: str) -> str:
        """获取用户上下文文件路径"""
        # 文件名使用用户的OpenID[reference:12]
        return os.path.join(self.CONTEXT_DIR, f"{openid}.json")
    
    def get_context(self, openid: str) -> List[Dict[str, str]]:
        """获取用户的上下文"""
        file_path = self._get_context_file_path(openid)
        if not os.path.exists(file_path):
            return []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('history', [])
        except (json.JSONDecodeError, IOError):
            return []
    
    def add_message(self, openid: str, role: str, content: str):
        """添加一条消息到用户上下文"""
        history = self.get_context(openid)
        history.append({"role": role, "content": content})
        
        # 限制上下文长度
        if len(history) > self.MAX_HISTORY:
            history = history[-self.MAX_HISTORY:]
        
        file_path = self._get_context_file_path(openid)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump({"openid": openid, "history": history}, f, ensure_ascii=False, indent=2)
    
    def clear_context(self, openid: str):
        """清空用户上下文"""
        file_path = self._get_context_file_path(openid)
        if os.path.exists(file_path):
            os.remove(file_path)
    
    def get_messages_for_ai(self, openid: str, system_prompt: str, current_message: str) -> List[Dict[str, str]]:
        """获取用于AI调用的完整消息列表"""
        messages = [{"role": "system", "content": system_prompt}]
        
        # 添加上下文
        history = self.get_context(openid)
        messages.extend(history)
        
        # 添加当前消息
        messages.append({"role": "user", "content": current_message})
        
        return messages