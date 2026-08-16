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
        self.context_enabled = self.config.get('context_enabled', True)
        
        # 有界队列：达到 max_queue_size 后新消息被拒绝（返回繁忙提示），防止无限积压
        self._task_queue = queue.Queue(maxsize=self.max_queue_size)
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
        """提交消息到处理队列；队列满时给发送者返回"繁忙"提示"""
        try:
            self._task_queue.put(message, timeout=1)
            if self.logger:
                self.logger.debug(f"消息已入队，当前队列大小: {self._task_queue.qsize()}")
        except queue.Full:
            if self.logger:
                self.logger.warning("队列已满，向用户发送繁忙提示")
            # 队列满：在新线程中回复"机器人忙"，避免阻塞WebSocket接收线程
            threading.Thread(target=self._send_busy_reply, args=(message,), daemon=True).start()
    
    def _send_busy_reply(self, message: Dict[str, Any]):
        """队列满时向发送者回复繁忙提示"""
        try:
            self._send_reply(
                message.get('type', 'c2c'),
                message.get('user_openid', ''),
                message.get('user_name', '用户'),
                message.get('group_openid', ''),
                message.get('channel_id', ''),
                message.get('msg_id', ''),
                "🤖 机器人正在处理其他消息，请稍后再试。"
            )
        except Exception as e:
            if self.logger:
                self.logger.error(f"发送繁忙提示失败: {e}")
    
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
            
            # 群聊特殊处理：当前订阅的是 GROUP_AT_MESSAGE_CREATE 事件，
            # 只有被@的消息才会推送到这里，因此 require_mention=True 天然成立。
            # 若要支持 require_mention=False（回复所有群消息），需在开放平台
            # 开通"群消息全量"权限并额外订阅对应事件后，再在这里处理。
            
            # ====== 上下文键：私聊与群聊分开存储（不同文件夹） ======
            # 统一身份标识：新版QQ平台群聊/私聊的 openid 一致（事件 author.user_openid），
            # 群聊事件取不到 user_openid 时回退到群成员标识 member_openid
            unified_id = message.get('unified_openid') or user_openid
            # 群聊：按 群ID+用户ID 区分（每个群的上下文相互独立）
            if msg_type == 'group':
                context_key = f"group_{group_openid}_{unified_id}"
            else:
                # 私聊/频道私信：按用户ID存储（private/ 文件夹）
                context_key = unified_id
            
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
                    self.context_manager.clear_context(context_key)
                    self._send_reply(msg_type, user_openid, user_name, group_openid, 
                                    channel_id, msg_id, "✅ 已清空您的对话历史。")
                else:
                    self._send_reply(msg_type, user_openid, user_name, group_openid, 
                                    channel_id, msg_id, "⚠️ 上下文功能未启用。")
                return
            
            # ===== 新增：把私聊上下文转移到当前群聊 =====
            # 主命令：/转移私聊到群聊（8字符）；保留旧命令作为别名
            transfer_commands = ["/转移私聊到群聊", "/将我的私聊上下文转移到当前群聊", "/转移私聊上下文", "/导入私聊上下文"]
            if msg_type == 'group' and content.strip() in transfer_commands:
                if not self.context_enabled:
                    self._send_reply(msg_type, user_openid, user_name, group_openid,
                                    channel_id, msg_id, "⚠️ 上下文功能未启用。")
                    return
                self._transfer_private_context(msg_type, user_openid, user_name,
                                               group_openid, channel_id, msg_id,
                                               unified_id, context_key)
                return
            
            # ===== 新增：用转移码绑定身份（绑定成功后立即转移） =====
            if msg_type == 'group' and content.strip().startswith("/绑定转移码"):
                if not self.context_enabled:
                    self._send_reply(msg_type, user_openid, user_name, group_openid,
                                    channel_id, msg_id, "⚠️ 上下文功能未启用。")
                    return
                self._bind_and_transfer(msg_type, user_openid, user_name,
                                        group_openid, channel_id, msg_id, content, context_key)
                return
            
            # ===== 新增：私聊中生成转移码 =====
            if msg_type == 'c2c' and content.strip() == "/生成转移码":
                if not self.context_enabled:
                    self._send_reply(msg_type, user_openid, user_name, group_openid,
                                    channel_id, msg_id, "⚠️ 上下文功能未启用。")
                    return
                code = self.context_manager.create_transfer_code(user_openid)
                self._send_reply(msg_type, user_openid, user_name, group_openid,
                                channel_id, msg_id,
                                f"🔑 您的转移码：{code}（10分钟内有效）。\n"
                                f"请在目标群聊发送：/绑定转移码 {code}")
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
                    context_key, self.system_prompt, content
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
                self.context_manager.add_message(context_key, 'user', content)
                self.context_manager.add_message(context_key, 'assistant', ai_response)
            
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
    
    def _merge_private_into_group(self, group_context_key: str,
                                  private_history: List[Dict[str, str]]) -> Optional[int]:
        """
        将私聊历史合并到群聊上下文：保留群聊原有上下文，只追加群聊中尚不存在的私聊记录。
        （按 role+content 去重，重复转移或部分新增时不会产生重复记录）
        返回 (新增条数, 合并后总条数)；没有新内容可追加（已合并过）返回 None。
        """
        existing = self.context_manager.get_context(group_context_key)
        existing_keys = {(m.get('role'), m.get('content')) for m in existing}
        new_entries = [m for m in private_history
                       if (m.get('role'), m.get('content')) not in existing_keys]
        if not new_entries:
            return None
        merged = existing + new_entries
        self.context_manager.set_context(group_context_key, merged)
        return len(new_entries), len(merged)

    def _transfer_private_context(self, msg_type: str, user_openid: str, user_name: str,
                                  group_openid: str, channel_id: str, msg_id: str,
                                  unified_openid: str, group_context_key: str):
        """把用户私聊上下文合并到当前群聊上下文（保留群聊原有内容）"""
        # user_openid 在群聊中即 member_openid（用于@提醒等）；
        # unified_openid 是与私聊一致的统一用户标识，用它读取私聊上下文。
        # 若直接取不到，再尝试身份绑定映射（针对旧版两套 openid 命名空间的情况）
        bound = self.context_manager.get_bound_user_openid(user_openid)
        private_owner = bound or unified_openid
        private_history = self.context_manager.get_context(private_owner)
        if not private_history:
            if bound is None:
                self._send_reply(msg_type, user_openid, user_name, group_openid, channel_id, msg_id,
                                 "未找到您的私聊上下文。请先在私聊中和机器人对话产生记录；"
                                 "若您的群聊与私聊标识不一致，请在私聊发送 /生成转移码，"
                                 "然后在这里发送 /绑定转移码 <6位码> 绑定后重试。")
            else:
                self._send_reply(msg_type, user_openid, user_name, group_openid, channel_id, msg_id,
                                 "您的私聊上下文中暂无历史记录，无法转移。")
            return
        result = self._merge_private_into_group(group_context_key, private_history)
        if result is None:
            self._send_reply(msg_type, user_openid, user_name, group_openid, channel_id, msg_id,
                             "✅ 私聊上下文已存在于当前群聊中，未重复合并。")
        else:
            added, total = result
            self._send_reply(msg_type, user_openid, user_name, group_openid, channel_id, msg_id,
                             f"✅ 已将 {added} 条私聊记录合并到当前群聊（共 {total} 条）。")
    
    def _bind_and_transfer(self, msg_type: str, user_openid: str, user_name: str,
                           group_openid: str, channel_id: str, msg_id: str,
                           content: str, group_context_key: str):
        """校验转移码并绑定身份，绑定成功后立即转移私聊上下文"""
        parts = content.strip().split()
        if len(parts) < 2:
            self._send_reply(msg_type, user_openid, user_name, group_openid, channel_id, msg_id,
                             "用法：/绑定转移码 <6位码>（码请在私聊中发送 /生成转移码 获取）")
            return
        code = parts[-1].strip()
        # user_openid 在群聊中即 member_openid，绑定的是"群成员 -> 私聊用户"的映射
        bound_user_openid = self.context_manager.bind_transfer_code(user_openid, code)
        if not bound_user_openid:
            self._send_reply(msg_type, user_openid, user_name, group_openid, channel_id, msg_id,
                             "❌ 转移码无效或已过期，请在私聊中重新发送 /生成转移码。")
            return
        private_history = self.context_manager.get_context(bound_user_openid)
        if private_history:
            result = self._merge_private_into_group(group_context_key, private_history)
            if result is None:
                self._send_reply(msg_type, user_openid, user_name, group_openid, channel_id, msg_id,
                                 "✅ 身份绑定成功（私聊上下文已存在于当前群聊，未重复合并）。")
            else:
                added, total = result
                self._send_reply(msg_type, user_openid, user_name, group_openid, channel_id, msg_id,
                                 f"✅ 身份绑定成功，已将 {added} 条私聊记录合并到当前群聊（共 {total} 条）。")
        else:
            self._send_reply(msg_type, user_openid, user_name, group_openid, channel_id, msg_id,
                             "✅ 身份绑定成功（私聊暂无历史记录，未转移）。")
    
    def _send_reply(self, msg_type: str, user_openid: str, user_name: str,
                    group_openid: str, channel_id: str, msg_id: str, content: str):
        """发送回复（支持分段）"""
        # 分段发送
        segments = self._split_message(content, self.max_segment_length)
        
        for i, segment in enumerate(segments):
            # 已按需求移除群聊/频道回复开头的@用户前缀（不@人，直接发送回复内容）
            
            # 发送消息
            if msg_type == 'c2c':
                self.qq_client.send_message(user_openid, segment, msg_id=msg_id if i == 0 else None)
            elif msg_type == 'group':
                self.qq_client.send_group_message(group_openid, segment, msg_id=msg_id if i == 0 else None)
            elif msg_type == 'channel':
                # 频道私信（DIRECT_MESSAGE_CREATE）的回复应发给用户本人，走单聊消息接口
                self.qq_client.send_message(user_openid, segment, msg_id=msg_id if i == 0 else None)
            
            # 分段之间稍作延迟，避免频控（最后一段之后无需等待）
            if i < len(segments) - 1:
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
                current += line + '\n'
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