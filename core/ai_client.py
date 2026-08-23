"""
AI服务调用模块 - 支持多个AI厂商
支持多模态图片识别（OpenAI兼容格式）
"""

import json
import time
import requests
from typing import List, Dict, Any, Optional


class AIClient:
    """AI客户端 - 支持OpenAI、DeepSeek、智谱AI、腾讯混元等"""
    
    def __init__(self, provider_config: Dict[str, Any], logger=None):
        self.config = provider_config
        self.logger = logger
        self.api_key = provider_config.get('api_key', '')
        self.base_url = provider_config.get('base_url', '')
        self.model = provider_config.get('model', 'gpt-3.5-turbo')
        self.file_handler = None  # 由外部注入
        # 语音识别（ASR）配置：可与对话模型相同（OpenAI 兼容 /audio/transcriptions），也可独立
        self.asr_base_url = (provider_config.get('asr_base_url') or '').strip()
        self.asr_api_key = (provider_config.get('asr_api_key') or '').strip()
        self.asr_model = provider_config.get('asr_model', 'whisper-1')

    def transcribe_audio(self, audio_url: str) -> Optional[str]:
        """语音转文字（OpenAI 兼容 ASR：POST {base}/audio/transcriptions）。

        返回识别出的文字；失败返回 None。
        """
        if not self.file_handler:
            if self.logger:
                self.logger.warning("file_handler 未注入，无法下载语音")
            return None
        base = self.asr_base_url or self.base_url
        api_key = self.asr_api_key or self.api_key
        if not base or not api_key:
            if self.logger:
                self.logger.warning("语音识别未配置（asr_base_url / asr_api_key），无法转文字")
            return None
        audio_bytes = self.file_handler.download_file_to_bytes(audio_url)
        if not audio_bytes:
            if self.logger:
                self.logger.warning("语音文件下载失败")
            return None
        # 语音文件通常没有扩展名或为 silk/amr，统一命名为 .mp3 让服务端按内容识别
        files = {'file': ('voice.mp3', audio_bytes, 'application/octet-stream')}
        data = {'model': self.asr_model}
        url = f"{base.rstrip('/')}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            response = requests.post(url, headers=headers, files=files, data=data, timeout=120)
            if response.status_code == 200:
                result = response.json()
                text = result.get('text', '').strip()
                if self.logger:
                    self.logger.info(f"语音识别成功: {text[:50]}...")
                return text or None
            if self.logger:
                self.logger.error(f"语音识别失败: {response.status_code} {response.text[:300]}")
            return None
        except Exception as e:
            if self.logger:
                self.logger.error(f"语音识别异常: {e}")
            return None
    
    def chat(self, messages: List[Dict[str, str]], stream: bool = False, 
             temperature: float = 0.7, max_tokens: int = 4096, retries: int = 2) -> Optional[str]:
        """
        纯文本对话（含重试）
        """
        if not self.api_key:
            self.logger.error("API Key未配置")
            return "抱歉，AI服务未配置，请联系管理员。"
        
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream
        }
        
        for attempt in range(retries + 1):
            try:
                response = requests.post(url, headers=headers, json=data, timeout=120)
                if response.status_code == 200:
                    result = response.json()
                    if 'choices' in result and len(result['choices']) > 0:
                        content = result['choices'][0].get('message', {}).get('content', '')
                        self.logger.debug(f"AI回复成功: {content[:50]}...")
                        return content
                    else:
                        self.logger.error(f"AI响应格式异常: {result}")
                        if attempt == retries:
                            return "AI服务响应异常，请稍后再试。"
                else:
                    self.logger.error(f"AI调用失败 (尝试 {attempt+1}/{retries+1}): {response.text}")
                    if attempt == retries:
                        return "AI服务暂时不可用，请稍后再试。"
            except requests.exceptions.Timeout:
                self.logger.error(f"AI调用超时 (尝试 {attempt+1}/{retries+1})")
                if attempt == retries:
                    return "AI服务响应超时，请稍后再试。"
            except Exception as e:
                self.logger.error(f"AI调用异常 (尝试 {attempt+1}/{retries+1}): {e}")
                if attempt == retries:
                    return "AI服务暂时不可用，请稍后再试。"
            # 仅在还有重试机会时指数退避，最后一次失败后直接返回，不再空等
            if attempt < retries:
                time.sleep(2 ** attempt)
        return "AI服务暂时不可用，请稍后再试。"
    
    def chat_with_files(self, messages: List[Dict[str, str]], 
                        files: List[Dict[str, Any]] = None) -> Optional[str]:
        """
        带文件的多模态对话（优先尝试多模态，失败后降级为文本描述）
        """
        # 如果没有文件，直接调用纯文本
        if not files:
            return self.chat(messages)
        
        # 检查是否有图片文件
        has_image = any(f.get('type') == 'image' for f in files)
        
        # 如果有图片且 file_handler 可用，优先尝试多模态请求
        if has_image and self.file_handler is not None:
            multimodal_response = self._chat_multimodal(messages, files)
            if multimodal_response is not None:
                # 多模态请求成功（包括返回空字符串），直接返回
                return multimodal_response
            else:
                # 多模态请求失败，记录日志并降级
                if self.logger:
                    self.logger.warning("多模态请求失败，降级为纯文本描述")
                # 继续执行降级逻辑（如下）
        
        # 降级：仅使用文件文本描述（适用于非图片文件或图片模型不支持）
        # 收集所有文件的描述
        file_descriptions = []
        for file_info in files:
            desc = self.file_handler.get_file_description(file_info) if self.file_handler else f"文件: {file_info.get('filename')}"
            file_descriptions.append(desc)
        
        # 将文件描述附加到最后一条用户消息中
        if messages and messages[-1]['role'] == 'user':
            new_messages = [m.copy() for m in messages]
            file_desc = "\n".join(file_descriptions)
            new_messages[-1]['content'] = f"{new_messages[-1]['content']}\n\n[附件信息: {file_desc}]"
            return self.chat(new_messages)
        else:
            return self.chat(messages)
    
    def _chat_multimodal(self, messages: List[Dict[str, str]], 
                         files: List[Dict[str, Any]]) -> Optional[str]:
        """
        发送多模态对话请求（OpenAI兼容格式）
        返回: 成功返回回复字符串，失败返回 None
        """
        if not self.api_key:
            self.logger.error("API Key未配置")
            return None
        
        # 构建多模态消息
        multimodal_messages = []
        for msg in messages:
            if msg['role'] == 'user' and msg == messages[-1]:
                # 最后一条用户消息，转换为多模态格式
                content_parts = []
                # 文本部分
                content_parts.append({"type": "text", "text": msg['content']})
                
                # 图片部分（只处理图片文件）
                for file_info in files:
                    if file_info.get('type') != 'image':
                        continue
                    url = file_info.get('url')
                    if not url:
                        if self.logger:
                            self.logger.warning("图片缺少URL，跳过")
                        continue
                    
                    # 下载并转换为base64
                    data_url = self.file_handler.download_image_as_base64(url)
                    if data_url:
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": data_url}
                        })
                    else:
                        # 下载失败时添加文本描述
                        desc = self.file_handler.get_file_description(file_info)
                        content_parts.append({"type": "text", "text": f"[图片加载失败: {desc}]"})
                
                multimodal_messages.append({
                    "role": "user",
                    "content": content_parts
                })
            else:
                # 其他消息保持不变
                multimodal_messages.append(msg.copy())
        
        # 发起请求
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": self.model,
            "messages": multimodal_messages,
            "temperature": 0.7,
            "max_tokens": 4096
        }
        
        try:
            if self.logger:
                self.logger.debug(f"调用多模态AI: {url}, model={self.model}")
            
            response = requests.post(url, headers=headers, json=data, timeout=120)
            
            if response.status_code == 200:
                result = response.json()
                if 'choices' in result and len(result['choices']) > 0:
                    content = result['choices'][0].get('message', {}).get('content', '')
                    if self.logger:
                        self.logger.debug(f"多模态AI回复成功: {content[:50]}...")
                    return content
                else:
                    if self.logger:
                        self.logger.error(f"多模态AI响应格式异常: {result}")
                    return None
            else:
                if self.logger:
                    self.logger.error(f"多模态AI调用失败: {response.status_code} - {response.text}")
                return None
        except requests.exceptions.Timeout:
            if self.logger:
                self.logger.error("多模态AI调用超时")
            return None
        except Exception as e:
            if self.logger:
                self.logger.error(f"多模态AI调用异常: {e}")
            return None
    
    def filter_thinking(self, content: str) -> str:
        """过滤掉AI回复中的思考过程"""
        import re
        
        patterns = [
            r'<thinking>.*?</thinking>',
            r'<thought>.*?</thought>',
            r'<reasoning>.*?</reasoning>',
            r'【思考】.*?【/思考】',
            r'\[思考\].*?\[/思考\]',
            r'--- 思考过程 ---.*?--- 回答 ---',
            r'^思考：[^\n]*(\n|$)',          # 仅删除以"思考："开头的整行，避免误删正文
        ]
        
        filtered = content
        for pattern in patterns:
            filtered = re.sub(pattern, '', filtered, flags=re.DOTALL | re.MULTILINE | re.IGNORECASE)
        
        filtered = re.sub(r'\n\s*\n', '\n\n', filtered)
        return filtered.strip()
    
    def strip_markdown(self, content: str) -> str:
        """
        去除AI回复中的Markdown格式符号，使内容在QQ聊天中显示更干净。
        保留正文；只处理常见且安全的标记（加粗/行内代码/删除线/标题/列表/引用/代码块/链接/表格），
        不处理单个 * 和 _（避免误伤"3*4"这类正常文本）。
        表格会转换为 "单元格 | 单元格" 的纯文本行（删除分隔行）。
        """
        import re
        text = content or ''
        # 代码块：删掉围栏行，保留内部内容
        text = re.sub(r'```[^\n]*\n?', '', text)
        text = re.sub(r'~~~[^\n]*\n?', '', text)
        # 行内代码 / 加粗 / 删除线
        text = re.sub(r'`([^`\n]+)`', r'\1', text)
        text = re.sub(r'\*\*([^*\n]+)\*\*', r'\1', text)
        text = re.sub(r'__([^_\n]+)__', r'\1', text)
        text = re.sub(r'~~([^~\n]+)~~', r'\1', text)
        # 链接 [文字](url) -> 文字 (url)
        text = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', r'\1 (\2)', text)
        # 行首标记：标题 / 引用 / 无序列表 / 有序列表 / 表格
        lines = []
        for line in text.split('\n'):
            stripped = line.strip()
            # 表格分隔行（仅含 | - : 空格，如 |---|---| 或 ---）：整行删除
            if re.fullmatch(r'\|?[\s|:\-]+\|?', stripped):
                continue
            # 表格数据行：去掉首尾竖线，规整为 "a | b" 形式
            if '|' in line:
                cells = [c.strip() for c in line.strip().strip('|').split('|')]
                line = ' | '.join(cells)
            line = re.sub(r'^\s*#{1,6}\s*', '', line)     # 标题
            line = re.sub(r'^\s*>\s?', '', line)          # 引用
            line = re.sub(r'^\s*[-*+]\s+', '', line)      # 无序列表
            line = re.sub(r'^\s*\d+[.)]\s*', '', line)    # 有序列表
            lines.append(line)
        text = '\n'.join(lines)
        # 清理多余空行与首尾空白
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()