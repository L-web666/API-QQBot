"""
云同步模块 - 把 data/ 下的用户数据同步到 Cloudflare D1（云数据库）
====================================================================
目的：机器人换服务器 / 重装系统后，用户上下文、身份绑定、统计、插件数据等
      可以直接从云端拉回来，不会丢用户数据。

同步内容（默认）：
    data/user_context/**         私聊与群聊上下文、bindings.json 身份绑定
    data/stats.json              统计数据
    data/plugins_data/**         插件自己的数据（如签到记录、抽奖配置）
    data/plugins_disabled.json   插件启停状态
    data/command_panel.json      指令面板缓存

永不同步（安全 / 无意义）：
    config.json、config配置说明文件.txt —— 含 AppSecret / API Key，绝不上云
    data/bot.lock、data/bot.pid        —— 运行期单实例锁与进程号
    data/logs/**                       —— 日志量大，默认不上云（可用 upload_logs 打开）
    data/.cloud_sync_state.json        —— 同步状态记录（本地文件）

工作机制：
    1) 启动时（pull_on_start=true）先从云端拉取：本地缺失的文件补回来，云端更新的覆盖本地；
    2) **冲突规则：同一条记录以时间戳更新的一方为准**——云端副本更新 → 用云端版本覆盖本地
       （并放弃本轮本地上传，避免本地旧副本把云端新数据覆盖掉）；本地副本更新 → 上传覆盖云端；
    3) **定时写入**：后台线程按 interval_seconds 周期同步（默认 60 秒；设成 300 就是每 5 分钟一次，
       间隔 ≥60 秒时会对齐到整点倍数，例如每 5 分钟落在 00/05/10 分）；
       每个周期只上传**发生改动**的文件，没有任何改动时该周期不会写数据库；
    4) 本地被删除的文件（如 /clear 清空上下文）会把云端对应记录一并删除，不会被拉回来；
    5) 退出前再做一次同步，保证最新数据已上云；
    6) 同一时间只应运行一台机器人（多台会互相覆盖，属于 Cloudflare D1 单写场景）。

配置见 config.json 的 cloud_sync 段（云同步默认关闭）。
"""

import os
import json
import socket
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests

from core.deferred_writes import (startup_buffer_flush, startup_buffer_mark_remote,
                                  startup_buffer_suspend)


D1_API = "https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{db}/query"
KEY_PREFIX = "data/"
INSTANCE_PREFIX = "system/instances/"
STATE_FILE = os.path.join("data", ".cloud_sync_state.json")

# 同一个文件连续失败多少次后：升级为 ERROR 并暂停云同步（避免一直撞同一个坏文件）
FAIL_THRESHOLD = 3


def _pid_alive(pid: int) -> bool:
    """尽力判断本机某个进程 pid 是否还在运行。

    用于清理"上一次运行 / 崩溃"留下的实例心跳（否则重启后会被误判成另一个实例）。
    判断不了时返回 True（保守：宁可多提示一次，也不要误删活实例的记录）。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True
    try:
        os.kill(pid, 0)          # POSIX：信号 0 只做存在性检查
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def test_connection(account_id: str, database_id: str, api_token: str,
                    timeout: int = 15) -> str:
    """测试 Cloudflare D1 凭据是否可用（供后台「测试连接」按钮调用）。

    成功返回 ''，失败返回错误说明。
    """
    backend = D1Backend(account_id, database_id, api_token, timeout=timeout)
    if not backend.configured():
        return '请先填写 Cloudflare 账户 ID、D1 数据库 ID 和 API 令牌'
    try:
        backend.test()
        return ''
    except Exception as e:
        return str(e)

# 单个 SQL 语句最多绑定的参数个数（D1 限制 100 个/语句），每行 3 个参数 → 每批 30 行
_BATCH_ROWS = 30
# 列 key 时分页拉取的行数
_PAGE_SIZE = 500


class CloudSyncError(Exception):
    """云同步相关错误"""


class D1Backend:
    """Cloudflare D1 后端（REST API，只用 requests，无需额外依赖）

    key -> 文本内容 的简单键值表（updated_at 为文件修改时间的毫秒值）：
        CREATE TABLE bot_data(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)
    """

    def __init__(self, account_id: str, database_id: str, api_token: str,
                 timeout: int = 30, logger=None):
        self.account_id = (account_id or '').strip()
        self.database_id = (database_id or '').strip()
        self.api_token = (api_token or '').strip()
        self.timeout = timeout
        self.logger = logger
        self._ready = False

    # ---------- 基础 ----------
    def configured(self) -> bool:
        return bool(self.account_id and self.database_id and self.api_token)

    def _url(self) -> str:
        return D1_API.format(account=self.account_id, db=self.database_id)

    def _query_full(self, sql: str, params: Optional[List[Any]] = None) -> Dict[str, Any]:
        """执行一条 SQL，返回 D1 的第一段结果（含 results 与 meta）"""
        if not self.configured():
            raise CloudSyncError("云同步未配置完整（account_id / database_id / api_token）")
        payload = {"sql": sql}
        if params:
            payload["params"] = params
        try:
            resp = requests.post(
                self._url(), json=payload, timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_token}",
                         "Content-Type": "application/json"})
        except requests.exceptions.RequestException as e:
            raise CloudSyncError(f"连接 Cloudflare 失败: {e}")
        if resp.status_code != 200:
            raise CloudSyncError(f"Cloudflare 返回 {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except Exception:
            raise CloudSyncError(f"Cloudflare 返回非 JSON: {resp.text[:200]}")
        if not data.get('success', False):
            errs = data.get('errors') or []
            msg = '; '.join(str(e.get('message', e)) for e in errs) if errs else str(data)[:200]
            raise CloudSyncError(f"D1 查询失败: {msg}")
        result = data.get('result') or []
        if isinstance(result, list) and result:
            return result[0] or {}
        return {}

    def _query(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        """执行一条 SQL，返回结果行列表"""
        return self._query_full(sql, params).get('results') or []

    # ---------- 建表 / 升级 ----------
    def ensure_schema(self):
        if self._ready:
            return
        # deleted: 0=正常文件  1=删除标记（墓碑，value 为空，只表示"这个 key 在某时刻被删了"）
        self._query("CREATE TABLE IF NOT EXISTS bot_data ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                    "updated_at INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)")
        # 老版本（没有 deleted 列）自动升级；列已存在时报 duplicate column，忽略即可
        try:
            self._query("ALTER TABLE bot_data ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        except CloudSyncError as e:
            if 'duplicate column' not in str(e).lower():
                raise
        self._ready = True

    # ---------- 连接测试 ----------
    def test(self) -> bool:
        """测试令牌与数据库是否可用（成功返回 True，失败抛 CloudSyncError）"""
        rows = self._query("SELECT 1 AS ok")
        if not rows:
            raise CloudSyncError("查询没有返回结果，请确认 D1 数据库已创建")
        self.ensure_schema()      # 顺便确认有建表 / 改表权限
        return True

    # ---------- 读写 ----------
    def list_entries(self, prefix: str = KEY_PREFIX) -> Dict[str, Tuple[int, bool]]:
        """列出所有 key → (更新时间, 是否删除标记)。分页拉取。"""
        self.ensure_schema()
        out: Dict[str, Tuple[int, bool]] = {}
        offset = 0
        while True:
            rows = self._query(
                "SELECT key, updated_at, deleted FROM bot_data WHERE key LIKE ? "
                "ORDER BY key LIMIT ? OFFSET ?",
                [prefix + '%', _PAGE_SIZE, offset])
            for r in rows:
                try:
                    out[str(r['key'])] = (int(r.get('updated_at') or 0),
                                          bool(int(r.get('deleted') or 0)))
                except Exception:
                    continue
            if len(rows) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
        return out

    def list_keys(self, prefix: str = KEY_PREFIX) -> Dict[str, int]:
        """只要 key → 更新时间（调试/兼容用；含删除标记）"""
        return {k: ts for k, (ts, _d) in self.list_entries(prefix).items()}

    def get(self, key: str) -> Optional[str]:
        """取文件内容（删除标记返回 None）"""
        self.ensure_schema()
        rows = self._query("SELECT value, deleted FROM bot_data WHERE key = ?", [key])
        if not rows:
            return None
        if int(rows[0].get('deleted') or 0):
            return None                      # 墓碑：不是文件
        return rows[0].get('value')

    def put_many(self, items: List[Tuple[str, str, int]]):
        """批量写入/更新文件（deleted=0，即"复活"同名墓碑）"""
        if not items:
            return
        self.ensure_schema()
        for i in range(0, len(items), _BATCH_ROWS):
            chunk = items[i:i + _BATCH_ROWS]
            placeholders = ','.join(['(?,?,?,0)'] * len(chunk))
            params: List[Any] = []
            for key, value, ts in chunk:
                params.extend([key, value, int(ts)])
            self._query(f"INSERT OR REPLACE INTO bot_data (key, value, updated_at, deleted) "
                        f"VALUES {placeholders}", params)

    def put_tombstone(self, key: str, ts: int):
        """写入删除标记（墓碑）：value 留空、deleted=1，updated_at=删除时间"""
        self.ensure_schema()
        self._query("INSERT OR REPLACE INTO bot_data (key, value, updated_at, deleted) "
                    "VALUES (?,'',?,1)", [key, int(ts)])

    def delete(self, key: str):
        """彻底删除该行（正常同步流程不再使用，保留给人工维护）"""
        self.ensure_schema()
        self._query("DELETE FROM bot_data WHERE key = ?", [key])

    def gc_tombstones(self, before_ms: int) -> int:
        """清理早于 before_ms 的删除标记，返回清理条数（-1 表示无法得知）"""
        self.ensure_schema()
        full = self._query_full("DELETE FROM bot_data WHERE deleted = 1 AND updated_at < ?",
                                [int(before_ms)])
        meta = full.get('meta') or {}
        for k in ('changes', 'rows_written'):
            if isinstance(meta.get(k), int):
                return meta[k]
        return -1

    def delete_instances(self, before_ms: int) -> int:
        """清理早于 before_ms 的实例心跳记录"""
        self.ensure_schema()
        full = self._query_full("DELETE FROM bot_data WHERE key LIKE ? AND updated_at < ?",
                                [INSTANCE_PREFIX + '%', int(before_ms)])
        meta = full.get('meta') or {}
        for k in ('changes', 'rows_written'):
            if isinstance(meta.get(k), int):
                return meta[k]
        return -1


class CloudSync:
    """云同步引擎：本地 data/ 目录 <-> 云数据库"""
    def __init__(self, backend, root: str = "data", logger=None,
                 interval: int = 60, pull_on_start: bool = True,
                 upload_logs: bool = False, max_file_mb: float = 2.0,
                 tombstone_days: int = 30, apply_remote_deletes: bool = True,
                 error_pause_minutes: float = 30.0):
        self.backend = backend
        self.root = root.rstrip('/\\') or 'data'
        self.logger = logger
        self.interval = max(10, int(interval or 60))
        self.pull_on_start = bool(pull_on_start)
        self.upload_logs = bool(upload_logs)
        self.max_file_bytes = int(float(max_file_mb or 2.0) * 1024 * 1024)
        # 删除墓碑：保留多少天（0=永久保留，不清理）；云端删除标记是否应用到本地
        self.tombstone_days = max(0, int(tombstone_days or 0))
        self.apply_remote_deletes = bool(apply_remote_deletes)
        # 同一文件连续失败 FAIL_THRESHOLD 次后暂停同步；0=不自动恢复（需手动或重启）
        self.error_pause_minutes = max(0.0, float(error_pause_minutes or 0))
        self._fail_counts: Dict[str, int] = {}     # key -> 连续失败次数
        self._paused_until = 0.0                   # 暂停到期时间（epoch 秒；inf=只有手动才恢复）
        self._pause_reason = ''
        self._last_gc = 0.0
        self._warned_multi = False
        self._started_at = int(time.time())
        # 是否已完成首次同步（启动时用它提示"刚启动数据可能不是最新的"）
        self.first_sync_done = False
        # 累计统计（程序退出时汇报；每个周期的明细见日志文件的 DEBUG 行）
        self._totals: Dict[str, int] = {'uploaded': 0, 'downloaded': 0, 'deleted': 0,
                                       'applied_remote_deletes': 0, 'revived': 0,
                                       'errors': 0, 'cycles': 0}
        self._state: Dict[str, List[int]] = {}     # key -> [mtime, size]
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Dict[str, Any] = {}
        self._load_state()

    # ---------- 日志 ----------
    def _log(self, level: str, msg: str):
        if not self.logger:
            return
        try:
            getattr(self.logger, level, self.logger.info)(f"[云同步] {msg}")
        except Exception:
            pass

    # ---------- 状态文件 ----------
    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._state = {str(k): list(v) for k, v in data.items()
                                   if isinstance(v, (list, tuple)) and len(v) >= 2}
        except Exception as e:
            self._log('warning', f"读取同步状态失败: {e}")
            self._state = {}

    def _save_state(self):
        try:
            with self._state_lock:
                snapshot = dict(self._state)
            os.makedirs('data', exist_ok=True)
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(snapshot, f, ensure_ascii=False)
        except Exception as e:
            self._log('warning', f"保存同步状态失败: {e}")

    # ---------- 文件筛选 ----------
    def _excluded(self, rel: str) -> bool:
        """rel 为相对 data/ 的路径（用 / 分隔）"""
        low = rel.replace('\\', '/')
        if low in ('bot.lock', 'bot.pid', '.cloud_sync_state.json'):
            return True
        if low.startswith('bot.lockdir') or low.startswith('.cloud_sync_state'):
            return True
        if '_test' in low or low.endswith('.tmp') or low.endswith('.part'):
            return True
        if low.startswith('logs/') and not self.upload_logs:
            return True
        return False

    def _iter_files(self) -> List[str]:
        """返回可同步的相对路径列表（相对 data/）"""
        out = []
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.root).replace('\\', '/')
                if self._excluded(rel):
                    continue
                out.append(rel)
        return out

    def _full(self, rel: str) -> str:
        return os.path.join(self.root, rel.replace('/', os.sep))

    def _key(self, rel: str) -> str:
        return KEY_PREFIX + rel

    def _rel(self, key: str) -> str:
        return key[len(KEY_PREFIX):] if key.startswith(KEY_PREFIX) else key

    @staticmethod
    def _mtime_ms(full: str) -> int:
        """文件修改时间（毫秒）。用毫秒是为了精确比较，避免同一秒内的修改被漏掉"""
        try:
            return int(os.stat(full).st_mtime * 1000)
        except OSError:
            return 0

    # ---------- 失败计数 / 暂停同步 ----------
    def is_paused(self) -> bool:
        """当前是否处于暂停状态（暂停期结束会自动恢复并重置失败计数）"""
        if not self._paused_until:
            return False
        if self._paused_until == float('inf'):
            return True
        if self._paused_until <= time.time():
            self._paused_until = 0.0
            self._pause_reason = ''
            self._fail_counts.clear()
            self._log('info', '云同步暂停期已结束，恢复同步（失败计数已重置）')
            return False
        return True

    def pause_info(self) -> Dict[str, Any]:
        """暂停状态（供后台状态页显示）"""
        paused = self.is_paused()
        left = 0
        if paused and self._paused_until != float('inf'):
            left = max(0, int(self._paused_until - time.time()))
        fails = sorted(self._fail_counts.items(), key=lambda x: -x[1])[:5]
        return {
            'paused': paused,
            'reason': self._pause_reason if paused else '',
            'resume_in_seconds': left,
            'resume_text': ('%d 分 %d 秒后自动重试' % (left // 60, left % 60)) if left
                           else ('需手动恢复（后台点「⬆️ 立即同步一次」或重启程序）' if paused else ''),
            'fail_files': ['%s（连续 %d 次）' % (self._rel(k), v) for k, v in fails],
        }

    def _pause(self, reason: str):
        """达到失败阈值：ERROR 告警 + 暂停同步"""
        if self.error_pause_minutes > 0:
            self._paused_until = time.time() + self.error_pause_minutes * 60
            tip = ('将在 %.0f 分钟后自动重试' % self.error_pause_minutes)
        else:
            self._paused_until = float('inf')
            tip = '需在后台点「⬆️ 立即同步一次」或重启程序后恢复'
        self._pause_reason = reason
        self._totals['pauses'] = self._totals.get('pauses', 0) + 1
        self._log('error', f"云同步已暂停（{reason}）——{tip}；"
                           f"其它数据不再同步，处理完（或等自动重试）后会自动继续")
        # 暂停意味着短时间内不会再同步：启动期间缓存的修改先落盘，不让数据一直悬在内存里
        startup_buffer_flush('云同步已暂停（%s）' % reason, warning=True)

    def resume(self, why: str = ''):
        """解除暂停并重置失败计数"""
        if self._paused_until or self._fail_counts:
            self._paused_until = 0.0
            self._pause_reason = ''
            self._fail_counts.clear()
            self._log('info', '云同步已恢复' + (f'（{why}）' if why else ''))

    def _bump_failures(self, keys) -> int:
        """累加这批 key 的连续失败次数，返回其中的最大值"""
        mx = 0
        for k in keys:
            n = self._fail_counts.get(k, 0) + 1
            self._fail_counts[k] = n
            mx = max(mx, n)
        return mx

    def _note_failure(self, key: str, action: str, err: str):
        """单个文件失败：第 1~2 次 WARNING，达到阈值升级 ERROR 并暂停同步"""
        n = self._bump_failures([key])
        rel = self._rel(key)
        if n >= FAIL_THRESHOLD:
            self._pause(f"{rel} 连续 {n} 次{action}失败：{err}")
        else:
            self._log('warning', f"{rel} {action}失败（第 {n}/{FAIL_THRESHOLD} 次，"
                                 f"下个周期自动重试）: {err}")

    def _note_success(self, keys):
        """这批 key 成功 → 清掉失败计数（恢复时提示一次）"""
        recovered = [k for k in keys if self._fail_counts.pop(k, None)]
        if recovered:
            self._log('info', f"{len(recovered)} 个之前失败的文件已恢复正常")

    # ---------- 同步 ----------
    def sync_once(self, force: bool = False) -> Dict[str, Any]:
        """执行一次增量同步（对外入口）。

        同步期间会临时挂起"启动写缓存"（core/deferred_writes.py）：
        云同步必须看到磁盘上的真实内容——否则那些还留在内存里的启动期改动
        会被当成"本地已改动"上传，反而把云端数据覆盖掉。
        """
        with startup_buffer_suspend():
            return self._sync_once_impl(force)

    def _sync_once_impl(self, force: bool = False) -> Dict[str, Any]:
        """执行一次增量同步。

        force=True（后台手动「立即同步一次」）：即使处于暂停状态也强制试一次，
        并重置失败计数；成功则解除暂停。
        """
        if not force and self.is_paused():
            self._log('debug', f"云同步暂停中，跳过本轮：{self._pause_reason}")
            self.last_result = {'paused': True, 'reason': self._pause_reason,
                                'uploaded': 0, 'downloaded': 0, 'deleted': 0,
                                'skipped': 0, 'applied_remote_deletes': 0,
                                'revived': 0, 'errors': 0, 'time': int(time.time())}
            return self.last_result
        was_paused = self.is_paused()
        if force and was_paused:
            self._log('info', '收到手动同步请求：临时解除暂停并重试一次')
            self._paused_until = 0.0
            self._pause_reason = ''
            self._fail_counts.clear()
        if not getattr(self.backend, 'configured', lambda: False)():
            raise CloudSyncError("云同步未配置（缺少 account_id / database_id / api_token）")
        entries = self.backend.list_entries(KEY_PREFIX)     # key -> (updated_at, 是否墓碑)
        uploaded = downloaded = deleted = skipped = 0
        applied_remote = revived = errors = 0
        handled = set()          # 本轮已由"云端删除标记"处理过的 key，避免再写一次墓碑
        revive = set()           # 本地比墓碑新 → 需要强制上传，把墓碑改回正常文件

        # ---------- A) 云端删除标记（墓碑）→ 应用到本地 ----------
        # 只有"墓碑比本地文件新"才删本地；本地文件更新的情况下保留本地并在 C 阶段复活它。
        # 这一步让"删除"能在多台机器之间正确传播，也让新服务器不会把已删除的数据拉回来。
        if self.apply_remote_deletes:
            for key, (ts, is_del) in entries.items():
                if not is_del or not key.startswith(KEY_PREFIX):
                    continue
                rel = self._rel(key)
                if self._excluded(rel):
                    continue
                handled.add(key)
                full = self._full(rel)
                if not os.path.exists(full):
                    with self._state_lock:
                        self._state.pop(key, None)     # 本地本来就没有，清掉状态即可
                    continue
                if int(ts) > self._mtime_ms(full):
                    try:
                        os.remove(full)
                        with self._state_lock:
                            self._state.pop(key, None)
                        applied_remote += 1
                        # 这个文件以云端为准（云端已删除）→ 启动期间对它的本地修改作废
                        startup_buffer_mark_remote(rel)
                        # 明细只写日志文件（DEBUG），控制台每轮只出一行汇总
                        self._log('debug', f"云端已有删除标记，同步删除本地文件: {rel}")
                    except OSError as e:
                        errors += 1
                        self._note_failure(key, '删除本地', str(e))
                else:
                    # 本地文件比删除更新（删除后又改过）→ 保留本地，稍后重新上传
                    revive.add(key)
                    self._log('debug', f"本地 {rel} 比云端的删除标记更新，保留并重新上传")
        else:
            for key, (ts, is_del) in entries.items():
                if is_del:
                    # 不删除本地文件，但也绝不上传/下载它，避免互相打架
                    handled.add(key)

        # ---------- B) 本地已删除 → 写删除标记（墓碑），不再硬删云端行 ----------
        #    必须放在下载之前：否则刚删掉的文件会立刻被云端旧数据拉回来。
        with self._state_lock:
            known_keys = list(self._state.keys())
        for key in known_keys:
            if key in handled or not key.startswith(KEY_PREFIX):
                continue
            rel = self._rel(key)
            if self._excluded(rel) or os.path.exists(self._full(rel)):
                continue
            try:
                now_ms = int(time.time() * 1000)      # 用当前时间：保证比任何旧副本新
                self.backend.put_tombstone(key, now_ms)
                entries[key] = (now_ms, True)
                with self._state_lock:
                    self._state.pop(key, None)
                deleted += 1
                # 明细只写日志文件（DEBUG）；控制台在周期结束只输出"删除标记 N 个"
                self._log('debug', f"本地已删除 {rel}，云端写入删除标记"
                                   f"（其它服务器/新机器不会再恢复它）")
            except CloudSyncError as e:
                errors += 1
                self._note_failure(key, '写入删除标记', str(e))

        # ---------- C) 本地 → 云端（有改动才传；墓碑 + 本地更新 → 复活） ----------
        local_keys = set()
        to_put: List[Tuple[str, str, int]] = []
        on_disk_size: Dict[str, int] = {}     # 记录"磁盘实际字节数"，作为下次比对依据
        cloud_newer: List[str] = []           # 云端副本更新的文件（本轮不上传，交给 D 阶段覆盖本地）
        for rel in self._iter_files():
            full = self._full(rel)
            key = self._key(rel)
            local_keys.add(key)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size > self.max_file_bytes:
                skipped += 1
                self._log('debug', f"{rel} 超过大小上限，跳过（{st.st_size} 字节）")
                continue
            mtime_ms = int(st.st_mtime * 1000)
            with self._state_lock:
                known = self._state.get(key)
            # 同一份记录：以时间戳更新的一方为准。
            # 云端是正常文件（不是墓碑）且比本地新 → 不上传本地（否则会用旧内容覆盖云端），
            # 交给后面的 D 阶段把云端的新版本拉下来。这一步很关键：
            # 新机器/同步状态丢失时，本地可能放着很旧的副本，旧逻辑会先把它传上云，
            # 于是"云端的较新数据"被本地旧数据覆盖掉了。
            cloud = entries.get(key)
            if cloud is not None and not cloud[1] and int(cloud[0]) > mtime_ms and key not in revive:
                cloud_newer.append(rel)
                self._log('debug', f"{rel} 云端副本更新（云端 {int(cloud[0])} > 本地 {mtime_ms}），"
                                   f"本轮不上传，改用云端版本")
                continue
            # 注意：比较的是"磁盘字节数"（st.st_size）。Windows 文本模式会把 \n 写成 \r\n，
            # 用"解码后内容的长度"记录会导致大小永远对不上、每个周期都重复上传（已修复）。
            unchanged = bool(known and int(known[0]) == mtime_ms
                             and int(known[1]) == int(st.st_size))
            if unchanged and key not in revive:
                continue
            try:
                with open(full, encoding='utf-8') as f:
                    content = f.read()
            except (UnicodeDecodeError, OSError) as e:
                skipped += 1
                self._log('debug', f"{rel} 不是文本或读取失败，跳过（{e}）")
                continue
            to_put.append((key, content, mtime_ms))
            on_disk_size[key] = int(st.st_size)
            if key in revive:
                revived += 1
        if cloud_newer:
            # 这是"以云端为准"的数据安全动作，明确提示一次（下个周期本地已是最新，不会再提示）
            self._log('warning', f"有 {len(cloud_newer)} 个文件的云端版本比本地新，"
                                 f"本轮不上传本地副本、改用云端版本："
                                 + "、".join(cloud_newer[:8]) + ("…" if len(cloud_newer) > 8 else ""))
        if to_put:
            try:
                self.backend.put_many(to_put)         # deleted=0 → 覆盖同名墓碑
            except CloudSyncError as e:
                # 上传失败：整批都没成功 → 给批内每个文件记一次失败；控制台只报一行
                errors += 1
                keys = [k for k, _c, _t in to_put]
                n = self._bump_failures(keys)
                if n >= FAIL_THRESHOLD:
                    self._pause(f"{self._rel(keys[0])} 等 {len(keys)} 个文件连续 {n} 次上传失败：{e}")
                else:
                    self._log('warning', f"上传失败（第 {n}/{FAIL_THRESHOLD} 次，本轮 {len(keys)} 个文件"
                                         f"未上传，下个周期自动重试）: {e}")
                self._log('debug', "上传失败的文件："
                                   + "、".join(self._rel(k) for k in keys))
                to_put = []
            if to_put:
                with self._state_lock:
                    for key, _content, ts in to_put:
                        self._state[key] = [ts, on_disk_size.get(key, len(_content.encode('utf-8')))]
                uploaded = len(to_put)
                self._note_success([k for k, _c, _t in to_put])
                self._log('debug', f"已上传 {uploaded} 个文件到云端"
                                   + (f"（其中 {revived} 个覆盖了旧的删除标记）" if revived else "")
                                   + "：" + "、".join(k[len(KEY_PREFIX):] for k, _c, _t in to_put))

        # ---------- D) 云端 → 本地（只下载正常文件；墓碑跳过 → 不会"复活"） ----------
        for key, (cloud_ts, is_del) in entries.items():
            if is_del:
                continue
            rel = self._rel(key)
            if self._excluded(rel):
                continue
            full = self._full(rel)
            need = False
            if not os.path.exists(full):
                need = True
            else:
                local_ms = self._mtime_ms(full)
                if int(cloud_ts) > local_ms:      # 云端更新（毫秒级比较）
                    need = True
            if not need:
                continue
            # 下载/写入失败：控制台明确告警（WARNING），跳过这个文件、不影响其它文件
            try:
                content = self.backend.get(key)
            except CloudSyncError as e:
                errors += 1
                self._note_failure(key, '下载', str(e))
                continue
            if content is None:
                continue
            try:
                os.makedirs(os.path.dirname(full) or '.', exist_ok=True)
                # newline='\n'：原样写入，不做 \n → \r\n 转换，保证与云端内容逐字节一致
                with open(full, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(content)
                try:
                    os.utime(full, (time.time(), int(cloud_ts) / 1000.0))
                except Exception:
                    pass
            except OSError as e:
                errors += 1
                self._note_failure(key, '写入本地', f"{e}（检查磁盘空间/权限）")
                continue
            with self._state_lock:
                try:
                    size = os.path.getsize(full)      # 以磁盘实际大小为准
                except OSError:
                    size = len(content.encode('utf-8'))
                self._state[key] = [int(cloud_ts), size]
            downloaded += 1
            self._note_success([key])
            # 这个文件以云端为准（刚用云端内容覆盖了本地）→ 启动期间对它的本地修改作废
            startup_buffer_mark_remote(rel)
            # 明细只写日志文件（DEBUG）；换服务器首次恢复很多文件时控制台不会刷屏
            self._log('debug', f"已从云端恢复 {rel}")

        # ---------- E) 清理过期删除标记（默认保留 30 天）+ 实例心跳 ----------
        self._maybe_gc_tombstones()
        self._heartbeat()

        self._save_state()
        self.last_result = {'uploaded': uploaded, 'downloaded': downloaded,
                            'deleted': deleted, 'skipped': skipped,
                            'applied_remote_deletes': applied_remote,
                            'revived': revived, 'errors': errors,
                            'cloud_newer': len(cloud_newer),
                            'time': int(time.time())}
        # 累计到本次运行的统计（退出时汇报）
        for _k in ('uploaded', 'downloaded', 'deleted', 'applied_remote_deletes',
                   'revived', 'errors', 'cloud_newer'):
            self._totals[_k] = self._totals.get(_k, 0) + int(self.last_result.get(_k, 0))
        self._totals['cycles'] = self._totals.get('cycles', 0) + 1
        # 首次同步完成 → 在控制台明确告知用户（这是"数据已对齐"的重要节点）
        if not self.first_sync_done:
            self.first_sync_done = True
            self._log_first_sync_done(downloaded, uploaded, deleted, errors)
            # 云端数据已经落盘 → 把启动期间缓存的本地修改写回磁盘
            # （默认"以云端为准"：刚被本次同步恢复的文件放弃本地改动，避免覆盖云端数据）
            startup_buffer_flush('第一次云同步已完成')
        # 暂停期间的手动重试成功 → 明确提示已恢复（状态在重试前已清空）
        if was_paused and not errors:
            self._log('info', '云同步已恢复（手动重试成功，失败计数已重置）')
        self._log_summary()
        return self.last_result

    def _log_first_sync_done(self, downloaded: int, uploaded: int, deleted: int, errors: int):
        """第一次同步完成时的提示（控制台可见，INFO）"""
        detail = []
        if downloaded:
            detail.append(f"从云端恢复 {downloaded} 个文件")
        if uploaded:
            detail.append(f"上传 {uploaded} 个文件")
        if deleted:
            detail.append(f"写入 {deleted} 个删除标记")
        if not detail:
            # 有失败时不能写"没有差异"（会让人误以为一切正常）
            detail.append("本轮同步已跑完（有文件失败，详见上面的告警）" if errors
                          else "本地与云端本来就没有差异")
        took = max(0, int(time.time() - self._started_at))
        extra = (f"；另有 {errors} 个文件本次失败，下个周期会自动重试" if errors else "")
        self._log('info', "✅ 第一次同步已完成：%s，用时 %d 秒%s——从现在起数据会与云端保持一致"
                          % ('、'.join(detail), took, extra))

    def _log_summary(self):
        """周期汇总只写日志文件（DEBUG）。

        控制台仅保留：失败告警（WARNING）与程序退出时的累计统计（INFO），
        避免刷屏；需要看明细时把后台日志级别切到 DEBUG。
        """
        r = self.last_result or {}
        parts = []
        if r.get('uploaded'):
            parts.append(f"上传 {r['uploaded']} 个")
        if r.get('downloaded'):
            parts.append(f"恢复 {r['downloaded']} 个")
        if r.get('deleted'):
            parts.append(f"写入删除标记 {r['deleted']} 个")
        if r.get('applied_remote_deletes'):
            parts.append(f"按云端标记删除本地 {r['applied_remote_deletes']} 个")
        if r.get('revived'):
            parts.append(f"复活 {r['revived']} 个")
        if r.get('cloud_newer'):
            parts.append(f"云端较新 {r['cloud_newer']} 个（本地未上传，改用云端版本）")
        if r.get('errors'):
            parts.append(f"失败 {r['errors']} 个（详见上面的告警）")
        if parts:
            self._log('debug', "本轮同步：" + "、".join(parts))

    def totals_text(self) -> str:
        """本次运行累计统计（程序退出时输出到控制台）"""
        t = self._totals
        return ("上传 %d 个、恢复 %d 个、写入删除标记 %d 个、按云端标记删除本地 %d 个、"
                "复活 %d 个、云端较新改用云端 %d 个、失败 %d 次、暂停 %d 次（共 %d 轮同步）"
                % (t.get('uploaded', 0), t.get('downloaded', 0), t.get('deleted', 0),
                   t.get('applied_remote_deletes', 0), t.get('revived', 0),
                   t.get('cloud_newer', 0),
                   t.get('errors', 0), t.get('pauses', 0), t.get('cycles', 0)))

    def pull_all(self) -> Dict[str, Any]:
        """启动时调用：把云端数据拉到本地（等同一次完整同步）"""
        return self.sync_once()

    def _maybe_gc_tombstones(self, force: bool = False) -> int:
        """清理过期删除标记（默认每天最多一次）。tombstone_days=0 表示永久保留。"""
        if self.tombstone_days <= 0:
            # 删除标记不清理，但实例心跳记录还是要清
            self._maybe_gc_instances(force)
            return 0
        now = time.time()
        if not force and (now - self._last_gc) < 86400:
            return 0
        self._last_gc = now
        before = int((now - self.tombstone_days * 86400) * 1000)
        try:
            n = self.backend.gc_tombstones(before)
        except CloudSyncError as e:
            self._log('warning', f"清理过期删除标记失败: {e}")
            n = 0
        if n:
            self._log('info', f"已清理 {n if n > 0 else '若干'} 条超过 "
                              f"{self.tombstone_days} 天的删除标记")
        self._maybe_gc_instances(force)
        return n

    def _maybe_gc_instances(self, force: bool = False):
        """清理 1 天前的实例心跳记录（防止表里堆积）"""
        try:
            self.backend.delete_instances(int((time.time() - 86400) * 1000))
        except CloudSyncError as e:
            self._log('debug', f"清理实例心跳失败: {e}")

    # ---------- 多实例检测 ----------
    def _instance_key(self) -> str:
        return INSTANCE_PREFIX + f"{socket.gethostname()}-{os.getpid()}"

    def _remove_heartbeat(self):
        """停止时删掉自己的实例心跳，避免下次启动被自己上一轮留下的记录误判"""
        try:
            self.backend.delete(self._instance_key())
            self._log('debug', '已移除本实例的云端心跳记录')
        except CloudSyncError as e:
            self._log('debug', f"移除实例心跳失败: {e}")

    def _heartbeat(self):
        """上报本实例心跳；若发现另一个实例也在同步同一数据库则告警。

        两个进程同时同步同一份数据会互相覆盖（也会看到"同一批文件被上传两次"），
        所以这里主动检测并提示。注意：
          · 心跳 key 带 pid，所以同一台机器重启后 key 会变；
          · 同一台机器上"进程已不存在"的旧心跳会被直接清理（上次运行/崩溃留下的），不算其它实例；
          · 不同机器的记录无法判断进程存活，按心跳时效（3 个周期）判断。
        """
        try:
            now_ms = int(time.time() * 1000)
            my_key = self._instance_key()
            my_host = socket.gethostname()
            info = json.dumps({'host': my_host, 'pid': os.getpid(),
                               'started': self._started_at, 'interval': self.interval,
                               'dir': os.path.abspath(self.root)}, ensure_ascii=False)
            self.backend.put_many([(my_key, info, now_ms)])
            entries = self.backend.list_entries(INSTANCE_PREFIX)
            fresh_ms = max(self.interval, 60) * 3000          # 3 个周期内算"活着"
            others = []
            for k, (ts, is_del) in entries.items():
                if is_del or k == my_key or (now_ms - int(ts)) > fresh_ms:
                    continue
                try:
                    o = json.loads(self.backend.get(k) or '{}')
                except Exception:
                    o = {}
                ohost = o.get('host') or k.split('/')[-1].rsplit('-', 1)[0]
                opid = o.get('pid') or 0
                if ohost == my_host and not _pid_alive(opid):
                    # 本机、但进程已经不在了 → 上一次运行（或崩溃）留下的残留记录，清掉
                    try:
                        self.backend.delete(k)
                        self._log('info', f"已清理本机上次运行留下的实例记录（pid {opid} 已退出）")
                    except CloudSyncError as e:
                        self._log('debug', f"清理残留实例记录失败: {e}")
                    continue
                where = o.get('dir') or ''
                others.append('%s(pid %s%s)' % (ohost, opid, ('，' + where) if where else ''))
            if others and not self._warned_multi:
                self._warned_multi = True
                self._log('warning',
                          '检测到另一个实例也在同步同一个云数据库：%s —— '
                          '多实例会互相覆盖（并出现同一批文件被重复上传），请只运行一台机器人'
                          % '、'.join(others))
            elif not others:
                self._warned_multi = False
        except CloudSyncError as e:
            self._log('debug', f"实例心跳上报失败: {e}")

    # ---------- 后台线程 ----------
    def start(self, immediate: bool = False):
        """启动后台同步线程。

        immediate=True：不等待那 3 秒缓冲，立刻做第一次同步
        （用于"连接 QQ 成功后立即拉取一次"，让数据尽快对齐）。
        """
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, args=(immediate,),
                                       daemon=True, name='cloud-sync')
        self._thread.start()
        self._log('info', f"云同步已开启（每 {self.interval} 秒检查一次）"
                          + ("，正在执行首次同步…" if immediate else ""))

    def _loop(self, immediate: bool = False):
        # 首次同步稍等片刻，避免和启动流程抢 IO（immediate=True 时不等待）
        if not immediate:
            self._stop_event.wait(3)
        while not self._stop_event.is_set():
            try:
                if self.is_paused():
                    # 暂停中：不做任何云端操作，安静等待（暂停/恢复时已各打过日志）
                    self._stop_event.wait(min(60, max(5, self.interval)))
                    continue
                res = self.sync_once()
                if res.get('uploaded') or res.get('downloaded') or res.get('deleted'):
                    self._log('debug', f"同步完成: {res}")
            except Exception as e:
                self._log('warning', f"同步失败（下个周期重试）: {e}")
            self._stop_event.wait(self._next_wait())

    def _next_wait(self) -> float:
        """距下一次写入云数据库的等待秒数。

        间隔 ≥ 60 秒时**对齐到整点倍数**：例如 interval=300（5分钟），
        写入时间会落在每小时的 00/05/10/15… 分，便于预期和排查。
        """
        if self.interval >= 60:
            now = time.time()
            nxt = (int(now // self.interval) + 1) * self.interval
            return max(1.0, nxt - now)
        return float(self.interval)

    def stop(self, final_sync: bool = True):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        if final_sync:
            try:
                self.sync_once()
                # 程序结束：控制台只汇报数量，不逐条列出文件名（明细在日志文件里）
                self._log('info', '云同步已停止，退出前完成最后一次同步；本次运行累计：'
                                  + self.totals_text())
            except Exception as e:
                self._log('warning', f"退出前同步失败: {e}")
        # 最后再删掉自己的实例心跳（必须在最后一次 sync_once 之后，
        # 否则那次同步里的心跳又会把记录写回去）
        self._remove_heartbeat()
        # 退出兜底：启动写缓存还没结束（首次同步一直没成功）→ 把缓存的改动写回磁盘，绝不丢数据
        startup_buffer_flush('程序退出（第一次云同步未完成）', warning=True)
