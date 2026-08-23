#!/usr/bin/env python3
"""
QQ AI Bot - 主程序
连接QQ机器人与AI模型服务
"""

import sys
import os
import json
import time
import signal
import threading
import shutil
from datetime import datetime, timezone, timedelta
from typing import Dict, Any


def get_base_dir():
    """
    获取程序根目录：
    - 开发环境：返回当前文件所在目录
    - 打包后（PyInstaller）：返回 exe 所在目录
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


# 切换到根目录，确保相对路径（config.json, data/ 等）正确
BASE_DIR = get_base_dir()
os.chdir(BASE_DIR)

# 将根目录加入模块搜索路径，以便导入 core 包
sys.path.insert(0, BASE_DIR)

# 导入 core 模块
from core.config_manager import ConfigManager
from core.logger import Logger
from core.context_manager import ContextManager
from core.qq_client import QQClient
from core.ai_client import AIClient
from core.message_filter import MessageFilter
from core.message_processor import MessageProcessor
from core.file_handler import FileHandler
from core.web_admin import WebAdmin
from core.stats import StatsCollector
from core.plugin_manager import PluginManager, PluginBot


def _pid_is_alive(pid: int) -> bool:
    """判断进程是否存活（用于识别残留锁）。注意 Windows 上 os.kill(pid,0) 会杀进程，不能用。"""
    if not pid or pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True  # 查询失败时保守认为存活
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


class _InstanceLock:
    """单实例锁对象：持有期保证唯一，release() 释放（兼容文件锁与目录锁两种模式）"""

    def __init__(self, handle=None, lock_dir_path=None, pid_path=None):
        self.handle = handle                  # 文件锁句柄（fcntl/msvcrt 模式）
        self.lock_dir_path = lock_dir_path    # 目录锁路径（mkdir 模式）
        self.pid_path = pid_path              # 独立的 PID 记录文件

    def release(self):
        if self.handle is not None:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
        if self.lock_dir_path:
            try:
                os.remove(os.path.join(self.lock_dir_path, 'pid'))
            except Exception:
                pass
            try:
                os.rmdir(self.lock_dir_path)
            except Exception:
                pass
            self.lock_dir_path = None
        if self.pid_path:
            try:
                os.remove(self.pid_path)
            except Exception:
                pass
            self.pid_path = None


def _acquire_file_lock(lock_file):
    """文件锁（fcntl/msvcrt）。返回 True=加锁成功 / False=被其他实例占用 / None=文件系统不支持"""
    try:
        import fcntl  # Linux / macOS / Termux
    except ImportError:
        import msvcrt  # Windows
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False  # Windows 字节锁失败即被占用
    import errno
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False  # 确实被其他实例占用
        return None       # 其他错误：如 Android 共享存储(FUSE) 不支持 flock


def _acquire_mkdir_lock(lock_dir_path: str):
    """
    目录锁：用原子 mkdir 实现（兼容不支持 flock 的文件系统）。
    返回 'locked' / 'busy' / 'unsupported'；'busy' 时带残留检测（锁主进程已死则自动清理）。
    """
    try:
        os.mkdir(lock_dir_path)
    except FileExistsError:
        # 已存在：判断是否为残留（锁主进程已退出）
        pid = None
        try:
            with open(os.path.join(lock_dir_path, 'pid'), 'r') as f:
                pid = int(f.read().strip() or 0)
        except Exception:
            pid = None
        if pid and _pid_is_alive(pid):
            return 'busy'
        try:
            shutil.rmtree(lock_dir_path)  # 残留锁，清理后重试
        except Exception:
            return 'busy'
        try:
            os.mkdir(lock_dir_path)
        except OSError:
            return 'busy'
    except OSError:
        return 'unsupported'
    try:
        with open(os.path.join(lock_dir_path, 'pid'), 'w') as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return 'locked'


def _refuse_start(lock_path: str, pid_path: str):
    """输出拒绝启动提示（按平台给出关闭旧实例的命令）"""
    old_pid = None
    for path in (pid_path, lock_path):
        try:
            with open(path, 'r', encoding='utf-8') as pf:
                old_pid = pf.read().strip() or None
        except Exception:
            old_pid = None
        if old_pid:
            break
    if os.name == 'nt':
        close_hint = (f"请先在任务管理器结束旧进程，或执行：taskkill /F /PID {old_pid}"
                      if old_pid else "请先在任务管理器结束旧进程（PID 未知时按进程名 QQAIbot/python 查找）")
        delete_hint = "可删除残留锁文件后重试：data/bot.lock 与 data/bot.lockdir 目录（按实际存在删除）"
    else:
        close_hint = (f"请先关闭旧实例：kill {old_pid}（或 pkill -f qqbot.py）"
                      if old_pid else "请先关闭旧实例：pkill -f qqbot.py")
        delete_hint = "可删除残留锁文件后重试：data/bot.lock 与 data/bot.lockdir 目录（按实际存在删除）"
    print("=" * 60)
    if old_pid:
        print(f"⚠️  检测到机器人已在运行（PID={old_pid}）")
    else:
        print("⚠️  检测到机器人已在运行（PID=未知）")
    print("    为避免重复回复和上下文冲突，本实例拒绝启动。")
    print(f"    {close_hint}")
    print(f"    如确认没有实例在运行，{delete_hint}。")
    print("=" * 60)
    return None


def acquire_single_instance_lock(base_dir: str):
    """
    单实例保护（跨平台）：
      1) 文件锁（fcntl / msvcrt）——常规环境可靠；
      2) 目录锁（原子 mkdir）——文件系统不支持 flock 时降级（如 Android 共享存储）；
      3) 仍不可用时提示后继续启动（尽力而为，不阻塞）。
    被其他实例占用时返回 None（拒绝启动）；否则返回 _InstanceLock（需在退出时 release()）。
    """
    try:
        lock_dir = os.path.join(base_dir, 'data')
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, 'bot.lock')
        pid_path = os.path.join(lock_dir, 'bot.pid')

        # 方式一：文件锁
        lock_file = open(lock_path, 'a+', encoding='utf-8')
        result = _acquire_file_lock(lock_file)
        if result is True:
            try:
                with open(pid_path, 'w', encoding='utf-8') as pf:
                    pf.write(str(os.getpid()))
            except Exception:
                pass
            return _InstanceLock(handle=lock_file, pid_path=pid_path)
        lock_file.close()
        if result is False:
            return _refuse_start(lock_path, pid_path)

        # 方式二：目录锁（文件锁不受支持时）
        mk = _acquire_mkdir_lock(os.path.join(lock_dir, 'bot.lockdir'))
        if mk == 'locked':
            return _InstanceLock(lock_dir_path=os.path.join(lock_dir, 'bot.lockdir'))
        if mk == 'busy':
            return _refuse_start(lock_path, pid_path)

        # 都不可用：提示后继续（不阻塞启动）
        print("⚠️  当前文件系统不支持文件锁，单实例保护已跳过，请勿同时启动多个实例。")
        return _InstanceLock()
    except Exception as e:
        # 无法创建锁文件等异常情况：提示后继续启动（保护是尽力而为）
        print(f"⚠️  单实例保护不可用（{e}），继续启动。")
        return _InstanceLock()


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
        filter_config = dict(self.config.get('filter', {}))  # 拷贝一份，避免污染共享配置
        filter_config['filter_meaningless'] = self.config.get('message', {}).get('filter_meaningless', True)
        self.message_filter = MessageFilter(filter_config)
        
        # 插件管理器（plugins/ 目录，用户可自行添加插件扩展功能，无需改代码）
        plugin_dir = self.config.get('plugins', {}).get('dir', 'plugins') or 'plugins'
        self.plugin_manager = PluginManager(logger=self.log, plugin_dir=plugin_dir)
        if self.config.get('plugins', {}).get('enabled', True):
            self.plugin_manager.load_plugins()
        else:
            self.log.info("插件系统已禁用（config plugins.enabled=false）")
        # 注入 bot 上下文：插件可通过 on_message(msg, bot)/on_start(bot) 主动发消息、读配置、记日志
        self.plugin_manager.set_bot(PluginBot(
            qq_client=self.qq_client,
            config=self.config,
            logger=self.log,
        ))
        
        # 消息处理器配置
        processor_config = {
            'max_queue_size': self.config.get('message', {}).get('max_queue_size', 10),
            'max_segment_length': self.config.get('message', {}).get('max_segment_length', 2000),
            'system_prompt': self.config.get('system_prompt', '你是一个智能助手。'),
            'require_mention': self.config.get('group', {}).get('require_mention', True),
            'context_enabled': self.config.get('context', {}).get('enabled', True),
            'admin_openids': self.config.get('admin', {}).get('openids', []),
            'rate_limit': self.config.get('rate_limit', {}),
            'sensitive_words': self.config.get('sensitive_words', {}),
        }
        
        # 消息处理器
        self.stats = StatsCollector(logger=self.log)
        self.message_processor = MessageProcessor(
            qq_client=self.qq_client,
            ai_client=self.ai_client,
            context_manager=self.context_manager,
            message_filter=self.message_filter,
            file_handler=self.file_handler,
            logger=self.log,
            config=processor_config,
            stats=self.stats,
            plugin_manager=self.plugin_manager
        )
        
        # 设置QQ消息处理回调
        self.qq_client.on_message(self._handle_message)
        
        # 配置热更新：后台线程轮询 config.json，变化后自动热应用
        hot_reload = self.config.get('hot_reload', {})
        self._hot_reload_enabled = hot_reload.get('enabled', True)
        self._hot_reload_interval = max(1.0, float(hot_reload.get('interval_seconds', 10)))
        self._config_watcher_thread = None
        self._config_watcher_running = False
        self._last_config_mtime = self._get_config_mtime()
        
        # 错误告警配置
        alert_cfg = self.config.get('alert', {})
        self._alert_enabled = alert_cfg.get('enabled', True)
        self._alert_owner = alert_cfg.get('owner_openid', '')
        
        # 定时任务配置
        schedule_cfg = self.config.get('schedule', {})
        self._schedule_enabled = schedule_cfg.get('enabled', True)
        self._schedule_tasks = schedule_cfg.get('tasks', []) or []
        self._schedule_last_sent = {}      # 任务下标 -> 已发送日期
        self._schedule_thread = None
        self._schedule_running = False
        
        # Web 管理后台配置
        web_cfg = self.config.get('web_admin', {})
        self._web_admin = None
        self._web_admin_enabled = web_cfg.get('enabled', True)
        
        # 指令面板配置
        panel_cfg = self.config.get('command_panel', {})
        self._command_panel_enabled = panel_cfg.get('enabled', True)
        self._command_panel_commands = panel_cfg.get('commands', []) or []
        self._command_panel_c2c = panel_cfg.get('c2c', {}) or {}
        self._command_panel_group = panel_cfg.get('group', {}) or {}
    
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
        
        # 启动配置热更新监视线程
        if self._hot_reload_enabled:
            self._config_watcher_running = True
            self._config_watcher_thread = threading.Thread(target=self._config_watch_loop, daemon=True)
            self._config_watcher_thread.start()
            self.log.info(f"配置热更新已开启（每 {self._hot_reload_interval:.0f} 秒检查 config.json）")
        
        # 启动定时任务线程
        if self._schedule_enabled and self._schedule_tasks:
            self._schedule_running = True
            self._schedule_thread = threading.Thread(target=self._schedule_loop, daemon=True)
            self._schedule_thread.start()
            self.log.info(f"定时任务已开启（{len(self._schedule_tasks)} 个任务）")
        
        # 启动 Web 管理后台
        if self._web_admin_enabled:
            web_cfg = self.config.get('web_admin', {})
            try:
                self._web_admin = WebAdmin(
                    host=web_cfg.get('host', '127.0.0.1'),
                    port=web_cfg.get('port', 8666),
                    token=web_cfg.get('token', ''),
                    hub=self,
                    logger=self.log
                )
                self._web_admin.start()
            except Exception as e:
                self.log.error(f"Web管理后台启动失败: {e}")
                self._web_admin = None
        
        # 注册指令面板（必须在 connect() 之前调用——connect() 内部 run_forever 会阻塞主线程，
        # 放在其后永远不会执行。尽力而为，失败不影响运行）
        self._register_command_panel()
        
        self.log.info("连接QQ服务器...")
        success = self.qq_client.connect()
        
        if not success:
            self.log.error("连接QQ服务器失败")
            self._send_alert("机器人连接QQ服务器失败，请检查网络或凭据。")
            self.stop()
            return
        
        self.log.info("QQ AI Bot 运行中...")
    
    def stop(self):
        """停止程序"""
        self.log.info("正在停止...")
        # 停止配置热更新监视线程
        self._config_watcher_running = False
        if self._config_watcher_thread:
            self._config_watcher_thread.join(timeout=2)
        # 停止定时任务线程
        self._schedule_running = False
        if self._schedule_thread:
            self._schedule_thread.join(timeout=2)
        # 停止 Web 管理后台
        if self._web_admin:
            self._web_admin.stop()
            self._web_admin = None
        if hasattr(self, 'message_processor'):
            self.message_processor.stop()
        if hasattr(self, 'qq_client'):
            self.qq_client.disconnect()
        self.log.info("程序已停止")
    
    # ---------- 配置热更新 ----------
    def _get_config_mtime(self) -> float:
        """获取配置文件最后修改时间"""
        try:
            return os.path.getmtime(self.config_manager.CONFIG_FILE)
        except Exception:
            return 0
    
    def _config_watch_loop(self):
        """后台线程：轮询 config.json，检测到变化后热更新运行中的组件"""
        while self._config_watcher_running:
            time.sleep(self._hot_reload_interval)
            try:
                mtime = self._get_config_mtime()
                if mtime != self._last_config_mtime:
                    self._last_config_mtime = mtime
                    self._apply_hot_config()
            except Exception as e:
                self.log.error(f"配置热更新检查异常: {e}")
    
    def _apply_hot_config(self):
        """把 config.json 中的可热更新项应用到运行中的组件"""
        self.config_manager.reload()
        cfg = self.config_manager.config
        changed = []
        
        # AI 客户端（api_key / base_url / model）
        ai = self.config_manager.get_ai_config()
        if ai != self.ai_client.config:
            self.ai_client.config = ai
            self.ai_client.api_key = ai.get('api_key', '')
            self.ai_client.base_url = ai.get('base_url', '')
            self.ai_client.model = ai.get('model', 'gpt-3.5-turbo')
            changed.append('AI配置')
        
        # 消息处理器
        mp = self.message_processor
        sp = cfg.get('system_prompt', '你是一个智能助手。')
        if sp != mp.system_prompt:
            mp.system_prompt = sp
            changed.append('system_prompt')
        seg = cfg.get('message', {}).get('max_segment_length', 2000)
        if seg != mp.max_segment_length:
            mp.max_segment_length = seg
            changed.append('max_segment_length')
        ctx = cfg.get('context', {}).get('enabled', True)
        if ctx != mp.context_enabled:
            mp.context_enabled = ctx
            changed.append('context.enabled')
        req = cfg.get('group', {}).get('require_mention', True)
        if req != mp.require_mention:
            mp.require_mention = req
            changed.append('require_mention')
        
        # 消息过滤器（关键词、无意义过滤）——仅在确实变化时才记录
        mf = self.message_filter
        fc = cfg.get('filter', {})
        new_kw = (
            fc.get('exact_match_keywords', []),
            fc.get('fuzzy_match_keywords', []),
            fc.get('exact_match_responses', {}),
            fc.get('fuzzy_match_responses', {}),
        )
        old_kw = (
            mf.exact_match_keywords,
            mf.fuzzy_match_keywords,
            mf.exact_match_responses,
            mf.fuzzy_match_responses,
        )
        if new_kw != old_kw:
            (mf.exact_match_keywords, mf.fuzzy_match_keywords,
             mf.exact_match_responses, mf.fuzzy_match_responses) = new_kw
            changed.append('关键词过滤')
        flt = cfg.get('message', {}).get('filter_meaningless', True)
        if flt != mf.filter_meaningless:
            mf.filter_meaningless = flt
            changed.append('filter_meaningless')
        
        # 定时任务（热更新：保存后新任务立即生效）
        sc = cfg.get('schedule', {})
        new_tasks = sc.get('tasks', []) or []
        if new_tasks != self._schedule_tasks:
            self._schedule_tasks = new_tasks
            self._schedule_last_sent = {}  # 任务变化后重置去重记录
            changed.append('schedule.tasks')
        
        # 指令面板（热更新：指令/面板配置变化后重新注册）
        cp = cfg.get('command_panel', {})
        new_cmds = cp.get('commands', []) or []
        new_en = cp.get('enabled', True)
        new_c2c = cp.get('c2c', {}) or {}
        new_group = cp.get('group', {}) or {}
        if (new_cmds != self._command_panel_commands or new_en != self._command_panel_enabled
                or new_c2c != self._command_panel_c2c or new_group != self._command_panel_group):
            self._command_panel_commands = new_cmds
            self._command_panel_enabled = new_en
            self._command_panel_c2c = new_c2c
            self._command_panel_group = new_group
            changed.append('command_panel')
            self._register_command_panel()
        
        # 告警
        ac = cfg.get('alert', {})
        if ac.get('owner_openid', '') != self._alert_owner:
            self._alert_owner = ac.get('owner_openid', '')
            changed.append('alert')
        
        # 上下文
        mh = cfg.get('context', {}).get('max_history', 20)
        if mh != self.context_manager.MAX_HISTORY:
            self.context_manager.MAX_HISTORY = mh
            changed.append('context.max_history')
        
        # 回复限速 / 敏感词（热更新：直接同步到消息处理器）
        mp = getattr(self, 'message_processor', None)
        if mp is not None:
            rl = cfg.get('rate_limit', {}) or {}
            if rl.get('enabled', True) != mp.rate_limit_enabled or \
                    float(rl.get('interval_seconds', 3)) != mp.rate_limit_interval:
                mp.rate_limit_enabled = rl.get('enabled', True)
                mp.rate_limit_interval = max(0.5, float(rl.get('interval_seconds', 3)))
                changed.append('rate_limit')
            sw = cfg.get('sensitive_words', {}) or {}
            new_sw = [str(w).strip() for w in (sw.get('list') or []) if str(w).strip()]
            if (sw.get('enabled', False) != mp.sensitive_enabled or new_sw != mp.sensitive_words
                    or sw.get('replacement', '***') != mp.sensitive_replacement
                    or sw.get('block_input', False) != mp.sensitive_block_input):
                mp.sensitive_enabled = sw.get('enabled', False)
                mp.sensitive_words = new_sw
                mp.sensitive_replacement = sw.get('replacement', '***') or '***'
                mp.sensitive_block_input = sw.get('block_input', False)
                changed.append('sensitive_words')
        
        self.log.info(f"配置已热更新: {', '.join(changed) if changed else '无变化'}")
        return changed
    
    # ---------- 指令面板 ----------
    def _get_panel_ids(self) -> dict:
        """读取已创建的指令面板 ID（data/command_panel.json，c2c/group 各一）"""
        try:
            with open(os.path.join('data', 'command_panel.json'), encoding='utf-8') as f:
                return json.load(f) or {}
        except Exception:
            return {}
    
    def _save_panel_ids(self, panel_ids: dict):
        """持久化指令面板 ID，避免每次启动重复创建面板"""
        try:
            os.makedirs('data', exist_ok=True)
            with open(os.path.join('data', 'command_panel.json'), 'w', encoding='utf-8') as f:
                json.dump(panel_ids, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    
    def _register_command_panel(self):
        """向 QQ 注册/更新机器人指令面板（c2c 私聊面板 + group 群聊面板）"""
        if not self._command_panel_enabled:
            return
        try:
            items = []
            for c in self._command_panel_commands:
                if not c.get('name'):
                    continue
                if c.get('type') == 'link':
                    items.append({"type": "link", "name": c.get('name', ''), "link": c.get('link', '')})
                else:
                    items.append({"type": "command", "name": c.get('name', ''), "desc": c.get('desc', '')})
            if not items:
                return
            panel_ids = self._get_panel_ids()
            for scope, section, openids_key in (
                    ('c2c', self._command_panel_c2c, 'user_openids'),
                    ('group', self._command_panel_group, 'group_openids')):
                if not section.get('enabled', True):
                    continue
                target_type = section.get('target_type', 'all')
                openids = section.get(openids_key, []) or []
                remark = section.get('remark', '') or ''
                panel_id = panel_ids.get(scope)
                if not panel_id:
                    # 本地无缓存时先查官方列表，优先复用已存在的同 scope 面板（避免重复创建占额度）
                    try:
                        existing = self.qq_client.list_command_panels(scope)
                        if existing:
                            panel_id = existing[0].get('panel_id') or existing[0].get('id')
                            if panel_id:
                                panel_ids[scope] = panel_id
                                self._save_panel_ids(panel_ids)
                                if self.logger:
                                    self.logger.info(f"发现已有{scope}面板并复用: {panel_id}")
                    except Exception as e:
                        if self.logger:
                            self.logger.warning(f"查询已有{scope}面板失败: {e}")
                if panel_id:
                    ok = self.qq_client.update_command_panel(panel_id, scope, target_type, items, remark, openids)
                    if ok:
                        if self.logger:
                            self.logger.info(f"指令面板更新成功({scope})")
                        continue
                    # 更新失败：本地缓存的面板可能已被删除/失效，清除缓存并尝试创建
                    if self.logger:
                        self.logger.warning(f"指令面板更新失败({scope})，清除缓存并尝试重新创建")
                    panel_ids.pop(scope, None)
                    self._save_panel_ids(panel_ids)
                panel_id = self.qq_client.create_command_panel(scope, target_type, items, remark, openids)
                if panel_id:
                    panel_ids[scope] = panel_id
                    self._save_panel_ids(panel_ids)
                    if self.logger:
                        self.logger.info(f"指令面板创建成功({scope}) panel_id={panel_id}")
                elif self.logger:
                    self.logger.error(f"指令面板创建失败({scope})；若提示\"超出数量限制\"，"
                                      f"请到 Web 管理面板『指令面板管理』删除旧面板后点「重新注册面板」重试")
        except Exception as e:
            if self.logger:
                self.logger.error(f"指令面板注册异常: {e}")
    
    # ---------- 错误告警 ----------
    def _send_alert(self, text: str):
        """出错时私聊通知主人（config.alert.owner_openid）"""
        if not self._alert_enabled or not self._alert_owner:
            return
        try:
            msg = f"⚠️ {text}\n时间：{datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}"
            self.qq_client.send_message(self._alert_owner, msg)
        except Exception as e:
            self.log.error(f"告警发送失败: {e}")
    
    # ---------- 定时任务 ----------
    def _schedule_loop(self):
        """定时任务线程：按北京时间检查并执行到点任务"""
        tz = timezone(timedelta(hours=8))
        while self._schedule_running:
            try:
                self._run_schedule_tick(datetime.now(tz))
            except Exception as e:
                self.log.error(f"定时任务异常: {e}")
            time.sleep(30)
    
    def _run_schedule_tick(self, now: datetime):
        """执行一轮定时检查（可测试：传入任意时间）"""
        hm = now.strftime('%H:%M')
        today = now.strftime('%Y-%m-%d')
        for i, task in enumerate(self._schedule_tasks):
            if task.get('time') != hm:
                continue
            if self._schedule_last_sent.get(i) == today:
                continue  # 今天已执行过
            self._schedule_last_sent[i] = today
            self._execute_schedule_task(task)
    
    def _execute_schedule_task(self, task: Dict[str, Any]):
        """执行单个定时任务（向指定群/用户发送固定文本）"""
        target_type = task.get('target_type', 'group')
        target_id = task.get('target_id', '')
        content = task.get('content', '')
        if not target_id or not content:
            self.log.warning("定时任务缺少 target_id 或 content，已跳过")
            return
        try:
            if target_type == 'c2c':
                ok = self.qq_client.send_message(target_id, content)
            else:
                ok = self.qq_client.send_group_message(target_id, content)
            self.log.info(f"定时任务执行{'成功' if ok else '失败'}: {content[:30]}")
        except Exception as e:
            self.log.error(f"定时任务发送异常: {e}")


def main():
    """主函数"""
    # 单实例保护：检测到已有实例在运行时拒绝启动，避免重复回复与上下文冲突
    lock = acquire_single_instance_lock(BASE_DIR)
    if lock is None:
        return
    try:
        bot = QQAIbot()
        if bot.initialized:
            bot.run()
        else:
            print("初始化失败，请查看日志文件 data/logs/ 中的详细信息。")
    finally:
        lock.release()  # 释放单实例锁（含 PID 清理）


if __name__ == "__main__":
    main()