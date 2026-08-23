"""
数据统计模块 - 按天记录机器人的运行数据（消息量、AI调用、关键词命中、指令使用等）
数据保存在 data/stats.json，自动保留最近 30 天。
"""

import json
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict


class StatsCollector:
    """轻量统计收集器（线程安全，按天分桶）"""

    FILE = os.path.join('data', 'stats.json')
    KEEP_DAYS = 30

    def __init__(self, logger=None):
        self.logger = logger
        self._lock = threading.Lock()
        self._days: Dict[str, Dict[str, int]] = {}
        self._load()

    # ---------- 内部 ----------
    def _load(self):
        try:
            if os.path.exists(self.FILE):
                with open(self.FILE, encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._days = data
        except Exception as e:
            if self.logger:
                self.logger.warning(f"读取统计数据失败: {e}")

    def _save(self):
        try:
            os.makedirs('data', exist_ok=True)
            # 只保留最近 KEEP_DAYS 天
            today = self._today()
            keep = set()
            from datetime import timedelta as _td
            for i in range(self.KEEP_DAYS):
                d = datetime.now(timezone(timedelta(hours=8))) - _td(days=i)
                keep.add(d.strftime('%Y-%m-%d'))
            self._days = {k: v for k, v in self._days.items() if k in keep or k == today}
            with open(self.FILE, 'w', encoding='utf-8') as f:
                json.dump(self._days, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if self.logger:
                self.logger.warning(f"保存统计数据失败: {e}")

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')

    # ---------- 写入 ----------
    def record(self, key: str, n: int = 1):
        """累加今天的某项统计（每次调用即落盘，避免进程退出丢失）"""
        if n <= 0:
            return
        with self._lock:
            day = self._days.setdefault(self._today(), {})
            day[key] = day.get(key, 0) + n
        self._save()

    # ---------- 读取 ----------
    def get_today(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._days.get(self._today(), {}))

    def get_recent(self, days: int = 7) -> Dict[str, Dict[str, int]]:
        """返回最近 N 天（含今天）的统计数据，按日期升序"""
        from datetime import timedelta as _td
        with self._lock:
            out = {}
            now = datetime.now(timezone(timedelta(hours=8)))
            for i in range(days - 1, -1, -1):
                d = (now - _td(days=i)).strftime('%Y-%m-%d')
                out[d] = dict(self._days.get(d, {}))
            return out

    # ---------- 汇总 ----------
    def totals(self) -> Dict[str, int]:
        """全部历史累计"""
        with self._lock:
            total: Dict[str, int] = {}
            for day_data in self._days.values():
                for k, v in day_data.items():
                    total[k] = total.get(k, 0) + v
            return total
