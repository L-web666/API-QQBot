"""
消息处理模块 - 排队、分段发送、@处理、过滤思考过程
"""

import threading
import queue
import time
from typing import Optional, Callable, Dict, Any, List


class MessageProcessor:
    """消息处理器 - 支持排队、分段发送"""
    
    def __init__(self, qq_client, ai_client, context_manager, message_filter,
                file_handler, logger=None, config: Dict[str, Any] = None):
        self.qq_client = qq_client
        self.ai_client = ai_client
        self.context_manager = context_manager
        self.message_filter = message_filter
        self.file_handler = file_handler
        self.logger = logger
        self.config = config or {}
        
        # 将 file_handler 注入到 ai_client
        self.ai_client.file_handler = file_handler

        self.max_queue_size = self.config.get('max_queue_size', 10)
        self.max_segment_length = self.config.get('max_segment_length', 2000)
        self.system_prompt = self.config.get('system_prompt', '你是一个智能助手。')
        self.require_mention = self.config.get('require_mention', True)
        self.reply_with_mention = self.config.get('reply_with_mention', True)
        self.context_enabled = self.config.get('context_enabled', True)
        
        self._task_queue = queue.Queue()
        self._processing = False
        self._worker_thread = None
    
    def start(self):
        """启动处理线程"""
        if self._processing:
            return
        
        self._processing = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        if self.logger:
            self.logger.info("消息处理器已启动")
    
    def stop(self):
        """停止处理"""
        self._processing = False
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
        if self.logger:
            self.logger.info("消息处理器已停止")
    
    def submit(self, message: Dict[str, Any]):
        """提交消息到处理队列"""
        try:
            self._task_queue.put(message, timeout=1)
            if self.logger:
                self.logger.debug(f"消息已入队，当前队列大小: {self._task_queue.qsize()}")
        except queue.Full:
            if self.logger:
                self.logger.warning("队列已满，消息被丢弃")
            # 无法回复用户，因为没有用户信息直接发送
    
    def _worker_loop(self):
        """工作线程主循环"""
        while self._processing:
            try:
                message = self._task_queue.get(timeout=1)
                self._process_message(message)
            except queue.Empty:
                continue
            except Exception as e:
                if self.logger:
                    self.logger.error(f"处理消息异常: {e}")
                # _process_message 内部已捕获所有异常，此处的异常理论上不会发生
    
    def _process_message(self, message: Dict[str, Any]):
        """处理单条消息，包含全局异常捕获以回复用户"""
        try:
            # 原有处理逻辑
            msg_type = message.get('type', 'c2c')
            content = message.get('content', '').strip()
            user_openid = message.get('user_openid', '')
            user_name = message.get('user_name', '用户')
            msg_id = message.get('msg_id', '')
            attachments = message.get('attachments', [])
            group_openid = message.get('group_openid', '')
            channel_id = message.get('channel_id', '')
            
            # 群聊特殊处理
            if msg_type == 'group':
                if self.require_mention:
                    # 已经是@消息事件，直接通过
                    pass
            
            # ====== 改进1：有附件时不过滤无意义消息 ======
            has_attachments = bool(attachments)
            if not has_attachments and self.message_filter.is_meaningless(content):
                if self.logger:
                    self.logger.debug(f"过滤无意义消息: {content}")
                return

            # ===== 新增：清空上下文指令 =====
            clear_commands = ["/clear", "/清空上下文", "/重置对话"]
            if content.strip() in clear_commands:
                if self.context_enabled:
                    self.context_manager.clear_context(user_openid)
                    self._send_reply(msg_type, user_openid, user_name, group_openid, 
                                    channel_id, msg_id, "✅ 已清空您的对话历史。")
                else:
                    self._send_reply(msg_type, user_openid, user_name, group_openid, 
                                    channel_id, msg_id, "⚠️ 上下文功能未启用。")
                return
            
            # 关键词匹配（优先处理，避免浪费token）
            keyword_response = self.message_filter.match_keyword(content)
            if keyword_response:
                self._send_reply(msg_type, user_openid, user_name, group_openid, 
                               channel_id, msg_id, keyword_response)
                return
            
            # 处理附件
            file_infos = []
            if attachments:
                file_infos = self.file_handler.process_attachments(attachments)
                if self.logger:
                    self.logger.debug(f"处理了 {len(file_infos)} 个附件")
                
                # ====== 改进2：纯图片消息自动补提示 ======
                if not content:
                    content = "请描述这张图片。"
                    message['content'] = content  # 更新，以便上下文存储使用
            
            # 获取上下文并调用AI
            if self.context_enabled:
                messages = self.context_manager.get_messages_for_ai(
                    user_openid, self.system_prompt, content
                )
            else:
                messages = [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": content}
                ]
            
            if self.logger:
                self.logger.info(f"调用AI处理用户 {user_openid} 的消息")
            
            # 使用带文件的多模态调用
            ai_response = self.ai_client.chat_with_files(messages, file_infos)
            
            # ====== 改进3：AI返回为空或错误时，统一处理 ======
            if ai_response is None:
                ai_response = "AI服务暂时不可用，请稍后再试。"
            # 如果已经是错误提示（以"AI服务"开头），不再过滤思考过程
            elif not ai_response.startswith("AI服务"):
                # 过滤思考过程
                ai_response = self.ai_client.filter_thinking(ai_response)
                if not ai_response:
                    ai_response = "抱歉，我无法生成合适的回复。"
            
            # ====== 改进4：仅当回复不是错误信息时才保存上下文 ======
            if self.context_enabled and ai_response and not ai_response.startswith("AI服务"):
                self.context_manager.add_message(user_openid, 'user', content)
                self.context_manager.add_message(user_openid, 'assistant', ai_response)
            
            # 发送回复（即使是错误信息也发送给用户）
            self._send_reply(msg_type, user_openid, user_name, group_openid, 
                           channel_id, msg_id, ai_response)
        except Exception as e:
            # 捕获所有未预期的异常，记录并发送友好信息
            if self.logger:
                self.logger.error(f"处理消息时发生未预期错误: {e}", exc_info=True)
            # 尝试获取必要信息发送错误提示
            try:
                user_openid = message.get('user_openid', '')
                user_name = message.get('user_name', '用户')
                msg_type = message.get('type', 'c2c')
                group_openid = message.get('group_openid', '')
                channel_id = message.get('channel_id', '')
                msg_id = message.get('msg_id', '')
                error_msg = "⚠️ 系统暂时遇到了问题，请稍后再试。"
                self._send_reply(msg_type, user_openid, user_name, group_openid,
                                channel_id, msg_id, error_msg)
            except Exception as send_err:
                if self.logger:
                    self.logger.error(f"发送错误回复时失败: {send_err}")
    
    def _send_reply(self, msg_type: str, user_openid: str, user_name: str,
                    group_openid: str, channel_id: str, msg_id: str, content: str):
        """发送回复（支持分段）"""
        # 分段发送
        segments = self._split_message(content, self.max_segment_length)
        
        for i, segment in enumerate(segments):
            # 在群聊或频道中，回复开头加上@用户昵称
            if msg_type in ['group', 'channel'] and self.reply_with_mention:
                if i == 0:  # 只在第一段添加@
                    segment = f"@{user_name} {segment}"
            
            # 发送消息
            if msg_type == 'c2c':
                self.qq_client.send_message(user_openid, segment, msg_id=msg_id if i == 0 else None)
            elif msg_type == 'group':
                self.qq_client.send_group_message(group_openid, segment, msg_id=msg_id if i == 0 else None)
            elif msg_type == 'channel':
                self.qq_client.send_channel_message(channel_id, segment, msg_id=msg_id if i == 0 else None)
            
            # 分段之间稍作延迟，避免频控
            if len(segments) > 1:
                time.sleep(0.5)
        
        if self.logger:
            self.logger.info(f"回复已发送 ({len(segments)}段): {content[:50]}...")
    
    def _split_message(self, text: str, max_length: int) -> List[str]:
        """将长文本分段"""
        if len(text) <= max_length:
            return [text]
        
        segments = []
        lines = text.split('\n')
        current = ''
        
        for line in lines:
            if len(current) + len(line) + 1 <= max_length:
                current += line + '\n' if current else line + '\n'
            else:
                if current:
                    segments.append(current.strip())
                # 如果单行太长，强制分割
                if len(line) > max_length:
                    for i in range(0, len(line), max_length):
                        segments.append(line[i:i+max_length])
                    current = ''
                else:
                    current = line + '\n'
        
        if current:
            segments.append(current.strip())
        
        return segments if segments else [text]