"""
文件处理模块 - 识别并处理QQ消息中的各类文件
支持下载图片并转换为base64，供多模态AI使用
"""

import os
import base64
import requests
from typing import List, Dict, Any, Optional


class FileHandler:
    """文件处理器 - 支持图片、视频、音频、文档等"""
    
    # 支持的文件类型
    SUPPORTED_TYPES = {
        'image': ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg'],
        'video': ['mp4', 'avi', 'mov', 'wmv', 'flv', 'mkv', 'webm'],
        'audio': ['mp3', 'wav', 'flac', 'aac', 'ogg', 'm4a'],
        'document': ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 
                     'txt', 'md', 'csv', 'json', 'xml', 'html']
    }
    
    def __init__(self, logger=None):
        self.logger = logger
    
    def get_file_type(self, filename: str) -> str:
        """根据文件名获取文件类型"""
        ext = filename.split('.')[-1].lower() if '.' in filename else ''
        
        for file_type, extensions in self.SUPPORTED_TYPES.items():
            if ext in extensions:
                return file_type
        
        return 'unknown'
    
    def process_attachments(self, attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """处理消息附件"""
        processed = []
        
        for attachment in attachments:
            file_info = {
                'url': attachment.get('url', ''),
                'filename': attachment.get('filename', ''),
                'size': attachment.get('size', 0),
                'type': 'unknown'
            }
            
            # 判断文件类型
            if 'width' in attachment and 'height' in attachment:
                file_info['type'] = 'image'
            elif 'duration' in attachment:
                file_info['type'] = 'video' if 'video' in attachment.get('content_type', '') else 'audio'
            else:
                file_info['type'] = self.get_file_type(attachment.get('filename', ''))
            
            # 对于图片，添加尺寸信息
            if 'width' in attachment:
                file_info['width'] = attachment['width']
            if 'height' in attachment:
                file_info['height'] = attachment['height']
            
            processed.append(file_info)
        
        return processed
    
    def download_file(self, url: str, save_path: str) -> bool:
        """下载文件到本地"""
        try:
            response = requests.get(url, timeout=60)
            if response.status_code == 200:
                with open(save_path, 'wb') as f:
                    f.write(response.content)
                if self.logger:
                    self.logger.debug(f"文件下载成功: {save_path}")
                return True
            else:
                if self.logger:
                    self.logger.error(f"文件下载失败: {response.status_code}")
                return False
        except Exception as e:
            if self.logger:
                self.logger.error(f"文件下载异常: {e}")
            return False
    
    def download_image_as_base64(self, url: str) -> Optional[str]:
        """
        下载图片并转换为 base64 编码的 data URL
        返回格式：data:image/png;base64,xxxxx
        """
        try:
            response = requests.get(url, timeout=30)
            if response.status_code == 200:
                content_type = response.headers.get('content-type', '')
                # 提取图片类型
                if 'image' in content_type:
                    img_type = content_type.split('/')[-1]  # png, jpeg, gif ...
                else:
                    # 从URL后缀猜测
                    ext = url.split('.')[-1].lower() if '.' in url else 'png'
                    img_type = 'png' if ext not in ['jpg', 'jpeg', 'gif', 'webp'] else ext
                
                b64_data = base64.b64encode(response.content).decode('utf-8')
                data_url = f"data:image/{img_type};base64,{b64_data}"
                if self.logger:
                    self.logger.debug(f"图片转base64成功，大小: {len(b64_data)} 字符")
                return data_url
            else:
                if self.logger:
                    self.logger.error(f"下载图片失败: {response.status_code}")
                return None
        except Exception as e:
            if self.logger:
                self.logger.error(f"下载图片异常: {e}")
            return None
    
    def get_file_description(self, file_info: Dict[str, Any]) -> str:
        """获取文件的文本描述，用于发送给AI（降级方案）"""
        filename = file_info.get('filename', '未知文件')
        file_type = file_info.get('type', 'unknown')
        
        descriptions = {
            'image': f"图片文件: {filename}",
            'video': f"视频文件: {filename}",
            'audio': f"音频文件: {filename}",
            'document': f"文档文件: {filename}"
        }
        
        description = descriptions.get(file_type, f"文件: {filename}")
        
        # 添加附加信息
        if 'width' in file_info and 'height' in file_info:
            description += f" (尺寸: {file_info['width']}x{file_info['height']})"
        if 'size' in file_info and file_info['size'] > 0:
            description += f" (大小: {self._format_size(file_info['size'])})"
        
        return description
    
    def _format_size(self, size: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"