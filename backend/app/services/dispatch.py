"""调度指令业务规则：状态流转、字段校验与筛选口径都收在这里。

升压站母线状态变化时会通过 :meth:`DispatchService.create_pending_order` 往待处理清单
同步一条调度指令；重复的业务请求（同一站、同一类型、还挂着待执行单）只落一条。
老的「确认执行/完成回复/驳回指令」动作结论维持既有判定，不做改动。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import store

MODULE = "dispatch"
REQUIRED_FIELDS = ["指令编号", "下发单位", "指令类型"]
STATUS_ORDER = ["待执行", "执行中", "已完成", "已驳回"]
ACTION_RULES = {"确认执行": "执行中", "完成回复": "已完成", "驳回指令": "已驳回"}
NEGATIVE_ACTIONS = ["驳回指令"]

PENDING_STATUS = "待执行"
ORDER_PREFIX = "DISP"


def normalize_entry(row: dict[str, Any]) -> dict[str, Any]:
    """读出时兜底：指令状态跟主状态字段对齐，避免列表与详情各说各话。"""
    status = row.get("status")
    if status in STATUS_ORDER:
        row["指令状态"] = status
    return row


class DispatchService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [normalize_entry(dict(row)) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("指令编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: int(row.get("id", 0)))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return normalize_entry(dict(row)) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        store.commit()
        return entry, []

    def _find_open_order(self, station_id: int, order_type: str) -> dict[str, Any] | None:
        """同一升压站、同一类型只要还挂着待执行单，就视为同一笔业务请求。"""
        for row in store.rows(MODULE):
            if (
                row.get("来源模块") == "boosting_station"
                and int(row.get("来源记录", 0) or 0) == station_id
                and row.get("指令类型") == order_type
                and row.get("status") == PENDING_STATUS
            ):
                return row
        return None

    def create_pending_order(
        self,
        *,
        station_id: int,
        station_code: str,
        order_type: str,
        content: str,
        issued_by: str = "升压站状态联动",
    ) -> tuple[dict[str, Any], bool]:
        """母线状态变化同步待处理清单；返回(指令单, 是否新建)，重复请求返回既有单且不新建。

        调用方需持有 ``store.lock``，与升压站状态机的检查-写入保持同一临界区。
        """
        existing = self._find_open_order(station_id, order_type)
        if existing is not None:
            return normalize_entry(dict(existing)), False

        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({
            "指令编号": f"{ORDER_PREFIX}-{entry['id']:04d}",
            "下发单位": "地区调度中心",
            "指令类型": order_type,
            "下发时间": date.today().isoformat(),
            "执行时限": "按调度令执行",
            "执行人": "",
            "执行结果": "",
            "指令状态": PENDING_STATUS,
        })
        entry["status"] = PENDING_STATUS
        entry["pending"] = True
        entry["abnormal"] = False
        # 与升压站记录建立同源关联，列表/详情都能追到是哪座站的母线变化
        entry["来源模块"] = "boosting_station"
        entry["来源记录"] = station_id
        entry["关联升压站"] = station_code
        entry["指令内容"] = content
        rows.append(entry)
        store.commit()
        return normalize_entry(dict(entry)), True

    def close_open_orders(
        self,
        station_id: int,
        order_type: str,
        *,
        result: str,
        status: str = "已完成",
    ) -> int:
        """把某站某类型仍挂着的待执行单结掉（如母线离开运行后，上一轮送电单即失效）。

        返回结掉的单数。调用方需持有 ``store.lock``。
        """
        closed = 0
        for row in store.rows(MODULE):
            if (
                row.get("来源模块") == "boosting_station"
                and int(row.get("来源记录", 0) or 0) == station_id
                and row.get("指令类型") == order_type
                and row.get("status") == PENDING_STATUS
            ):
                row["status"] = status
                row["指令状态"] = status
                row["pending"] = status not in ("已完成", "已驳回")
                row["执行结果"] = result
                closed += 1
        if closed:
            store.commit()
        return closed

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"调度指令单 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于调度指令可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["指令状态"] = target
        entry["pending"] = target not in ("已完成", "已驳回")
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.commit()
        return normalize_entry(dict(entry)), f"调度指令单已{action}"
