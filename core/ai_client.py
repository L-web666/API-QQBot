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