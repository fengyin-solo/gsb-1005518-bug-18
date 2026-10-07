"""升压站监视业务规则：母线状态机、断路器同源校验与调度联动都收在这里。

核心约束：
1. 母线状态严格按「运行 → 热备用 → 检修 → 停运」逐档前进，跨档、回到上一档一律拦下；
2. 断路器状态与母线状态同源推导，展示口径不允许各存一份；母线只有在断路器已合闸时
   才能被推到「运行」，断路器没合上的送电请求直接拒绝；
3. 已「检修」「停运」的站不能直接送电，必须按档位顺序流转回来；
4. 站内送电请求重复提交只落一条（运行态再送、待执行调度单已存在都算重复）；
5. 每次母线状态变化都落盘（store.commit），并往调度指令待处理清单同步一条指令；
6. 老状态名、老动作（恢复正常/检查保护/安排检修）继续兼容，判定结论保持老口径。
"""
from __future__ import annotations

from typing import Any

from app.services.dispatch import DispatchService
from app.store import store

MODULE = "boosting_station"
REQUIRED_FIELDS = ["升压站编号", "进线电压", "出线电压"]

# 母线状态机：只允许沿该序列逐档前进，末态「停运」不可再变
BUS_STATES = ["运行", "热备用", "检修", "停运"]
RUNNING = "运行"
HOT_STANDBY = "热备用"
MAINTENANCE = "检修"
SHUTDOWN = "停运"

# 断路器状态由母线状态同源推导，杜绝列表与详情错位
BREAKER_BY_BUS = {
    RUNNING: "合闸",
    HOT_STANDBY: "分闸",
    MAINTENANCE: "分闸",
    SHUTDOWN: "分闸",
}
BREAKER_CLOSED = "合闸"

# 老状态名/老动作结论与新状态机的兼容映射
LEGACY_BUS_STATES = {
    "正常运行": RUNNING,
    "非全相运行": HOT_STANDBY,
    "待检修": MAINTENANCE,
    "保护动作": SHUTDOWN,
}
LEGACY_ACTIONS = {
    "恢复正常": "送电",
    "检查保护": HOT_STANDBY,
    "安排检修": MAINTENANCE,
}

BUS_FIELD = "母线状态"
BREAKER_FIELD = "断路器状态"
RUN_FIELD = "运行状态"

_dispatch = DispatchService()


def normalize_bus(value: Any) -> str | None:
    """把入参或老数据里的状态名归一到新状态机；无法识别返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text in BUS_STATES:
        return text
    return LEGACY_BUS_STATES.get(text)


def normalize_entry(row: dict[str, Any]) -> dict[str, Any]:
    """读出时统一口径：母线状态归一、断路器/运行状态同源、老数据补齐缺失字段。"""
    bus = normalize_bus(row.get(BUS_FIELD)) or normalize_bus(row.get("status")) or HOT_STANDBY
    row[BUS_FIELD] = bus
    row["status"] = bus
    row[BREAKER_FIELD] = BREAKER_BY_BUS[bus]
    row[RUN_FIELD] = bus
    row.setdefault("pending", bus != SHUTDOWN)
    row.setdefault("abnormal", bus in (MAINTENANCE, SHUTDOWN))
    return row


class BoostingStationService:
    # ------------------------------------------------------------------ 查询
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
            rows = [row for row in rows if keyword in str(row.get("升压站编号", ""))]
        if status:
            wanted = normalize_bus(status) or status
            rows = [row for row in rows if row.get(BUS_FIELD) == wanted]
        rows.sort(key=lambda row: int(row.get("id", 0)))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def all_entries(self) -> list[dict[str, Any]]:
        rows = [normalize_entry(dict(row)) for row in store.rows(MODULE)]
        rows.sort(key=lambda row: int(row.get("id", 0)))
        return rows

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return normalize_entry(dict(row)) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            # 新登记站默认热备用、断路器分闸；要运行必须走送电并确认断路器合闸
            entry[BUS_FIELD] = HOT_STANDBY
            entry[BREAKER_FIELD] = BREAKER_BY_BUS[HOT_STANDBY]
            entry[RUN_FIELD] = HOT_STANDBY
            entry["status"] = HOT_STANDBY
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            store.commit()
            return normalize_entry(dict(entry)), []

    # ------------------------------------------------------------- 内部状态机
    @staticmethod
    def _next_state(current: str, target: str) -> str | None:
        """合法时返回目标档，非法（跨档/回退/同档/末态再动）返回 None。"""
        if current not in BUS_STATES or target not in BUS_STATES:
            return None
        cur_idx = BUS_STATES.index(current)
        tgt_idx = BUS_STATES.index(target)
        if cur_idx == len(BUS_STATES) - 1:
            return None  # 停运是末态
        return target if tgt_idx == cur_idx + 1 else None

    @staticmethod
    def _apply_state(entry: dict[str, Any], bus: str, *, abnormal: bool | None = None) -> None:
        """状态回写：母线、断路器、运行状态同源于一个值，一次写齐。"""
        entry[BUS_FIELD] = bus
        entry["status"] = bus
        entry[BREAKER_FIELD] = BREAKER_BY_BUS[bus]
        entry[RUN_FIELD] = bus
        entry["pending"] = bus != SHUTDOWN
        entry["abnormal"] = bus in (MAINTENANCE, SHUTDOWN) if abnormal is None else abnormal

    def _sync_dispatch(self, entry: dict[str, Any], order_type: str, content: str) -> dict[str, Any]:
        order, _ = _dispatch.create_pending_order(
            station_id=int(entry["id"]),
            station_code=str(entry.get("升压站编号", "")),
            order_type=order_type,
            content=content,
        )
        return order

    # --------------------------------------------------------------- 状态回写
    def change_bus_state(self, entry_id: int, target_raw: str) -> tuple[dict[str, Any] | None, str]:
        """母线状态回写：逐档前进；禁止跨档、回退、在未合闸时直接推运行。"""
        target = normalize_bus(target_raw)
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"升压站 {entry_id} 不存在或已归档"
            current = normalize_bus(entry.get(BUS_FIELD)) or normalize_bus(entry.get("status"))
            if target is None:
                return None, f"目标母线状态「{target_raw}」不合法，仅支持：{'、'.join(BUS_STATES)}"
            if current is None:
                return None, f"当前母线状态「{entry.get(BUS_FIELD)}」不在允许的状态序列里"
            # 推到运行只能走「站内送电」并确认断路器合闸，普通回写一律不放行
            if target == RUNNING:
                return None, "母线转运行必须执行「站内送电」并确认断路器已合闸"
            nxt = self._next_state(current, target)
            if nxt is None:
                if current == SHUTDOWN:
                    return None, "母线已处于停运末态，不能再回退或改档"
                if BUS_STATES.index(target) <= BUS_STATES.index(current):
                    return None, f"母线状态只能按 {'→'.join(BUS_STATES)} 顺序流转，不能回到「{target}」"
                return None, (
                    f"母线状态只能逐档流转，当前「{current}」"
                    f"下一步只能到「{BUS_STATES[BUS_STATES.index(current) + 1]}」，不能跨到「{target}」"
                )
            self._apply_state(entry, nxt)
            # 母线离开运行 → 上一轮送电单随之了结，之后重新送电才能再落新单
            if current == RUNNING and nxt == HOT_STANDBY:
                _dispatch.close_open_orders(
                    entry_id,
                    "站内送电",
                    result=f"母线已由运行转{nxt}，原送电指令执行完毕",
                )
            store.commit()
            order = self._sync_dispatch(
                entry,
                order_type=f"母线转{nxt}",
                content=f"升压站 {entry.get('升压站编号')} 母线状态由「{current}」变更为「{nxt}」，请调度确认",
            )
            message = f"母线状态已由「{current}」变更为「{nxt}」，已同步调度待处理指令 {order['指令编号']}"
            return normalize_entry(dict(entry)), message

    # --------------------------------------------------------------- 站内送电
    def energize(
        self,
        entry_id: int,
        *,
        breaker_closed: bool,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """站内送电：仅热备用可送；必须确认断路器合闸；重复提交只落一条。

        返回 (记录, 提示, 是否生效)；重复请求 ok=True 但 created=False，不重复落单、不重复变更。
        """
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"升压站 {entry_id} 不存在或已归档", False
            current = normalize_bus(entry.get(BUS_FIELD)) or normalize_bus(entry.get("status"))
            code = str(entry.get("升压站编号", ""))

            # 1) 只有热备用允许直接送电；停运/检修/运行都拦
            if current == SHUTDOWN:
                return None, "该站已停运，不能直接送电，请先按恢复流程逐档转回", False
            if current == MAINTENANCE:
                return None, "该站处于检修态，不能直接送电，请先转热备用再送电", False
            if current == RUNNING:
                # 运行态重复送电：查既有待执行单，只落一条
                order = _dispatch._find_open_order(entry_id, "站内送电")
                if order is not None:
                    return (
                        normalize_entry(dict(entry)),
                        f"该站已在运行，站内送电请求已提交过（调度单 {order['指令编号']}），请勿重复提交",
                        False,
                    )
                return normalize_entry(dict(entry)), "该站已在运行，无需重复送电", False
            if current != HOT_STANDBY:
                return None, f"当前母线状态「{current}」不允许直接送电", False

            # 2) 断路器没合上，不许把母线推成运行
            if not breaker_closed:
                return None, "断路器尚未合闸，不允许把母线推成运行；请先确认断路器合闸后再送电", False

            # 3) 待执行调度单已存在 → 重复提交，只落一条
            order, created = _dispatch.create_pending_order(
                station_id=entry_id,
                station_code=code,
                order_type="站内送电",
                content=f"升压站 {code} 申请站内送电：断路器已确认合闸，母线由「热备用」转「运行」，请调度下令",
            )
            if not created:
                return (
                    normalize_entry(dict(entry)),
                    f"站内送电请求已提交过（调度单 {order['指令编号']}），重复提交不再落单",
                    False,
                )

            # 4) 状态同源回写并落盘
            self._apply_state(entry, RUNNING, abnormal=False)
            store.commit()
            return (
                normalize_entry(dict(entry)),
                f"站内送电成功：断路器已合闸，母线转运行，已同步调度待处理指令 {order['指令编号']}",
                True,
            )

    # ----------------------------------------------------- 老动作入口（兼容）
    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        """老动作入口：恢复正常/检查保护/安排检修，结论维持老口径，内部走新状态机。"""
        values = values or {}
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"升压站 {entry_id} 不存在或已归档"
            if action not in LEGACY_ACTIONS:
                return None, f"动作「{action}」不属于升压站监视可执行范围"

            if LEGACY_ACTIONS[action] == "送电":
                # 老「恢复正常」按送电处理：兼容老入参里显式携带的断路器合闸标记
                breaker = str(values.get("断路器状态", values.get("breaker_closed", ""))).strip()
                breaker_closed = breaker in (BREAKER_CLOSED, "1", "true", "True", "是")
                result, message, _ = self.energize(entry_id, breaker_closed=breaker_closed)
                if result is None:
                    return None, message
                return result, message

            target = LEGACY_ACTIONS[action]  # 检查保护→热备用，安排检修→检修
            result, message = self.change_bus_state(entry_id, target)
            if result is None:
                return None, message
            return result, f"升压站已{action}（母线{message.split('，')[0]}）"
