"""
QQ机器人连接模块 - 负责WebSocket连接、消息收发
"""

import json
import time
import threading
import requests
import websocket
from typing import Optional, Dict, Any, Callable
from urllib.parse import urlparse


class QQClient:
    """QQ机器人客户端"""
    
    API_BASE = "https://api.bot.qq.com"
    WSS_GATEWAY = "wss://api.bot.qq.com/websocket"
    
    def __init__(self, app_id: str, app_secret: str, sandbox: bool = False, reconnect_attempts: int = 5, reconnect_interval: int = 10, logger=None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.sandbox = sandbox
        self.logger = logger
        self.access_token = None
        self.token_expires_at = 0
        self.ws = None
        self.is_running = False
        self.message_handler = None
        self._heartbeat_thread = None
        self.heartbeat_interval = 30          # 默认值，后续由服务器更新
        self.reconnect_attempts = reconnect_attempts           # 最大重连次数
        self.reconnect_interval = reconnect_interval          # 重连间隔（秒）
        self.should_reconnect = True          # 控制是否继续重连
        self._current_attempt = 0
    
    def _get_api_base(self) -> str:
        """获取API基础地址"""
        if self.sandbox:
            return "https://sandbox.api.sgroup.qq.com"
        return self.API_BASE
    
    def get_access_token(self) -> Optional[str]:
        """获取Access Token[reference:13][reference:14]"""
        if self.access_token and time.time() < self.token_expires_at:
            return self.access_token
        
        url = f"{self._get_api_base()}/app/getAppAccessToken"
        headers = {"Content-Type": "application/json"}
        data = {
            "appId": self.app_id,
            "clientSecret": self.app_secret
        }
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=10)
            result = response.json()
            
            if response.status_code == 200 and 'access_token' in result:
                self.access_token = result['access_token']
                self.token_expires_at = time.time() + int(result.get('expires_in', 7200)) - 300
                if self.logger:
                    self.logger.info("Access Token获取成功")
                return self.access_token
            else:
                if self.logger:
                    self.logger.error(f"获取Access Token失败: {result}")
                return None
        except Exception as e:
            if self.logger:
                self.logger.error(f"获取Access Token异常: {e}")
            return None
    
    def send_message(self, openid: str, content: str, msg_type: int = 0, 
                     msg_id: Optional[str] = None, is_wakeup: bool = False) -> bool:
        """发送单聊消息[reference:15][reference:16]"""
        token = self.get_access_token()
        if not token:
            return False
        
        url = f"{self._get_api_base()}/v2/users/{openid}/messages"
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        data = {
            "content": content,
            "msg_type": msg_type
        }
        if msg_id:
            data["msg_id"] = msg_id
        if is_wakeup:
            data["is_wakeup"] = True
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)
            if response.status_code == 200:
                if self.logger:
                    self.logger.debug(f"消息发送成功: {content[:50]}...")
                return True
            else:
                if self.logger:
                    self.logger.error(f"发送消息失败: {response.text}")
                return False
        except Exception as e:
            if self.logger:
                self.logger.error(f"发送消息异常: {e}")
            return False
    
    def send_group_message(self, group_openid: str, content: str, msg_type: int = 0,
                           msg_id: Optional[str] = None) -> bool:
        """发送群聊消息[reference:17]"""
        token = self.get_access_token()
        if not token:
            return False
        
        url = f"{self._get_api_base()}/v2/groups/{group_openid}/messages"
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        data = {
            "content": content,
            "msg_type": msg_type
        }
        if msg_id:
            data["msg_id"] = msg_id
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)
            if response.status_code == 200:
                if self.logger:
                    self.logger.debug(f"群消息发送成功: {content[:50]}...")
                return True
            else:
                if self.logger:
                    self.logger.error(f"发送群消息失败: {response.text}")
                return False
        except Exception as e:
            if self.logger:
                self.logger.error(f"发送群消息异常: {e}")
            return False
    
    def send_channel_message(self, channel_id: str, content: str, msg_type: int = 0,
                             msg_id: Optional[str] = None) -> bool:
        """发送频道消息"""
        token = self.get_access_token()
        if not token:
            return False
        
        url = f"{self._get_api_base()}/v2/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        data = {
            "content": content,
            "msg_type": msg_type
        }
        if msg_id:
            data["msg_id"] = msg_id
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)
            if response.status_code == 200:
                if self.logger:
                    self.logger.debug(f"频道消息发送成功: {content[:50]}...")
                return True
            else:
                if self.logger:
                    self.logger.error(f"发送频道消息失败: {response.text}")
                return False
        except Exception as e:
            if self.logger:
                self.logger.error(f"发送频道消息异常: {e}")
            return False
    
    def on_message(self, handler: Callable):
        """设置消息处理函数"""
        self.message_handler = handler
    
    def _on_ws_message(self, ws, message):
        try:
            data = json.loads(message)
            op = data.get('op')
            self.logger.debug(f"收到WebSocket消息: {data}")
            
            if op == 10:  # Hello
                d = data.get('d', {})
                self.heartbeat_interval = d.get('heartbeat_interval', 30000) / 1000.0
                self.logger.info(f"收到Hello，心跳间隔: {self.heartbeat_interval}秒")
                self._send_identify(ws)
                return
            
            if op == 11:  # 心跳响应
                self.logger.debug("收到心跳响应")
                return
            
            if op == 0 and 't' in data:
                event_type = data['t']
                event_data = data.get('d', {})
                # 根据事件类型分发
                if event_type == 'GROUP_AT_MESSAGE_CREATE':
                    self._handle_group_at_message(event_data)
                elif event_type == 'C2C_MESSAGE_CREATE':
                    self._handle_c2c_message(event_data)
                elif event_type == 'DIRECT_MESSAGE_CREATE':
                    self._handle_direct_message(event_data)
                # 可以添加其他事件
        except json.JSONDecodeError as e:
            self.logger.error(f"解析WebSocket消息失败: {e}")
        except Exception as e:
            self.logger.error(f"处理WebSocket消息异常: {e}")

    def _send_identify(self, ws):
        token = self.get_access_token()
        if not token:
            self.logger.error("无有效Token，无法发送Identify")
            return
        
        # 按需开启事件（位掩码）
        # 常用事件位：
        #   bit 4  : DIRECT_MESSAGE         (频道私信)
        #   bit 5  : GROUP_AT_MESSAGE       (群聊@消息)
        #   bit 6  : C2C_MESSAGE            (私聊消息)
        # 根据需要，也可以添加其他位
        intents = (1 << 25) # 群@ + C2C
        
        payload = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": intents
            }
        }
        ws.send(json.dumps(payload))
        self.logger.info(f"已发送Identify鉴权，intents={intents}")
    
    def _handle_group_at_message(self, data: Dict[str, Any]):
        """处理群@消息[reference:19]"""
        if not self.message_handler:
            return
        
        content = data.get('content', '')  # 已自动去除@机器人的前缀[reference:20]
        group_openid = data.get('group_openid', '')
        author = data.get('author', {})
        user_openid = author.get('member_openid', '')  # 群成员OpenID[reference:21]
        user_name = author.get('username', '用户')
        msg_id = data.get('id', '')
        attachments = data.get('attachments', [])  # 消息附件[reference:22]
        
        # 构造消息对象
        message = {
            'type': 'group',
            'content': content,
            'group_openid': group_openid,
            'user_openid': user_openid,
            'user_name': user_name,
            'msg_id': msg_id,
            'attachments': attachments
        }
        
        self.message_handler(message)
    
    def _handle_c2c_message(self, data: Dict[str, Any]):
        """处理C2C私聊消息"""
        if not self.message_handler:
            return
        
        content = data.get('content', '')
        author = data.get('author', {})
        user_openid = author.get('user_openid', '')
        user_name = author.get('username', '用户')
        msg_id = data.get('id', '')
        attachments = data.get('attachments', [])
        
        message = {
            'type': 'c2c',
            'content': content,
            'user_openid': user_openid,
            'user_name': user_name,
            'msg_id': msg_id,
            'attachments': attachments
        }
        
        self.message_handler(message)
    
    def _handle_direct_message(self, data: Dict[str, Any]):
        """处理频道私信消息"""
        if not self.message_handler:
            return
        
        content = data.get('content', '')
        author = data.get('author', {})
        user_openid = author.get('user_openid', '')
        user_name = author.get('username', '用户')
        msg_id = data.get('id', '')
        channel_id = data.get('channel_id', '')
        attachments = data.get('attachments', [])
        
        message = {
            'type': 'channel',
            'content': content,
            'user_openid': user_openid,
            'user_name': user_name,
            'msg_id': msg_id,
            'channel_id': channel_id,
            'attachments': attachments
        }
        
        self.message_handler(message)
    
    def _heartbeat_loop(self):
        while self.is_running:
            time.sleep(self.heartbeat_interval)
            if self.ws and self.ws.sock and self.ws.sock.connected:
                try:
                    self.ws.send(json.dumps({"op": 1, "d": None}))
                    self.logger.debug("发送心跳")
                except Exception as e:
                    self.logger.error(f"发送心跳失败: {e}")

    
    def connect(self):
        """连接WebSocket，含自动重连"""
        self.should_reconnect = True
        self._current_attempt = 0

        while self.should_reconnect and self._current_attempt < self.reconnect_attempts:
            self._current_attempt += 1
            token = self.get_access_token()
            if not token:
                self.logger.error(f"无法获取Access Token (尝试 {self._current_attempt}/{self.reconnect_attempts})")
                time.sleep(self.reconnect_interval)
                continue
            
            ws_url = f"{self.WSS_GATEWAY}?access_token={token}"
            self.ws = websocket.WebSocketApp(
                ws_url,
                on_message=self._on_ws_message,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close,
                on_open=self._on_ws_open
            )
            
            self.is_running = True
            if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
                self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
                self._heartbeat_thread.start()
            
            self.logger.info(f"正在连接WebSocket (尝试 {self._current_attempt}/{self.reconnect_attempts})...")
            try:
                self.ws.run_forever()
            except Exception as e:
                self.logger.error(f"WebSocket运行异常: {e}")
            
            # 如果主动断开，则退出循环
            if not self.should_reconnect:
                break
            
            # 如果连接断开但未达到最大次数，等待间隔
            if self._current_attempt < self.reconnect_attempts:
                self.logger.info(f"将在 {self.reconnect_interval} 秒后重连...")
                time.sleep(self.reconnect_interval)
            else:
                self.logger.error("已达到最大重连次数，放弃连接")
                break

        return self._current_attempt < self.reconnect_attempts
    
    def _on_ws_open(self, ws):
        self.logger.info("WebSocket连接已建立")
        self._current_attempt = 0   # 连接成功，重置尝试计数器
        
    def _on_ws_error(self, ws, error):
        if self.logger:
            self.logger.error(f"WebSocket错误: {error}")
    
    def _on_ws_close(self, ws, close_status_code, close_msg):
        self.logger.info(f"WebSocket连接已关闭: {close_status_code} - {close_msg}")
        self.is_running = False
        # 不要在这里设置 should_reconnect = False，以便自动重连

    def disconnect(self):
        self.should_reconnect = False
        self.is_running = False
        if self.ws:
            self.ws.close()