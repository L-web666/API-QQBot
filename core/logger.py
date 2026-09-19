"""
日志管理模块 - 负责日志的创建、写入和自动分割
"""

import os
import sys
import logging
import re
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from typing import Optional


# 转移码正则：匹配 "转移码：123456" / "绑定转移码 123456" 形式的 6 位数字
_TRANSFER_CODE_RE = re.compile(r'((?:转移码|绑定转移码)[:：]?\s*)\d{6}')


def mask_transfer_code(text: str) -> str:
    """日志脱敏：把消息内容中的转移码打码，避免明文落入日志"""
    if not text:
        return text
    return _TRANSFER_CODE_RE.sub(r'\1******', text)


# 北京时间时区（固定 UTC+8，不随系统时区变化）
_BJTZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """返回当前北京时间"""
    return datetime.now(_BJTZ)


class BeijingFormatter(logging.Formatter):
    """日志格式化器：时间戳固定用北京时间，避免系统时区影响日志时间"""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=_BJTZ)
        return dt.strftime(datefmt or '%Y-%m-%d %H:%M:%S')


# ============ 控制台彩色输出 ============
# 只给「时间」和「级别」上色，后面的正文保持原样；
# 文件日志始终使用不带颜色的 BeijingFormatter，避免日志文件里出现 ANSI 乱码。

_ANSI_RESET = '\033[0m'
_ANSI_TIME = '\033[90m'          # 时间：灰色
_ANSI_LEVEL = {
    'DEBUG': '\033[90m',         # 灰
    'INFO': '',                  # 默认色（不上色）
    'WARNING': '\033[33m',       # 黄
    'ERROR': '\033[31m',         # 红
    'CRITICAL': '\033[1;31m',    # 亮红加粗
}


class ColorConsoleFormatter(BeijingFormatter):
    """控制台彩色格式化器：时间灰色、级别按等级上色，正文无色"""

    def format(self, record):
        time_str = self.formatTime(record, self.datefmt)
        level = record.levelname
        color = _ANSI_LEVEL.get(level, '')
        level_str = '%s%s%s' % (color, level, _ANSI_RESET) if color else level
        line = '%s%s%s - %s - %s - %s' % (_ANSI_TIME, time_str, _ANSI_RESET,
                                          record.name, level_str, record.getMessage())
        # 异常堆栈、堆栈信息照常附在后面（不额外上色）
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line = line + '\n' + record.exc_text
        if record.stack_info:
            line = line + '\n' + self.formatStack(record.stack_info)
        return line


def _enable_windows_ansi() -> bool:
    """Windows 10+ 控制台开启 ANSI 转义支持（成功返回 True）；非 Windows 直接 True"""
    if os.name != 'nt':
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        enabled = False
        for std_id in (-11, -12):        # STDOUT / STDERR
            handle = kernel32.GetStdHandle(std_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                if kernel32.SetConsoleMode(handle, mode.value | 0x0004):  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                    enabled = True
        return enabled
    except Exception:
        return False


def console_color_supported(stream=None) -> bool:
    """当前控制台是否支持彩色输出。

    规则：NO_COLOR 环境变量=强制关闭；FORCE_COLOR=强制开启；
    否则要求输出是终端（重定向到文件/管道、无控制台的 exe 都不上色）。
    """
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    stream = stream if stream is not None else sys.stderr
    try:
        if stream is None or not stream.isatty():
            return False
    except Exception:
        return False
    if os.name == 'nt':
        return _enable_windows_ansi()
    return True


class Logger:
    """日志管理器 - 每次启动自动创建新日志文件，超过大小自动分割"""
    
    LOG_DIR = "data/logs"
    
    def __init__(self, max_size_mb: int = 10, console_color: Optional[bool] = None):
        self.max_size_bytes = max_size_mb * 1024 * 1024
        # None = 自动判断（终端才上色）；True/False = 配置里的 log.console_color
        self.console_color = console_color
        self._ensure_log_dir()
        self.logger = self._create_logger()
    
    def _ensure_log_dir(self):
        """确保日志目录存在"""
        if not os.path.exists(self.LOG_DIR):
            os.makedirs(self.LOG_DIR)
    
    def _create_logger(self) -> logging.Logger:
        """创建日志记录器"""
        # 日志文件名：程序启动的日期和时间.txt（固定北京时间）
        timestamp = beijing_now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(self.LOG_DIR, f"{timestamp}.txt")
        
        logger = logging.getLogger('QQAIBot')
        logger.setLevel(logging.DEBUG)
        
        # 清除已有的handler避免重复（先关闭再移除：不然旧的日志文件句柄要等 GC 才释放，
        # 期间还可能丢最后几行没 flush 的日志）
        for _h in list(logger.handlers):
            try:
                _h.close()
            except Exception:
                pass
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
        
        # 格式化（时间戳固定北京时间）
        # 文件：纯文本（不上色）；控制台：时间灰色 + 级别按等级上色，正文无色
        file_formatter = BeijingFormatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        if self.console_color is None:
            use_color = console_color_supported(console_handler.stream)
        else:
            use_color = bool(self.console_color) and console_color_supported(console_handler.stream)
        if use_color:
            console_handler.setFormatter(ColorConsoleFormatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'))
        else:
            console_handler.setFormatter(file_formatter)
        
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