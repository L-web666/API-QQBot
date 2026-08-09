#!/usr/bin/env python3
"""
QQ AI Bot - 主程序
连接QQ机器人与AI模型服务
"""

import sys
import os
import signal
from typing import Dict, Any

# 添加core模块到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config_manager import ConfigManager
from core.logger import Logger
from core.context_manager import ContextManager
from core.qq_client import QQClient
from core.ai_client import AIClient
from core.message_filter import MessageFilter
from core.message_processor import MessageProcessor
from core.file_handler import FileHandler


class QQAIbot:
    """QQ AI Bot 主类"""
    
    def __init__(self):
        # 加载配置
        self.config_manager = ConfigManager()
        self.config = self.config_manager.config
        
        # 初始化日志
        max_log_size = self.config.get('log', {}).get('max_size_mb', 10)
        self.logger = Logger(max_size_mb=max_log_size)
        self.log = self.logger.get_logger()
        
        self.log.info("=" * 60)
        self.log.info("QQ AI Bot 启动")
        self.log.info("=" * 60)
        
        # 检查QQ配置
        qq_config = self.config.get('qq', {})
        if not qq_config.get('app_id') or qq_config.get('app_id') == '请填写你的APPID':
            self.log.error("请先在config.json中填写正确的QQ AppID")
            self.initialized = False
            return
        if not qq_config.get('app_secret') or qq_config.get('app_secret') == '请填写你的APPSECRET':
            self.log.error("请先在config.json中填写正确的QQ AppSecret")
            self.initialized = False
            return
        
        # 检查AI配置（使用简化版）
        ai_config = self.config_manager.get_ai_config()
        if not ai_config.get('api_key') or ai_config.get('api_key') == '请填写你的API密钥':
            self.log.error("请先在config.json中填写正确的API Key")
            self.initialized = False
            return
        if not ai_config.get('base_url') or ai_config.get('base_url') == '请填写API基础地址（如 https://api.openai.com/v1）':
            self.log.error("请先在config.json中填写API基础地址")
            self.initialized = False
            return
        if not ai_config.get('model') or ai_config.get('model') == '请填写模型名称（如 gpt-3.5-turbo）':
            self.log.error("请先在config.json中填写模型名称")
            self.initialized = False
            return
        
        self.log.info(f"使用AI模型: {ai_config.get('model')}")
        
        # 初始化所有模块
        self._init_modules(qq_config, ai_config)
        self.initialized = True
        
        # 设置信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _init_modules(self, qq_config: Dict[str, Any], ai_config: Dict[str, Any]):
        """初始化所有模块"""
        # QQ客户端
        self.qq_client = QQClient(
            app_id=qq_config['app_id'],
            app_secret=qq_config['app_secret'],
            sandbox=qq_config.get('sandbox', False),
            reconnect_attempts=qq_config.get('reconnect_attempts', 5),
            reconnect_interval=qq_config.get('reconnect_interval', 10),
            logger=self.log
        )
        
        # AI客户端
        self.ai_client = AIClient(ai_config, logger=self.log)
        
        # 上下文管理器
        max_history = self.config.get('context', {}).get('max_history', 20)
        self.context_manager = ContextManager(max_history=max_history)
        
        # 文件处理器
        self.file_handler = FileHandler(logger=self.log)
        
        # 消息过滤器
        filter_config = self.config.get('filter', {})
        filter_config['filter_meaningless'] = self.config.get('message', {}).get('filter_meaningless', True)
        self.message_filter = MessageFilter(filter_config)
        
        # 消息处理器配置
        processor_config = {
            'max_queue_size': self.config.get('message', {}).get('max_queue_size', 10),
            'max_segment_length': self.config.get('message', {}).get('max_segment_length', 2000),
            'system_prompt': self.config.get('system_prompt', '你是一个智能助手。'),
            'require_mention': self.config.get('group', {}).get('require_mention', True),
            'reply_with_mention': self.config.get('group', {}).get('reply_with_mention', True),
            'context_enabled': self.config.get('context', {}).get('enabled', True)
        }
        
        # 消息处理器
        self.message_processor = MessageProcessor(
            qq_client=self.qq_client,
            ai_client=self.ai_client,
            context_manager=self.context_manager,
            message_filter=self.message_filter,
            file_handler=self.file_handler,
            logger=self.log,
            config=processor_config
        )
        
        # 设置QQ消息处理回调
        self.qq_client.on_message(self._handle_message)
    
    def _handle_message(self, message: Dict[str, Any]):
        """处理收到的消息"""
        self.message_processor.submit(message)
    
    def _signal_handler(self, signum, frame):
        """信号处理"""
        self.log.info(f"收到信号 {signum}，正在关闭...")
        self.stop()
        sys.exit(0)
    
    def run(self):
        """运行主程序"""
        if not self.initialized:
            self.log.error("程序初始化失败，请检查配置文件后重新启动")
            return
        
        self.log.info("启动消息处理器...")
        self.message_processor.start()
        
        self.log.info("连接QQ服务器...")
        success = self.qq_client.connect()
        
        if not success:
            self.log.error("连接QQ服务器失败")
            self.stop()
            return
        
        self.log.info("QQ AI Bot 运行中...")
    
    def stop(self):
        """停止程序"""
        self.log.info("正在停止...")
        if hasattr(self, 'message_processor'):
            self.message_processor.stop()
        if hasattr(self, 'qq_client'):
            self.qq_client.disconnect()
        self.log.info("程序已停止")


def main():
    """主函数"""
    bot = QQAIbot()
    if bot.initialized:
        bot.run()
    else:
        print("初始化失败，请查看日志文件 data/logs/ 中的详细信息。")


if __name__ == "__main__":
    main()