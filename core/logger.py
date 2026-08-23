"""
日志管理模块 - 负责日志的创建、写入和自动分割
"""

import os
import logging
import re
from logging.handlers import RotatingFileHandler
from datetime import datetime
from typing import Optional


# 转移码正则：匹配 "转移码：123456" / "绑定转移码 123456" 形式的 6 位数字
_TRANSFER_CODE_RE = re.compile(r'((?:转移码|绑定转移码)[:：]?\s*)\d{6}')


def mask_transfer_code(text: str) -> str:
    """日志脱敏：把消息内容中的转移码打码，避免明文落入日志"""
    if not text:
        return text
    return _TRANSFER_CODE_RE.sub(r'\1******', text)


class Logger:
    """日志管理器 - 每次启动自动创建新日志文件，超过大小自动分割"""
    
    LOG_DIR = "data/logs"
    
    def __init__(self, max_size_mb: int = 10):
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self._ensure_log_dir()
        self.logger = self._create_logger()
    
    def _ensure_log_dir(self):
        """确保日志目录存在"""
        if not os.path.exists(self.LOG_DIR):
            os.makedirs(self.LOG_DIR)
    
    def _create_logger(self) -> logging.Logger:
        """创建日志记录器"""
        # 日志文件名：程序启动的日期和时间.txt
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(self.LOG_DIR, f"{timestamp}.txt")
        
        logger = logging.getLogger('QQAIBot')
        logger.setLevel(logging.DEBUG)
        
        # 清除已有的handler避免重复
        logger.handlers.clear()
        
        # 文件处理器 - 自动分割[reference:11]
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=self.max_size_bytes,
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # 格式化
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        
        return logger
    
    def get_logger(self) -> logging.Logger:
        """获取日志记录器"""
        return self.logger
    
    def info(self, message: str):
        self.logger.info(message)
    
    def debug(self, message: str):
        self.logger.debug(message)
    
    def warning(self, message: str):
        self.logger.warning(message)
    
    def error(self, message: str):
        self.logger.error(message)
    
    def critical(self, message: str):
        self.logger.critical(message)