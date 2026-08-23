"""
QQ机器人连接模块 - 负责WebSocket连接、消息收发、会话恢复
"""

import json
import time
import threading
import requests
import websocket
from typing import Optional, Dict, Any, Callable

from core.logger import mask_transfer_code


class QQClient:
    """QQ机器人客户端，支持自动重连和会话恢复（RESUME）"""
    
    API_BASE = "https://api.bot.qq.com"
    WSS_GATEWAY = "wss://api.bot.qq.com/websocket"
    
    def __init__(self, app_id: str, app_secret: str, sandbox: bool = False,
                 reconnect_attempts: int = 5, reconnect_interval: int = 10,
                 logger=None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.sandbox = sandbox
        self.logger = logger
        
        # 连接参数
        self.reconnect_attempts = reconnect_attempts
        self.reconnect_interval = reconnect_interval
        self.should_reconnect = True
        
        # 会话状态（用于 RESUME）
        self.session_id = None          # 会话ID，由 READY 事件提供
        self.last_seq = 0               # 最后收到的消息序列号
        self._should_resume = False     # 是否尝试恢复会话
        self.last_heartbeat_ack = 0     # 最近一次收到服务器响应（心跳回执/事件）的时间
        
        # WebSocket 相关
        self.access_token = None
        self.token_expires_at = 0
        self.ws = None
        self.is_running = False
        self.message_handler = None
        self._heartbeat_thread = None
        self.heartbeat_interval = 30    # 默认，由服务器更新
    
    def _get_api_base(self) -> str:
        """获取API基础地址"""
        if self.sandbox:
            return "https://sandbox.api.sgroup.qq.com"
        return self.API_BASE
    
    def _get_ws_gateway(self) -> str:
        """获取WebSocket网关地址（沙箱与正式环境不同）"""
        if self.sandbox:
            return "wss://sandbox.api.sgroup.qq.com/websocket"
        return self.WSS_GATEWAY
    
    def get_access_token(self) -> Optional[str]:
        """获取Access Token"""
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
    
    # ---------- 发送消息接口 ----------
    def send_message(self, openid: str, content: str, msg_type: int = 0,
                     msg_id: Optional[str] = None, is_wakeup: bool = False) -> bool:
        """发送单聊消息"""
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
                    self.logger.debug(f"消息发送成功: {mask_transfer_code(content)[:50]}...")
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
        """发送群聊消息"""
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
                    self.logger.debug(f"群消息发送成功: {mask_transfer_code(content)[:50]}...")
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
                    self.logger.debug(f"频道消息发送成功: {mask_transfer_code(content)[:50]}...")
                return True
            else:
                if self.logger:
                    self.logger.error(f"发送频道消息失败: {response.text}")
                return False
        except Exception as e:
            if self.logger:
                self.logger.error(f"发送频道消息异常: {e}")
            return False
    
    # ---------- 指令面板 ----------
    # 官方接口（请求格式已按文档确认）：
    #   POST /v2/panels              创建指令面板
    #   PUT  /v2/panels/{panel_id}   修改指令面板
    # 创建/修改请求体：
    #   { "scope": "c2c"|"group", "target_type": "all"|"specific",
    #     "user_openids"|"group_openids": [...],   # target_type=specific 时
    #     "panel": { "items": [{"type":"command","name":..,"desc":..} | {"type":"link","name":..,"link":..}], "remark": .. } }
    def create_command_panel(self, scope: str, target_type: str, items: list,
                             remark: str = '', openids: list = None) -> Optional[str]:
        """创建指令面板，成功返回 panel_id，失败返回 None"""
        token = self.get_access_token()
        if not token:
            return None
        url = f"{self._get_api_base()}/v2/panels"
        headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
        data = {
            "scope": scope,
            "target_type": target_type,
            "panel": {"items": items or []}
        }
        if remark:
            data["panel"]["remark"] = remark
        if target_type == 'specific' and openids:
            data["user_openids" if scope == 'c2c' else "group_openids"] = list(openids)
        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)
            if response.status_code == 200:
                result = response.json()
                panel_id = result.get('panel_id') or (result.get('data') or {}).get('panel_id')
                if self.logger:
                    self.logger.info(f"指令面板创建成功({scope}/{target_type}) panel_id={panel_id}")
                return panel_id
            else:
                if self.logger:
                    self.logger.error(f"指令面板创建失败: {response.status_code} {response.text}")
                    # 记录请求体（脱敏），便于对照官方文档排查
                    import json as _json
                    self.logger.error(f"指令面板创建请求体({scope}): {_json.dumps(data, ensure_ascii=False)[:800]}")
                return None
        except Exception as e:
            if self.logger:
                self.logger.error(f"指令面板创建异常: {e}")
            return None
    
    def update_command_panel(self, panel_id: str, scope: str, target_type: str, items: list,
                             remark: str = '', openids: list = None) -> bool:
        """更新已有指令面板"""
        token = self.get_access_token()
        if not token:
            return False
        url = f"{self._get_api_base()}/v2/panels/{panel_id}"
        headers = {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}
        data = {
            "scope": scope,
            "target_type": target_type,
            "panel": {"items": items or []}
        }
        if remark:
            data["panel"]["remark"] = remark
        if target_type == 'specific' and openids:
            data["user_openids" if scope == 'c2c' else "group_openids"] = list(openids)
        try:
            response = requests.put(url, headers=headers, json=data, timeout=30)
            if response.status_code in (200, 204):
                if self.logger:
                    self.logger.info(f"指令面板更新成功（{len(data['panel']['items'])} 条指令）")
                return True
            else:
                if self.logger:
                    self.logger.error(f"指令面板更新失败: {response.status_code} {response.text}")
                return False
        except Exception as e:
            if self.logger:
                self.logger.error(f"指令面板更新异常: {e}")
            return False
    
    def list_command_panels(self, scope: str = 'c2c', with_raw: bool = False):
        """查询指令面板列表（GET /v2/panels?scope=c2c|group），scope 必填，否则报"生效场景不合法"。

        返回面板列表（每个元素为 dict）。with_raw=True 时返回 (panels, raw_text)，
        raw_text 为接口原始响应，便于诊断返回结构。

        官方列表接口为分页格式：响应含 is_end（是否最后一页）、next_key（下一页游标），
        数据通常在 panels/data 字段；本方法自动翻页直到 is_end=true。"""
        token = self.get_access_token()
        if not token:
            return ([], '') if with_raw else []
        if scope not in ('c2c', 'group'):
            return ([], '') if with_raw else []
        headers = {"Authorization": f"QQBot {token}"}
        collected = []
        raw_text = ''
        next_key = ''
        try:
            for _page in range(1, 21):  # 最多翻 20 页，防止死循环
                url = f"{self._get_api_base()}/v2/panels?scope={scope}"
                # 分页参数：兼容 limit / page / next_key 三种常见风格
                url += "&limit=100&page_size=100"
                if next_key:
                    url += f"&next_key={next_key}"
                response = requests.get(url, headers=headers, timeout=30)
                raw_text = response.text
                if response.status_code != 200:
                    if self.logger:
                        self.logger.error(f"查询指令面板列表失败({scope}): {response.status_code} {raw_text}")
                    return (collected, raw_text) if with_raw else collected
                try:
                    result = response.json()
                except Exception:
                    if self.logger:
                        self.logger.error(f"查询指令面板列表返回非JSON({scope}): {raw_text[:500]}")
                    return (collected, raw_text) if with_raw else collected
                if not isinstance(result, dict):
                    if self.logger:
                        self.logger.warning(f"查询指令面板列表({scope})：返回非对象，原始响应: {raw_text[:500]}")
                    return (collected, raw_text) if with_raw else collected
                # 取出本页面板（官方字段为 records；兼容 panels/panel_list/list/data）
                page_panels = None
                for key in ('records', 'panels', 'panel_list', 'list'):
                    if isinstance(result.get(key), list):
                        page_panels = result[key]
                        break
                if page_panels is None:
                    data = result.get('data')
                    if isinstance(data, list):
                        page_panels = data
                    elif isinstance(data, dict):
                        for key in ('records', 'panels', 'panel_list', 'list'):
                            if isinstance(data.get(key), list):
                                page_panels = data[key]
                                break
                if page_panels is not None:
                    collected.extend(p for p in page_panels if isinstance(p, dict))
                # 分页游标与结束判断
                next_key = result.get('next_key') or (result.get('data') or {}).get('next_key') or ''
                if result.get('is_end') or not next_key:
                    break
            return (collected, raw_text) if with_raw else collected
        except Exception as e:
            if self.logger:
                self.logger.error(f"查询指令面板列表异常: {e}")
            return ([], raw_text) if with_raw else []
    
    def delete_command_panel(self, panel_id: str) -> bool:
        """删除指令面板（DELETE /v2/panels/{panel_id}）"""
        token = self.get_access_token()
        if not token:
            return False
        url = f"{self._get_api_base()}/v2/panels/{panel_id}"
        headers = {"Authorization": f"QQBot {token}"}
        try:
            response = requests.delete(url, headers=headers, timeout=30)
            if response.status_code in (200, 204):
                if self.logger:
                    self.logger.info(f"指令面板已删除: {panel_id}")
                return True
            else:
                if self.logger:
                    self.logger.error(f"删除指令面板失败: {response.status_code} {response.text}")
                return False
        except Exception as e:
            if self.logger:
                self.logger.error(f"删除指令面板异常: {e}")
            return False
    
    # ---------- 事件处理 ----------
    def on_message(self, handler: Callable):
        """设置消息处理函数"""
        self.message_handler = handler
    
    def _send_identify_or_resume(self, ws):
        """
        发送鉴权请求：优先尝试 RESUME，若无效则回退到 IDENTIFY
        """
        token = self.get_access_token()
        if not token:
            self.logger.error("无有效Token，无法发送鉴权请求")
            return
        
        # 如果存在有效的 session_id 且允许恢复，则尝试 RESUME
        if self.session_id and self._should_resume:
            resume_payload = {
                "op": 6,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self.session_id,
                    "seq": self.last_seq
                }
            }
            ws.send(json.dumps(resume_payload))
            self.logger.info(f"已发送RESUME请求，session_id={self.session_id}, seq={self.last_seq}")
        else:
            # 否则执行完整的 IDENTIFY
            # intents 按需订阅：25=群聊@消息(GROUP_AT_MESSAGE_CREATE)，26=C2C单聊消息(C2C_MESSAGE_CREATE)
            # 如需频道消息/频道私信等，需额外开通对应权限后再追加对应的 intent 位
            intents = (1 << 25) | (1 << 26)
            identify_payload = {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": intents
                }
            }
            ws.send(json.dumps(identify_payload))
            self.logger.info(f"已发送IDENTIFY鉴权，intents={intents}")
            # 清除旧的会话信息，准备新会话
            self.session_id = None
            self.last_seq = 0
            self._should_resume = False
    
    def _handle_dispatch(self, data: Dict[str, Any]):
        """
        处理 Dispatch (op=0) 事件
        """
        # 收到任何 Dispatch 事件都说明本会话已鉴权成功（IDENTIFY 或 RESUME 均会推送事件），
        # 此时重置重连计数，避免服务器周期性 RECONNECT 后计数不断累加最终耗尽导致程序退出。
        self._current_attempt = 0
        # 更新序列号
        if 's' in data:
            self.last_seq = data['s']
        
        event_type = data.get('t')
        event_data = data.get('d', {})
        
        # 如果是 READY 事件，保存 session_id
        if event_type == 'READY':
            self.session_id = event_data.get('session_id')
            self._should_resume = True
            # 收到服务器数据，视为连接存活
            self.last_heartbeat_ack = time.time()
            if self.logger:
                self.logger.info(f"READY 事件，session_id={self.session_id}")
        
        # 分发具体消息事件
        if event_type == 'GROUP_AT_MESSAGE_CREATE':
            self._handle_group_at_message(event_data)
        elif event_type == 'C2C_MESSAGE_CREATE':
            self._handle_c2c_message(event_data)
        elif event_type == 'DIRECT_MESSAGE_CREATE':
            self._handle_direct_message(event_data)
        # 可添加其他事件
    
    def _on_ws_message(self, ws, message):
        """WebSocket消息接收回调"""
        try:
            data = json.loads(message)
            op = data.get('op')
            
            if op == 10:   # Hello
                d = data.get('d', {})
                self.heartbeat_interval = d.get('heartbeat_interval', 30000) / 1000.0
                self.logger.info(f"收到Hello，心跳间隔: {self.heartbeat_interval}秒")
                self.last_heartbeat_ack = time.time()
                # 建立连接后立即发送鉴权
                self._send_identify_or_resume(ws)
                return
            
            if op == 11:   # 心跳响应
                self.logger.debug("收到心跳响应")
                self.last_heartbeat_ack = time.time()
                return
            
            if op == 0:    # Dispatch
                # 收到任何事件都说明连接存活，更新心跳时间戳
                self.last_heartbeat_ack = time.time()
                self._handle_dispatch(data)
                return
            
            if op == 7:    # RECONNECT - 服务端要求重连
                self.logger.warning("收到 RECONNECT 指令，将重连并尝试恢复会话")
                self._should_resume = True   # 保留 session_id 和 last_seq
                # 主动关闭连接，触发重连循环
                if self.ws:
                    self.ws.close()
                return
            
            if op == 9:    # Invalid Session - 会话无效，需要重新 IDENTIFY
                d = data.get('d')
                # d 为 True 表示可以重试，False 表示不可重试
                if d is False:
                    self.logger.error("Invalid Session (不可恢复)，清除会话信息，重新IDENTIFY")
                    self.session_id = None
                    self.last_seq = 0
                    self._should_resume = False
                    # 重新发送 IDENTIFY，但需要先关闭连接重连
                    if self.ws:
                        self.ws.close()
                else:
                    self.logger.warning("Invalid Session (可重试)，将重发鉴权")
                    # 可尝试重发 IDENTIFY
                    self._send_identify_or_resume(ws)
                return
            
            # 其他 opcode 可忽略或记录
            if op is not None:
                self.logger.debug(f"收到未处理的 opcode: {op}, data: {data}")
        except json.JSONDecodeError as e:
            self.logger.error(f"解析WebSocket消息失败: {e}")
        except Exception as e:
            self.logger.error(f"处理WebSocket消息异常: {e}")
    
    def _resolve_media(self, data: Dict[str, Any], scope: str = 'c2c',
                       openid: str = '') -> List[Dict[str, Any]]:
        """合并 QQ 事件中的富媒体附件（兼容 attachments / media 两种字段名）。

        media 元素可能只有 file_info（需调接口换下载链接），这里尽量补全 url；
        无法补全的条目保留 file_info，后续按文本描述降级处理。
        """
        raw = data.get('media') or data.get('attachments') or []
        out = []
        if not isinstance(raw, list):
            return out
        for m in raw:
            if not isinstance(m, dict):
                continue
            item = dict(m)
            # 没有 url 但有 file_info 时，尝试换取下载链接
            if not item.get('url') and item.get('file_info'):
                url = self._get_file_download_url(scope, openid, item['file_info'])
                if url:
                    item['url'] = url
            out.append(item)
        return out

    def _get_file_download_url(self, scope: str, openid: str, file_info: str) -> Optional[str]:
        """用 file_info 换取富媒体文件下载链接（QQ v2 富媒体规范）"""
        token = self.get_access_token()
        if not token or not openid or not file_info:
            return None
        try:
            if scope == 'group':
                url = f"{self._get_api_base()}/v2/groups/{openid}/files/{file_info}"
            elif scope == 'c2c':
                url = f"{self._get_api_base()}/v2/users/{openid}/files/{file_info}"
            else:
                url = f"{self._get_api_base()}/v2/users/{openid}/files/{file_info}"
            headers = {"Authorization": f"QQBot {token}"}
            response = requests.get(url, headers=headers, timeout=20)
            if response.status_code == 200:
                result = response.json()
                return result.get('url') or (result.get('data') or {}).get('url')
            if self.logger:
                self.logger.warning(f"获取文件下载链接失败: {response.status_code} {response.text[:200]}")
            return None
        except Exception as e:
            if self.logger:
                self.logger.warning(f"获取文件下载链接异常: {e}")
            return None

    def _handle_group_at_message(self, data: Dict[str, Any]):
        """处理群聊@消息"""
        if not self.message_handler:
            return
        
        content = data.get('content', '')
        group_openid = data.get('group_openid', '')
        author = data.get('author', {})
        member_openid = author.get('member_openid', '')
        # 实测（本机器人）：群聊事件 author 不含 user_openid 字段，
        # 且 member_openid 与私聊事件的 user_openid 值相同；
        # 仍保留 user_openid 优先读取，兼容平台未来行为变化（如事件携带 user_openid 或两套标识分离）
        unified_openid = author.get('user_openid') or member_openid
        user_name = author.get('username', '用户')
        msg_id = data.get('id', '')
        attachments = self._resolve_media(data, scope='group', openid=group_openid)
        
        message = {
            'type': 'group',
            'content': content,
            'group_openid': group_openid,
            'user_openid': member_openid,        # 群@提醒使用群成员标识
            'unified_openid': unified_openid,     # 统一的用户身份标识（用于上下文存取）
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
        attachments = self._resolve_media(data, scope='c2c', openid=user_openid)
        
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
        attachments = self._resolve_media(data, scope='c2c', openid=user_openid)
        
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
    
    # ---------- 心跳 ----------
    def _heartbeat_loop(self):
        """心跳线程 - 定时发送心跳，并检测连接假死（超时未收到任何服务器响应）"""
        while self.is_running:
            time.sleep(self.heartbeat_interval)
            if not (self.ws and self.ws.sock and self.ws.sock.connected):
                continue
            
            # 心跳超时检测：超过 3 个心跳周期未收到任何服务器数据，判定连接假死
            # （如网络静默丢包、半开连接），主动断开以触发重连逻辑
            timeout = max(self.heartbeat_interval * 3, 30)
            if self.last_heartbeat_ack and time.time() - self.last_heartbeat_ack > timeout:
                self.logger.warning(f"心跳超时（{timeout:.0f}秒未收到服务器响应），主动断开以触发重连")
                try:
                    self.ws.close()
                except Exception as e:
                    self.logger.error(f"主动断开连接失败: {e}")
                continue
            
            try:
                self.ws.send(json.dumps({"op": 1, "d": None}))
                self.logger.debug("发送心跳")
            except Exception as e:
                self.logger.error(f"发送心跳失败: {e}")
    
    # ---------- 连接与重连 ----------
    def connect(self):
        """连接WebSocket，含自动重连和会话恢复"""
        self.should_reconnect = True
        self._current_attempt = 0  # 当前尝试计数（鉴权成功前不会被重置）
        
        while self.should_reconnect and self._current_attempt < self.reconnect_attempts:
            self._current_attempt += 1
            token = self.get_access_token()
            if not token:
                self.logger.error(f"无法获取Access Token (尝试 {self._current_attempt}/{self.reconnect_attempts})")
                time.sleep(self.reconnect_interval)
                continue
            
            # 鉴权通过连接内的 Identify(op2) 包完成，token 不放入 URL，避免泄露到网关/代理日志
            ws_url = self._get_ws_gateway()
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
            
            # 如果未达到最大次数，等待间隔
            if self._current_attempt < self.reconnect_attempts:
                self.logger.info(f"将在 {self.reconnect_interval} 秒后重连...")
                time.sleep(self.reconnect_interval)
            else:
                self.logger.error("已达到最大重连次数，放弃连接")
                break
        
        return self._current_attempt < self.reconnect_attempts
    
    def _on_ws_open(self, ws):
        self.logger.info("WebSocket连接已建立")
        # 注意：鉴权在收到 Hello (op10) 后触发，不在这里直接发送
        # 重连计数不在此处重置，而是在鉴权成功（收到 READY）后重置，
        # 否则 AppID/Secret 无效导致鉴权失败时会陷入无限重连
    
    def _on_ws_error(self, ws, error):
        self.logger.error(f"WebSocket错误: {error}")
    
    def _on_ws_close(self, ws, close_status_code, close_msg):
        self.logger.info(f"WebSocket连接已关闭: {close_status_code} - {close_msg}")
        self.is_running = False
        self.last_heartbeat_ack = 0
        # 主动断开（disconnect）时不恢复会话；其余情况只要存在历史 session_id 就尝试 RESUME。
        # 即便 RESUME 被服务器拒绝，也会收到 Invalid Session(op9) 并自动回退到重新 IDENTIFY，
        # 因此无需按关闭错误码细分处理。
        if self.should_reconnect and self.session_id:
            self._should_resume = True
    
    def disconnect(self):
        """主动断开连接，阻止自动重连，并清除会话信息"""
        self.should_reconnect = False
        self.is_running = False
        # 主动断开时应清除 session 信息，因为下次连接是全新的
        self.session_id = None
        self.last_seq = 0
        self._should_resume = False
        if self.ws:
            self.ws.close()