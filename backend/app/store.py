"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

状态变更会落盘到 backend/data/runtime_state.json（原子替换），服务重启后从落盘文件
恢复，不会再出现「存过一次、退出重进又滑回去」的情况；落盘文件缺失时才回退到种子数据。
真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from app.seed import SEED_ROWS

_STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STATE_FILE = os.path.join(_STATE_DIR, "runtime_state.json")


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = self._load()

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        saved: dict[str, Any] | None = None
        try:
            with open(STATE_FILE, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                saved = data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            saved = None

        tables: dict[str, list[dict[str, Any]]] = {}
        # 种子模块优先：有落盘用落盘，没有就用种子
        for name, rows in SEED_ROWS.items():
            saved_rows = saved.get(name) if saved else None
            if isinstance(saved_rows, list):
                tables[name] = [dict(row) for row in saved_rows]
            else:
                tables[name] = [dict(row) for row in rows]
        # 兼容落盘里多出来、种子还没有的模块
        if saved:
            for name, rows in saved.items():
                if name not in tables and isinstance(rows, list):
                    tables[name] = [dict(row) for row in rows]
        return tables

    def commit(self) -> None:
        """把当前全部数据原子写回落盘文件，保证状态变化真正进库。"""
        with self._lock:
            os.makedirs(_STATE_DIR, exist_ok=True)
            tmp_file = f"{STATE_FILE}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as handle:
                json.dump(self._tables, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_file, STATE_FILE)

    def reset(self) -> None:
        """丢弃运行期改动并恢复种子数据（测试与演示复位用）；落盘文件同步清掉。"""
        with self._lock:
            try:
                os.remove(STATE_FILE)
            except FileNotFoundError:
                pass
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }

    @property
    def lock(self) -> threading.RLock:
        """状态机「检查-写入-落盘」整段串行化用的可重入锁。"""
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
