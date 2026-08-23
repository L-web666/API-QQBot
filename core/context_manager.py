"""
用户上下文管理模块 - 每个用户的上下文使用不同的文件存储
"""

import os
import json
import random
import time
from typing import List, Dict, Any, Optional


class ContextManager:
    """用户上下文管理器"""
    
    CONTEXT_DIR = "data/user_context"
    MAX_HISTORY = 20  # 默认最大上下文条数
    BINDINGS_FILE = "bindings.json"  # 身份绑定存储文件名（位于 CONTEXT_DIR 下）
    
    def __init__(self, max_history: int = 20):
        self.MAX_HISTORY = max_history
        self._ensure_context_dir()
        # 身份绑定：群聊成员(member_openid) -> 私聊用户(user_openid)
        self.bindings: Dict[str, str] = {}
        # 待使用的转移码：code -> {user_openid, expires}
        self.pending_codes: Dict[str, Dict[str, Any]] = {}
        self._load_bindings()
    
    def _ensure_context_dir(self):
        """确保上下文目录存在（私聊与群聊分文件夹）"""
        os.makedirs(self.CONTEXT_DIR, exist_ok=True)
        os.makedirs(os.path.join(self.CONTEXT_DIR, 'private'), exist_ok=True)
        os.makedirs(os.path.join(self.CONTEXT_DIR, 'group'), exist_ok=True)
    
    def _get_context_file_path(self, openid: str) -> str:
        """获取上下文文件路径：私聊与群聊存储在不同文件夹中
        - 群聊上下文键 group_<群ID>_<用户ID> -> group/<群ID>_<用户ID>.json
        - 私聊上下文键 <user_openid>        -> private/<user_openid>.json
        """
        if openid.startswith('group_'):
            folder = os.path.join(self.CONTEXT_DIR, 'group')
            name = openid[len('group_'):]
        else:
            folder = os.path.join(self.CONTEXT_DIR, 'private')
            name = openid
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{name}.json")
    
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
    
    def set_context(self, context_key: str, history: List[Dict[str, str]]):
        """直接设置某个上下文的完整历史（用于上下文转移）"""
        if len(history) > self.MAX_HISTORY:
            history = history[-self.MAX_HISTORY:]
        file_path = self._get_context_file_path(context_key)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump({"openid": context_key, "history": history}, f, ensure_ascii=False, indent=2)
    
    # ---------- 私聊↔群聊上下文转移 ----------
    # 说明：QQ 的群聊成员标识(member_openid)与私聊用户标识(user_openid)是两个不同的命名空间，
    # 同一个用户在群聊和私聊中的 id 无法由 API 直接关联，因此转移前需要用户用"转移码"完成一次身份绑定。
    
    def _get_bindings_path(self) -> str:
        return os.path.join(self.CONTEXT_DIR, self.BINDINGS_FILE)
    
    def _load_bindings(self):
        """加载绑定信息（bindings + pending_codes），并清理过期转移码"""
        path = self._get_bindings_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.bindings = data.get('bindings', {}) or {}
            self.pending_codes = data.get('pending_codes', {}) or {}
            now = time.time()
            expired = [c for c, info in self.pending_codes.items()
                       if info.get('expires', 0) < now]
            for c in expired:
                self.pending_codes.pop(c, None)
            if expired:
                self._save_bindings()
        except (json.JSONDecodeError, IOError):
            self.bindings = {}
            self.pending_codes = {}
    
    def _save_bindings(self):
        with open(self._get_bindings_path(), 'w', encoding='utf-8') as f:
            json.dump({
                'bindings': self.bindings,
                'pending_codes': self.pending_codes
            }, f, ensure_ascii=False, indent=2)
    
    def create_transfer_code(self, user_openid: str, ttl: int = 600) -> str:
        """生成6位转移码（默认10分钟有效），用于把私聊上下文绑定到群聊。
        同一用户同时最多只有一个有效码：生成新码时自动作废该用户旧的码，防止堆积。"""
        # 清理已过期的转移码，避免文件无限增长
        now = time.time()
        self.pending_codes = {c: i for c, i in self.pending_codes.items()
                              if i.get('expires', 0) > now}
        # 同一用户只保留最新一个有效码：生成新码时自动作废该用户旧的码
        self.pending_codes = {c: i for c, i in self.pending_codes.items()
                              if i.get('user_openid') != user_openid}
        code = f"{random.randint(0, 999999):06d}"
        # 防重复：与当前仍未使用的转移码冲突时重新生成
        while code in self.pending_codes:
            code = f"{random.randint(0, 999999):06d}"
        self.pending_codes[code] = {
            'user_openid': user_openid,
            'expires': time.time() + ttl
        }
        self._save_bindings()
        return code
    
    def bind_transfer_code(self, member_openid: str, code: str) -> Optional[str]:
        """
        用转移码将群聊成员(member_openid)绑定到私聊用户(user_openid)。
        成功返回 user_openid，码无效/过期返回 None。
        """
        info = self.pending_codes.get(code)
        if not info:
            return None
        if info.get('expires', 0) < time.time():
            self.pending_codes.pop(code, None)
            self._save_bindings()
            return None
        user_openid = info.get('user_openid', '')
        self.pending_codes.pop(code, None)
        if user_openid:
            self.bindings[member_openid] = user_openid
        self._save_bindings()
        return user_openid or None
    
    def get_bound_user_openid(self, member_openid: str) -> Optional[str]:
        """获取群成员已绑定的私聊用户ID（未绑定返回 None）"""
        return self.bindings.get(member_openid)