# -*- coding: utf-8 -*-
"""启动写缓存（startup write buffer）

【为什么需要】
程序刚启动时，核心模块与插件常常会立刻写自己的数据文件（补默认值、整理格式、
重置结构……）。而云同步的第一次同步还没跑完，本地数据可能比云端旧：
这些"基于旧数据的写入"一旦落盘，云同步就会认为本地更新 → 不上传云端的新数据 →
等于把云端数据覆盖掉了。换服务器/新机器时最明显（本地是空的，一启动就写默认值）。

【做法】
从程序启动到"第一次云同步完成"这段时间里，把对**会被同步的文件**（data/ 下的数据，
不含日志等不上云的文件）的修改先记在内存里：

  · 写入 / 追加 / 删除 / 改名 都只记在内存，不动磁盘
  · 读取会看到缓存里的内容（程序逻辑前后一致，不会读到旧数据）
  · os.path.exists / getsize / getmtime 也按缓存内容回答
  · 第一次云同步完成后（云端数据已经落盘）再统一写回磁盘
  · 若某个文件刚被首次同步从云端恢复（说明云端有更新的版本），默认放弃
    启动期间的这份本地修改（"以云端为准"），并在控制台 WARNING 里列出来，
    避免"刚拉下来的云端数据又被启动时的旧数据盖回去"

【什么时候不生效】
  · 没有启用云同步（配置不完整也一样）→ 本模块根本不会被 activate，一切照旧
  · 等待超过 cloud_sync.startup_buffer_minutes 分钟 → 先落盘（避免数据一直不写）
  · 缓存内容超过 cloud_sync.startup_buffer_max_mb（默认 8MB）→ 立刻落盘并停止缓存，
    避免机器人很忙时缓存无限占内存（之后的改动直接写磁盘）
  · 云同步进入暂停状态 → 立即落盘（短时间内不会再同步，不能一直等）
  · 程序退出 → 落盘（不丢数据）
  · 日志、config.json 等不参与云同步的文件 → 永远直接写磁盘

【实现】
包装内置 open() 以及 os.remove / unlink / rename / replace / stat。
这样核心代码、插件、第三方库的写入都会被拦下，插件不需要做任何改动。
云同步自己的读写（下载云端文件、扫描本地文件）必须看到磁盘真实状态，
所以 core/cloud_sync.py 在 sync_once() 外层调用 startup_buffer_suspend() 临时挂起。
"""

import builtins
import io
import os
import threading
import time

# ---------- 先保存原始函数（必须在包装之前） ----------
_REAL_OPEN = builtins.open
_REAL_REMOVE = os.remove
_REAL_UNLINK = os.unlink
_REAL_RENAME = os.rename
_REAL_REPLACE = os.replace
_REAL_STAT = os.stat
# Windows 上 os.path.exists/isfile 是 C 实现（nt._exists），不会走 os.stat，必须单独包装
_REAL_EXISTS = os.path.exists
_REAL_ISFILE = os.path.isfile
_REAL_LEXISTS = os.path.lexists

_LOCK = threading.RLock()

# ---------- 缓存状态 ----------
_active = False            # 是否处于"启动写缓存"状态
_suspended = 0             # 挂起计数（>0 时全部透传；云同步自己的 IO 会临时挂起）
_root = ''                 # 同步根目录（data 的绝对路径）
_logger = None
_limit_seconds = 0.0       # 等待首次同步的上限（秒）；0=一直等
_keep_remote = True        # 首次同步刚恢复的文件：放弃启动期间的本地修改
_activated_at = 0.0
_ops = []                  # 顺序记录的写操作：('write', rel, data, kind, kw) / ('delete', rel)
_views = {}                # rel -> ('text', str) / ('bytes', bytes) / ('gone', None)
_bytes = 0                 # 缓存内容总大小（字节，估算）——用于容量上限
_max_bytes = 0             # 容量上限（0=不限制）；超限就立刻写回磁盘，不占内存
# 兜底硬上限：即使把 startup_buffer_max_mb 配成 0（不限制），也不能真的"无限"——
# 超过这个数照样立刻落盘并告警，保证内存占用永远有上界
HARD_MAX_BYTES = 128 * 1024 * 1024
# startup_buffer_minutes=0（不限等待时间）时的兜底超时：首次同步一直失败也不会无限期占内存
SAFETY_LIMIT_SECONDS = 1800.0
_remote_won = set()        # 本次同步中"以云端为准"的文件（被云端覆盖 / 被云端标记删除）
_released = threading.Event()   # 缓存结束时置位，用于结束看门狗线程
_watchdog = None
_watchdog_gen = 0          # 看门狗对应的激活代次
_gen = 0                   # 每次 activate 递增：旧看门狗线程据此自动作废
_flushing = False

# 这些文件不参与云同步（与 core/cloud_sync.py 的 _excluded 保持一致）
_SKIP_NAMES = ('bot.lock', 'bot.pid', '.cloud_sync_state.json')


def _log(level: str, msg: str):
    if not _logger:
        return
    try:
        getattr(_logger, level, _logger.info)('[启动缓存] ' + msg)
    except Exception:
        pass


# ---------- 路径判断 ----------
def _is_synced_rel(rel: str) -> bool:
    """rel（相对 data/，用 / 分隔）是否属于"会被同步的文件\""""
    low = rel.replace('\\', '/')
    if low in _SKIP_NAMES:
        return False
    if low.startswith('bot.lockdir') or low.startswith('.cloud_sync_state'):
        return False
    if '_test' in low or low.endswith('.tmp') or low.endswith('.part'):
        return False
    # 日志不缓存：用户要求日志（含报错）任何时候都立刻写磁盘
    if low == 'logs' or low.startswith('logs/'):
        return False
    return True


def _rel_for(path):
    """返回可缓存的相对路径；不在同步范围/不是路径 → None"""
    if not _root or isinstance(path, int):
        return None
    try:
        p = os.fspath(path)
    except TypeError:
        return None
    if isinstance(p, bytes):
        try:
            p = os.fsdecode(p)
        except Exception:
            return None
    if not isinstance(p, str):
        return None
    try:
        full = os.path.abspath(p)
    except (OSError, ValueError):
        return None
    if full == _root or not full.startswith(_root + os.sep):
        return None
    rel = os.path.relpath(full, _root).replace('\\', '/')
    if rel.startswith('..'):
        return None
    return rel if _is_synced_rel(rel) else None


def _full_for(rel: str) -> str:
    return os.path.join(_root, rel.replace('/', os.sep))


def _buffering() -> bool:
    return _active and not _suspended


def _view(rel: str):
    with _LOCK:
        return _views.get(rel)


# ---------- 记录操作 ----------
def _size_of(data, kind: str) -> int:
    """内容大致占用多少字节（文本按 UTF-8 长度估算；0 表示没有内容）"""
    if data is None:
        return 0
    if kind == 'bytes' or isinstance(data, (bytes, bytearray)):
        return len(data)
    try:
        return len(data.encode('utf-8'))
    except Exception:
        return len(data)


def _sb_size_text(n: int) -> str:
    if n >= 1024 * 1024:
        return '%.1f MB' % (n / 1024.0 / 1024.0)
    if n >= 1024:
        return '%.0f KB' % (n / 1024.0)
    return '%d 字节' % n


def _record_write(rel: str, data, kind: str, kw):
    """记下这次写入；超过容量上限（或兜底硬上限）就立刻把缓存写回磁盘，避免无限占内存"""
    global _bytes
    over = ''
    with _LOCK:
        _ops.append(('write', rel, data, kind, kw))
        _views[rel] = (kind, data)
        _bytes += _size_of(data, kind)
        limit = _max_bytes or HARD_MAX_BYTES
        if _bytes > limit:
            over = ('超过容量上限 %s，已缓存 %s' % (_sb_size_text(_max_bytes), _sb_size_text(_bytes))
                    if _max_bytes else
                    '超过兜底上限 %s，已缓存 %s' % (_sb_size_text(HARD_MAX_BYTES), _sb_size_text(_bytes)))
    if over:
        startup_buffer_flush('启动缓存' + over, warning=True)


def _record_delete(rel: str):
    with _LOCK:
        _ops.append(('delete', rel, None, None, None))
        _views[rel] = ('gone', None)


def _exists_effective(rel: str, full: str) -> bool:
    v = _view(rel)
    if v is not None:
        return v[0] != 'gone'
    return os.path.exists(full)


def _disk_bytes(full: str):
    try:
        with _REAL_OPEN(full, 'rb') as f:
            return f.read()
    except OSError:
        return None


def _effective_text(rel, full, encoding=None, errors=None):
    """缓存里的文本内容；没有缓存记录时读磁盘；都不存在 → None"""
    v = _view(rel) if rel else None
    if v is not None:
        kind, data = v
        if kind == 'gone':
            return None
        return data if kind == 'text' else data.decode(encoding or 'utf-8', errors or 'strict')
    try:
        with _REAL_OPEN(full, 'r', encoding=encoding or 'utf-8', errors=errors or 'strict') as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _effective_bytes(rel, full, encoding=None, errors=None):
    v = _view(rel) if rel else None
    if v is not None:
        kind, data = v
        if kind == 'gone':
            return None
        return data if kind == 'bytes' else data.encode(encoding or 'utf-8', errors or 'strict')
    return _disk_bytes(full)


# ---------- 内存里的"文件对象" ----------
class _PendingWriterMixin:
    """写入内容先留在内存里的文件对象；真正落盘在 flush 时"""
    _pw_kind = 'text'

    def _pw_init(self, path, rel, mode, kw):
        self._pw_rel = rel
        self._pw_kw = kw
        self._pw_done = False
        try:
            self.name = path
            self.mode = mode
        except Exception:
            pass

    def close(self):
        if not self._pw_done:
            self._pw_done = True
            try:
                data = self.getvalue()
            except Exception:
                data = '' if self._pw_kind == 'text' else b''
            _record_write(self._pw_rel, data, self._pw_kind, self._pw_kw)
        super().close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _PendingTextWriter(_PendingWriterMixin, io.StringIO):
    _pw_kind = 'text'

    def __init__(self, path, rel, mode, kw, initial):
        self._pw_init(path, rel, mode, kw)
        # StringIO 默认 newline='\n'：不翻译换行，原样保存调用方写入的文本，
        # flush 时再用调用方的原始参数（encoding/newline）写盘，效果与直接写文件一致
        super().__init__(initial or '')
        self.seek(len(initial or '') if 'a' in mode else 0)


class _PendingBinaryWriter(_PendingWriterMixin, io.BytesIO):
    _pw_kind = 'bytes'

    def __init__(self, path, rel, mode, kw, initial):
        self._pw_init(path, rel, mode, kw)
        super().__init__(initial or b'')
        self.seek(len(initial or b'') if 'a' in mode else 0)


# ---------- open / os.* 包装 ----------
def _open(file, mode='r', buffering=-1, encoding=None, errors=None,
          newline=None, closefd=True, opener=None):
    if not _buffering():
        return _REAL_OPEN(file, mode, buffering, encoding, errors, newline, closefd, opener)
    rel = _rel_for(file)
    if rel is None:
        return _REAL_OPEN(file, mode, buffering, encoding, errors, newline, closefd, opener)
    full = _full_for(rel)
    m = mode or 'r'
    binary = 'b' in m
    writing = any(c in m for c in 'wax+')

    if not writing:
        # 只读：缓存里有内容就用缓存（否则读到的是旧数据，程序会做出错误判断）
        v = _view(rel)
        if v is None:
            return _REAL_OPEN(file, mode, buffering, encoding, errors, newline, closefd, opener)
        kind, data = v
        if kind == 'gone':
            raise FileNotFoundError(2, 'No such file or directory', str(file))
        if binary:
            raw = data if kind == 'bytes' else data.encode(encoding or 'utf-8', errors or 'strict')
            f = io.BytesIO(raw)
        else:
            text = data if kind == 'text' else data.decode(encoding or 'utf-8', errors or 'strict')
            f = io.StringIO(text)
        try:
            f.name = str(file)
            f.mode = m
        except Exception:
            pass
        return f

    # 写模式：内容先留在内存
    if 'x' in m and _exists_effective(rel, full):
        raise FileExistsError(17, 'File exists', str(file))
    if m.startswith('r') and not _exists_effective(rel, full):
        raise FileNotFoundError(2, 'No such file or directory', str(file))
    initial = None
    if not any(c in m for c in 'wx'):
        # 追加/读写：以"缓存里的当前内容"为起点
        if binary:
            initial = _effective_bytes(rel, full, encoding, errors)
        else:
            initial = _effective_text(rel, full, encoding, errors)
    kw = {'encoding': encoding, 'errors': errors, 'newline': newline}
    if binary:
        return _PendingBinaryWriter(str(file), rel, m, kw, initial or b'')
    return _PendingTextWriter(str(file), rel, m, kw, initial or '')


def _remove(path, *args, **kwargs):
    if _buffering():
        rel = _rel_for(path)
        if rel is not None:
            if not _exists_effective(rel, _full_for(rel)):
                raise FileNotFoundError(2, 'No such file or directory', str(path))
            _record_delete(rel)
            return None
    return _REAL_REMOVE(path, *args, **kwargs)


def _replace(src, dst, *args, **kwargs):
    if _buffering():
        rs = _rel_for(src)
        rd = _rel_for(dst)
        if rs is not None or rd is not None:
            return _buffered_move(src, dst, rs, rd)
    return _REAL_REPLACE(src, dst, *args, **kwargs)


def _buffered_move(src, dst, rs, rd):
    """缓存状态下的 os.replace / os.rename"""
    src_full = _full_for(rs) if rs else os.fspath(src)
    if rd is None:
        # 目标不在同步范围：先把缓存内容落盘，再做真实的移动
        if rs is not None:
            _materialize(rs)
        return _REAL_REPLACE(src, dst)
    data = _effective_bytes(rs, src_full) if rs else _disk_bytes(os.fspath(src))
    if data is None:
        raise FileNotFoundError(2, 'No such file or directory', str(src))
    _record_write(rd, data, 'bytes', None)
    if rs is not None:
        _record_delete(rs)
    # 源文件（一般是中转文件）立刻从磁盘移除：它不参与同步，也不该留在缓存期间
    try:
        _REAL_REMOVE(src_full if rs else os.fspath(src))
    except OSError:
        pass
    return None


def _stat(path, *args, **kwargs):
    if _buffering():
        rel = _rel_for(path)
        if rel is not None:
            v = _view(rel)
            if v is not None:
                if v[0] == 'gone':
                    raise FileNotFoundError(2, 'No such file or directory', str(path))
                data = v[1]
                size = len(data.encode('utf-8')) if v[0] == 'text' else len(data)
                try:
                    t = tuple(_REAL_STAT(_full_for(rel), *args, **kwargs))
                except OSError:
                    t = None
                now = time.time()
                if t is None or len(t) < 10:
                    t = (0o100644, 0, 0, 1, 0, 0, size, now, now, now)
                else:
                    t = (t[0], t[1], t[2], t[3], t[4], t[5], size, now, now, t[9])
                return os.stat_result(t)
    return _REAL_STAT(path, *args, **kwargs)


def _exists(path):
    """os.path.exists：缓存里"新建"的文件算存在、"已删除"的算不存在"""
    if _buffering():
        rel = _rel_for(path)
        if rel is not None:
            v = _view(rel)
            if v is not None:
                return v[0] != 'gone'
    return _REAL_EXISTS(path)


def _lexists(path):
    if _buffering():
        rel = _rel_for(path)
        if rel is not None:
            v = _view(rel)
            if v is not None:
                return v[0] != 'gone'
    return _REAL_LEXISTS(path)


def _isfile(path):
    if _buffering():
        rel = _rel_for(path)
        if rel is not None:
            v = _view(rel)
            if v is not None:
                return v[0] != 'gone'      # 缓存里记录的都是文件
    return _REAL_ISFILE(path)


def _materialize(rel: str):
    """把某个文件的缓存内容立刻落盘（少数无法延迟的场景用）"""
    global _bytes
    with _LOCK:
        v = _views.pop(rel, None)
        if v is None:
            return
        freed = sum(_size_of(op[2], op[3]) for op in _ops
                    if op[1] == rel and op[0] == 'write')
        _ops[:] = [op for op in _ops if op[1] != rel]
        _bytes = max(0, _bytes - freed)
    _write_now(rel, v[0], v[1], None)


def _write_now(rel: str, kind: str, data, kw):
    """真正写磁盘（不走缓存）"""
    full = _full_for(rel)
    _makedirs(os.path.dirname(full))
    kw = kw or {}
    if kind == 'bytes':
        with _REAL_OPEN(full, 'wb') as f:
            f.write(data)
    else:
        with _REAL_OPEN(full, 'w', encoding=kw.get('encoding') or 'utf-8',
                        errors=kw.get('errors'), newline=kw.get('newline')) as f:
            f.write(data)


def _makedirs(path):
    try:
        if path:
            os.makedirs(path, exist_ok=True)
    except OSError:
        pass


# 安装包装（模块导入时一次即可；未激活时是"直接透传"，几乎没有开销）
builtins.open = _open
os.remove = _remove
os.unlink = _remove
os.rename = _replace
os.replace = _replace
os.stat = _stat
os.path.exists = _exists
os.path.lexists = _lexists
os.path.isfile = _isfile


# ---------- 对外接口 ----------
def startup_buffer_active() -> bool:
    """当前是否正在缓存启动期间的写入"""
    return _active


def startup_buffer_mark_remote(rel: str):
    """标记"这个文件本次以云端为准"（首次同步下载了云端内容 / 按云端标记删除了本地）"""
    if not _active or not rel:
        return
    with _LOCK:
        _remote_won.add(str(rel).replace('\\', '/'))


def startup_buffer_state() -> dict:
    """缓存状态（供控制台提示与 Web 后台展示）"""
    with _LOCK:
        n = len({op[1] for op in _ops})
        size = _bytes
    return {
        'active': bool(_active),
        'files': n,
        'bytes': int(size),
        'size_text': _sb_size_text(int(size)),
        'seconds': int(max(0, time.time() - _activated_at)) if _active else 0,
        'limit_seconds': int(_limit_seconds),
        'max_bytes': int(_max_bytes),
        'max_text': _sb_size_text(int(_max_bytes)) if _max_bytes else '不限制',
        'keep_remote': bool(_keep_remote),
    }


def startup_buffer_activate(root: str = 'data', logger=None, limit_seconds: float = 180,
                           keep_remote: bool = True, max_mb: float = 8.0):
    """开启启动写缓存（只在"启用了云同步、但首次同步还没完成"时调用）

    max_mb: 缓存容量上限（MB，0=不限制）。超过就立刻把已缓存的内容写回磁盘，
            避免机器人很忙时缓存无限占内存。
    """
    global _active, _root, _logger, _limit_seconds, _keep_remote, _activated_at
    global _watchdog, _gen, _bytes, _max_bytes
    with _LOCK:
        if logger is not None:
            _logger = logger
        _root = os.path.abspath(root or 'data')
        _limit_seconds = max(0.0, float(limit_seconds or 0))
        _keep_remote = bool(keep_remote)
        _max_bytes = int(max(0.0, float(max_mb or 0)) * 1024 * 1024)
        if _active:                      # 已经在缓存中：只更新参数（看门狗按新的上限重开）
            _gen += 1
            gen = _gen
            _start_watchdog(gen)
            return
        _ops.clear()
        _views.clear()
        _remote_won.clear()
        _bytes = 0
        _released.clear()
        _activated_at = time.time()
        _active = True
        _gen += 1
        gen = _gen
    limit_text = ('最多等 %s' % _fmt_limit(_limit_seconds)) if _limit_seconds else '一直等到同步完成'
    _log('info', "已开启：第一次云同步完成前（%s%s），data/ 下的数据改动会先存在内存里，"
                 "避免刚启动时写的旧数据把云端数据覆盖掉；"
                 "日志与 config.json 不受影响"
         % (limit_text,
            ('，缓存上限 %s' % _sb_size_text(_max_bytes)) if _max_bytes else ''))
    _start_watchdog(gen)


def _start_watchdog(gen: int):
    """按当前上限启动看门狗（旧线程靠 gen 不匹配自动作废）

    即使把 startup_buffer_minutes 配成 0（不限制等待时间），也会用兜底上限
    SAFETY_LIMIT_SECONDS 起一个看门狗：万一首次同步一直失败，数据也不会无限期留在内存里。
    """
    global _watchdog, _watchdog_gen
    limit = _limit_seconds if _limit_seconds > 0 else SAFETY_LIMIT_SECONDS
    if _watchdog is not None and _watchdog.is_alive() and _watchdog_gen == gen:
        return
    _watchdog_gen = gen
    _watchdog = threading.Thread(target=_watchdog_loop, args=(limit, gen),
                                 daemon=True, name='startup-buffer')
    _watchdog.start()


def _fmt_limit(seconds: float) -> str:
    if seconds >= 60:
        m = seconds / 60.0
        return ('%g 分钟' % m)
    return ('%g 秒' % seconds)


def _watchdog_loop(limit: float, gen: int):
    if _released.wait(limit):
        return
    with _LOCK:
        if not _active or gen != _gen:      # 已经结束 / 已被重新激活 → 本次不再处理
            return
    reason = ('等待第一次云同步超过 %s' % _fmt_limit(limit)) if _limit_seconds > 0 \
        else ('等待第一次云同步超过兜底上限 %s（startup_buffer_minutes=0）' % _fmt_limit(limit))
    startup_buffer_flush(reason, warning=True)


def startup_buffer_deactivate():
    """关闭缓存但不落盘（丢弃缓存内容；正常流程请用 flush）"""
    global _active, _bytes
    with _LOCK:
        if not _active:
            return
        _active = False
        _ops.clear()
        _views.clear()
        _remote_won.clear()
        _bytes = 0
    _released.set()


class StartupBufferSuspend:
    """临时挂起缓存（云同步自己的文件读写必须看到磁盘真实状态）"""

    def __enter__(self):
        global _suspended
        with _LOCK:
            _suspended += 1
        return self

    def __exit__(self, *exc):
        global _suspended
        with _LOCK:
            _suspended = max(0, _suspended - 1)
        return False


def startup_buffer_suspend():
    return StartupBufferSuspend()


def startup_buffer_flush(reason: str = '', warning: bool = False, logger=None) -> dict:
    """把缓存的修改写回磁盘（第一次云同步完成后 / 超时 / 退出时调用）

    reason: 写进日志的原因（如"第一次云同步已完成"）
    warning: True=用 WARNING 输出（超时、云同步暂停等异常情况）
    logger: 可选；不传就用 activate 时登记的日志器
    """
    global _active, _flushing, _logger, _bytes
    if logger is not None:
        _logger = logger
    with _LOCK:
        if not _active:
            return {'files': 0, 'deletes': 0, 'kept_remote': [], 'failed': []}
        if _flushing:
            return {'files': 0, 'deletes': 0, 'kept_remote': [], 'failed': []}
        _flushing = True
        ops = list(_ops)
        _ops.clear()
        _views.clear()
        remote = set(_remote_won)
        _remote_won.clear()
        _bytes = 0
        _active = False
    _released.set()

    written = deleted = 0
    kept, failed = [], []
    try:
        for op in ops:
            kind_op, rel = op[0], op[1]
            if _keep_remote and rel in remote:
                # 这个文件刚被首次同步从云端恢复 → 以云端为准，放弃启动期间的本地修改
                kept.append(rel)
                continue
            try:
                if kind_op == 'write':
                    _write_now(rel, op[3], op[2], op[4])
                    written += 1
                else:
                    full = _full_for(rel)
                    if _REAL_EXISTS(full):
                        _REAL_REMOVE(full)
                    deleted += 1
            except OSError as e:
                failed.append('%s（%s）' % (rel, e))
    finally:
        with _LOCK:
            _flushing = False

    total = written + deleted
    tail = ('，原因：%s' % reason) if reason else ''
    if warning:
        if '容量上限' in reason:
            # 容量上限触发：说清楚后续行为（不再缓存，直接写磁盘）
            _log('warning', "启动缓存已满，提前写回本地：%d 个文件改动、%d 个删除（%s）"
                            "——之后的改动会直接写磁盘"
                 % (written, deleted, reason))
        else:
            _log('warning', "已把启动期间缓存的内容写回本地：%d 个文件改动、%d 个删除%s"
                            "（为避免数据一直不落盘）" % (written, deleted, tail))
    elif total or kept:
        _log('info', "启动期间的修改已写回本地：%d 个文件改动、%d 个删除%s"
                     % (written, deleted, tail))
    if kept:
        _log('warning', "有 %d 个文件在启动期间被修改，但第一次同步刚用云端版本覆盖过它们，"
                        "已按「以云端为准」放弃这些本地修改：%s"
                        "（若希望本地修改优先，把 config.json 里 "
                        "cloud_sync.startup_buffer_keep_remote 改成 false）"
                        % (len(kept), '、'.join(kept[:8]) + ('…' if len(kept) > 8 else '')))
    if failed:
        _log('error', "启动期间的修改有 %d 个写入失败（其它文件已正常写入）: %s"
                      % (len(failed), '；'.join(failed[:5])))
    if not ops:
        _log('debug', "启动写缓存结束：启动期间没有任何数据文件改动%s" % tail)
    return {'files': written, 'deletes': deleted, 'kept_remote': kept, 'failed': failed}
