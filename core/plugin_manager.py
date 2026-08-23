"""
插件系统 - 让用户无需改源代码即可扩展机器人功能。

插件放在 plugins/ 目录，支持两种形态：
  1. 单文件插件：plugins/我的插件.py
  2. 多文件插件：plugins/我的插件/ 目录（入口为 __init__.py / main.py / 与目录同名 .py，
     其余 .py 为辅助模块，入口内用 "import 辅助模块名" 引用）

插件接口见 plugins/README.md。
支持两类插件：
1. 回复型：收到消息 → 返回要回复的文本（on_message(msg) 返回 str）
2. 连接型/桥接型：通过 bot 上下文主动发消息、启动后台线程
   （on_message(msg, bot) / on_start(bot) / on_stop(bot)）

启用/禁用状态保存在 data/plugins_disabled.json，重启后保持。
"""

import importlib.util
import inspect
import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional


class PluginBot:
    """注入给插件的 bot 上下文：让插件能主动发消息、读配置、记日志。

    插件内可用：
      bot.send_message(openid, text)        主动发私聊消息
      bot.send_group_message(group_openid, text)  主动发群消息
      bot.config                           当前配置（dict，只读建议）
      bot.log(msg) / bot.info / bot.error   写日志
      bot.qq_client                         底层 QQ 客户端（高级用法）
    """

    def __init__(self, qq_client=None, config: dict = None, logger=None):
        self.qq_client = qq_client
        self.config = config or {}
        self.logger = logger

    # ---- 发消息 ----
    def send_message(self, openid: str, content: str) -> bool:
        if self.qq_client is None:
            return False
        return self.qq_client.send_message(openid, content)

    def send_group_message(self, group_openid: str, content: str) -> bool:
        if self.qq_client is None:
            return False
        return self.qq_client.send_group_message(group_openid, content)

    # ---- 日志 ----
    def log(self, msg: str):
        if self.logger:
            self.logger.info(f"[插件] {msg}")

    def info(self, msg: str):
        if self.logger:
            self.logger.info(f"[插件] {msg}")

    def error(self, msg: str):
        if self.logger:
            self.logger.error(f"[插件] {msg}")


class PluginManager:
    """轻量插件管理器：扫描 plugins/ 目录加载插件（单文件/多文件），按顺序分发消息"""

    DEFAULT_DIR = 'plugins'
    DISABLED_FILE = os.path.join('data', 'plugins_disabled.json')

    def __init__(self, logger=None, plugin_dir: str = None):
        self.logger = logger
        self.plugin_dir = plugin_dir or self.DEFAULT_DIR
        self._lock = threading.Lock()
        self.plugins: List[Dict[str, Any]] = []   # 已加载的插件条目
        self.disabled: set = set()                # 被禁用的插件名（文件名或目录名）
        self.bot: Optional[PluginBot] = None      # 由外部注入（qqbot 启动时）
        self._load_disabled()

    # ---------- 日志 ----------
    def _log(self, level: str, msg: str):
        if self.logger:
            getattr(self.logger, level, print)(msg)

    # ---------- 禁用状态持久化 ----------
    def _load_disabled(self):
        try:
            if os.path.exists(self.DISABLED_FILE):
                with open(self.DISABLED_FILE, encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.disabled = set(str(x) for x in data)
        except Exception as e:
            self._log('warning', f"读取禁用插件列表失败: {e}")

    def _save_disabled(self):
        try:
            os.makedirs('data', exist_ok=True)
            with open(self.DISABLED_FILE, 'w', encoding='utf-8') as f:
                json.dump(sorted(self.disabled), f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log('warning', f"保存禁用插件列表失败: {e}")

    # ---------- 加载 ----------
    def load_plugins(self) -> List[Dict[str, Any]]:
        """扫描插件目录并加载所有插件（单文件 + 目录），返回插件条目列表

        目录不存在时自动创建（首次运行也能直接放插件）。
        """
        with self._lock:
            self.plugins = []
            if not os.path.isdir(self.plugin_dir):
                try:
                    os.makedirs(self.plugin_dir, exist_ok=True)
                    self._log('info', f"插件目录不存在，已自动创建: {self.plugin_dir}")
                except Exception as e:
                    self._log('error', f"创建插件目录失败: {self.plugin_dir} - {e}")
                    return self.plugins
            for name in sorted(os.listdir(self.plugin_dir)):
                path = os.path.join(self.plugin_dir, name)
                entry = None
                if os.path.isfile(path) and name.endswith('.py') and not name.startswith('_'):
                    entry = self._load_one(name[:-3], path, kind='file')
                elif os.path.isdir(path) and not name.startswith('_') and not name.startswith('.'):
                    entry = self._load_dir(name, path)
                if entry:
                    self.plugins.append(entry)
            disabled_count = len(self.disabled)
            if disabled_count:
                self._log('info', f"插件加载完成：共 {len(self.plugins)} 个（另有 {disabled_count} 个插件被禁用）")
            else:
                self._log('info', f"插件加载完成：共 {len(self.plugins)} 个")
            return self.plugins

    def _load_dir(self, dir_name: str, dir_path: str) -> Optional[Dict[str, Any]]:
        """加载目录型插件（多文件）。入口文件优先级：__init__.py > main.py > 与目录同名.py"""
        if dir_name in self.disabled:
            self._log('debug', f"插件已禁用，跳过: {dir_name}")
            return None
        # 找入口文件
        candidates = [
            os.path.join(dir_path, '__init__.py'),
            os.path.join(dir_path, 'main.py'),
            os.path.join(dir_path, dir_name + '.py'),
        ]
        entry_file = next((p for p in candidates if os.path.isfile(p)), None)
        if entry_file is None:
            # 兜底：目录里任意含 PLUGIN 定义或 on_message 的 .py
            for fn in sorted(os.listdir(dir_path)):
                if fn.endswith('.py') and not fn.startswith('_'):
                    entry_file = os.path.join(dir_path, fn)
                    break
        if entry_file is None:
            self._log('error', f"目录插件缺少入口文件: {dir_name}（需要 __init__.py 或 main.py）")
            return None
        # 把插件目录加入 sys.path，让入口内 "import 辅助模块" 可用
        if dir_path not in sys.path:
            sys.path.insert(0, dir_path)
        entry = self._load_one(dir_name, entry_file, kind='dir', base_dir=dir_path)
        return entry

    def _load_one(self, mod_name: str, path: str, kind: str = 'file',
                  base_dir: str = None) -> Optional[Dict[str, Any]]:
        """加载单个插件（文件或目录入口）。返回插件条目 dict；失败返回 None。"""
        if mod_name in self.disabled:
            self._log('debug', f"插件已禁用，跳过: {mod_name}")
            return None
        try:
            spec = importlib.util.spec_from_file_location(f"_qqbot_plugin_{mod_name}", path)
            if spec is None or spec.loader is None:
                self._log('error', f"插件加载失败(无法创建spec): {path}")
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        except Exception as e:
            self._log('error', f"插件加载失败: {mod_name} - {e}")
            return None

        meta = getattr(module, 'PLUGIN', None) or {}
        entry = {
            'name': mod_name,
            'kind': kind,                              # 'file' 单文件 / 'dir' 多文件目录
            'file': os.path.basename(path),
            'path': path,
            'dir': base_dir or os.path.dirname(path),
            'title': str(meta.get('name', mod_name)),
            'description': str(meta.get('description', '')),
            'version': str(meta.get('version', '1.0.0')),
            'author': str(meta.get('author', '')),
            'module': module,
            'commands': list(getattr(module, 'COMMANDS', None) or []),
            'keywords': list(getattr(module, 'KEYWORDS', None) or []),
            'match_fn': getattr(module, 'match', None),
            'handle_fn': getattr(module, 'on_message', None),
            'start_fn': getattr(module, 'on_start', None),
            'stop_fn': getattr(module, 'on_stop', None),
            'loaded_at': time.strftime('%H:%M:%S'),
        }
        if not entry['handle_fn'] and not entry['start_fn']:
            self._log('error', f"插件缺少 on_message 或 on_start 函数: {mod_name}（已跳过）")
            return None
        self._log('debug', f"插件已加载: {entry['title']} v{entry['version']} ({kind})")
        return entry

    def reload(self) -> List[Dict[str, Any]]:
        """重新加载全部插件（先通知旧插件停止，清空旧模块，再重新扫描）"""
        self._call_stop()
        with self._lock:
            for p in self.plugins:
                name = getattr(p.get('module'), '__name__', None)
                if name and name in sys.modules:
                    try:
                        del sys.modules[name]
                    except Exception:
                        pass
            self.plugins = []
        plugins = self.load_plugins()
        self._call_start()
        return plugins

    # ---------- bot 注入 ----------
    def set_bot(self, bot: Optional[PluginBot]):
        """注入 bot 上下文（qqbot 启动时调用）"""
        self.bot = bot
        if bot is not None:
            self._call_start()

    def _call_start(self):
        """调用所有插件的 on_start(bot)（启动后台线程/连接外部服务）"""
        if self.bot is None:
            return
        with self._lock:
            plugins = list(self.plugins)
        for p in plugins:
            fn = p.get('start_fn')
            if not fn:
                continue
            try:
                self._call_with_bot(fn, p, 'on_start')
            except Exception as e:
                self._log('error', f"插件 {p['title']} on_start 异常: {e}")

    def _call_stop(self):
        """调用所有插件的 on_stop(bot)（清理后台线程）"""
        if self.bot is None:
            return
        with self._lock:
            plugins = list(self.plugins)
        for p in plugins:
            fn = p.get('stop_fn')
            if not fn:
                continue
            try:
                self._call_with_bot(fn, p, 'on_stop')
            except Exception as e:
                self._log('error', f"插件 {p['title']} on_stop 异常: {e}")

    def _call_with_bot(self, fn, p: Dict[str, Any], label: str):
        """按函数签名调用：接收 bot 参数则传入，否则只传自身参数"""
        try:
            sig = inspect.signature(fn)
            params = [x for x in sig.parameters.values()
                      if x.kind in (x.POSITIONAL_ONLY, x.POSITIONAL_OR_KEYWORD)]
            if len(params) >= 1 and params[0].name in ('bot', 'ctx', 'context'):
                fn(self.bot)
            else:
                fn()
        except (TypeError, ValueError):
            try:
                fn()
            except Exception:
                raise

    # ---------- 分发 ----------
    def dispatch(self, msg: Dict[str, Any]) -> Optional[str]:
        """
        把消息交给插件处理。返回插件要回复的文本（str），
        或 None 表示没有插件处理/插件不回复（交给内置逻辑继续）。
        """
        with self._lock:
            plugins = list(self.plugins)
        for p in plugins:
            try:
                if not self._matches(p, msg):
                    continue
                result = self._call_handle(p, msg)
                if result is not None and str(result).strip():
                    self._log('debug', f"插件 {p['title']} 回复: {str(result)[:50]}")
                    return str(result)
                # 插件匹配了但返回空：视为不回复，继续下一个插件
            except Exception as e:
                self._log('error', f"插件 {p['title']} 执行异常: {e}")
        return None

    def _call_handle(self, p: Dict[str, Any], msg: Dict[str, Any]):
        """调用 on_message：签名含 bot 则传 (msg, bot)，否则只传 (msg)"""
        fn = p['handle_fn']
        try:
            sig = inspect.signature(fn)
            params = [x for x in sig.parameters.values()
                      if x.kind in (x.POSITIONAL_ONLY, x.POSITIONAL_OR_KEYWORD)]
            if len(params) >= 2:
                return fn(msg, self.bot)
            return fn(msg)
        except (TypeError, ValueError):
            return fn(msg)

    def _matches(self, p: Dict[str, Any], msg: Dict[str, Any]) -> bool:
        """判断插件是否匹配该消息（指令精确 / 关键词包含 / 自定义 match）"""
        content = (msg.get('content') or '').strip()
        # 1) 自定义 match 函数
        if p['match_fn'] is not None:
            try:
                if p['match_fn'](msg):
                    return True
            except Exception as e:
                self._log('error', f"插件 {p['title']} match 异常: {e}")
        # 2) COMMANDS：内容完全等于指令，或以指令开头（支持 " /天气 北京" 带参数）
        for cmd in p['commands']:
            cmd = str(cmd).strip()
            if not cmd:
                continue
            if content == cmd or content.startswith(cmd + ' ') or content.startswith(cmd + '\n'):
                return True
        # 3) KEYWORDS：内容包含关键词
        for kw in p['keywords']:
            kw = str(kw).strip()
            if kw and kw in content:
                return True
        return False

    # ---------- 启用/禁用 ----------
    def set_disabled(self, name: str, disabled: bool) -> bool:
        """暂存启用/禁用插件（按文件名或目录名）。

        只改内存中的"待应用"状态，不会立即重载；调用 apply_changes()
        后才真正生效（写盘 + 重载插件）。返回操作是否成功。
        """
        if disabled:
            self.disabled.add(name)
        else:
            self.disabled.discard(name)
        return True

    def apply_changes(self) -> int:
        """把暂存的启用/禁用状态真正应用：写盘 + 重载插件。

        返回加载后插件数量。
        """
        self._save_disabled()
        try:
            self.reload()
        except Exception as e:
            self._log('error', f"应用插件状态后重载失败: {e}")
        return len(self.plugins)

    def is_disabled(self, name: str) -> bool:
        return name in self.disabled

    # ---------- 状态 ----------
    def list_plugins(self) -> List[Dict[str, Any]]:
        """返回插件状态列表（不含模块对象，供 Web 展示）。

        包含已加载的插件和已被禁用的插件（禁用插件也能在后台重新启用）。
        """
        with self._lock:
            loaded = list(self.plugins)
            loaded_names = {p['name'] for p in loaded}
        result = [{
            'name': p['name'],
            'kind': p['kind'],
            'title': p['title'],
            'description': p['description'],
            'version': p['version'],
            'author': p['author'],
            'file': p['file'],
            'commands': p['commands'],
            'keywords': p['keywords'],
            'disabled': p['name'] in self.disabled,
            'loaded_at': p['loaded_at'],
        } for p in loaded]
        # 补上被禁用的插件（扫描目录，不真正加载代码，只提取文件名信息）
        if os.path.isdir(self.plugin_dir):
            for name in sorted(os.listdir(self.plugin_dir)):
                if name.startswith('_') or name.startswith('.'):
                    continue
                base = name[:-3] if name.endswith('.py') else name
                if base in loaded_names or base not in self.disabled:
                    continue
                path = os.path.join(self.plugin_dir, name)
                kind = 'dir' if os.path.isdir(path) else 'file'
                result.append({
                    'name': base,
                    'kind': kind,
                    'title': base,          # 未加载，无元信息，用文件名作标题
                    'description': '(已停用，点击「▶️ 启用」后加载)',
                    'version': '-',
                    'author': '-',
                    'file': name,
                    'commands': [],
                    'keywords': [],
                    'disabled': True,
                    'loaded_at': '-',
                })
        result.sort(key=lambda x: (x['disabled'], x['name']))
        return result
