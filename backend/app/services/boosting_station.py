"""升压站监视业务规则：母线/断路器同源状态流转、站内送电申请与调度清单同步。

设计约定：
- 母线状态是唯一权威量，严格按 运行 → 热备用 → 检修 → 停运 单向逐档流转，
  跨档跳转与回退一律拦截；
- 断路器状态由母线状态同源派生（仅运行档合上），列表、详情、导出读到的都是
  同一条记录、同一份回写结果，不允许出现错位；
- 母线每发生一次变化，都同步一条指令到调度指令的待处理清单；已经有结论的
  老指令保持原结论不动（兼容既有判定）；
- 停运站不能直接送电，只能提交站内送电申请，重复申请只落一条待处理指令。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import store

MODULE = "boosting_station"
DISPATCH_MODULE = "dispatch"

REQUIRED_FIELDS = ["升压站编号", "进线电压", "出线电压"]
OPTIONAL_FIELDS = ["主变容量", "无功补偿"]

# 母线状态档位：顺序即允许的流转方向，只能逐档前进。
BUS_STATES = ["运行", "热备用", "检修", "停运"]
BREAKER_CLOSED = "合上"
BREAKER_OPEN = "断开"

# 母线逐档前进的动作。
ACTION_RULES = {"转热备用": "热备用", "转检修": "检修", "转停运": "停运"}
# 老版本动作名继续兼容，但目标档位仍要走同一套流转校验，不会因此放开回退。
LEGACY_ACTIONS = {"恢复正常": "运行", "检查保护": "热备用", "安排检修": "检修"}
# 老版本状态值归一化到现行母线档位，历史数据读出来不再错位。
LEGACY_BUS_STATES = {
    "正常运行": "运行",
    "非全相运行": "运行",
    "保护动作": "热备用",
    "待检修": "检修",
}

POWER_ON_ACTION = "申请站内送电"
DISPATCH_BUS_CHANGE = "母线状态变更"
DISPATCH_POWER_ON = "站内送电"
DISPATCH_PENDING = "待执行"


def breaker_state_for(bus_state: str) -> str:
    """断路器与母线同源：只有母线在运行档时才允许合上，其余档位一律断开。"""
    return BREAKER_CLOSED if bus_state == BUS_STATES[0] else BREAKER_OPEN


class BoostingStationService:
    # ---------- 读取 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        # 入口先归一化，保证列表与详情拿到的永远是同源后的同一份数据。
        for row in rows:
            self._normalize(row)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("升压站编号", ""))]
        if status:
            wanted = LEGACY_BUS_STATES.get(status, status)
            rows = [row for row in rows if row.get("母线状态") == wanted]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._normalize(entry)
        return entry

    # ---------- 登记 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if str(values.get(field) or "").strip():
                entry[field] = values.get(field)
        entry["abnormal"] = False
        # 新登记站默认并网运行：母线与断路器在同一回写里落库，天然同源。
        self._apply_state(entry, BUS_STATES[0], enforce_breaker=False)
        rows.append(entry)
        return entry, []

    # ---------- 状态流转 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"升压站 {entry_id} 不存在或已归档"
        self._normalize(entry)

        target = ACTION_RULES.get(action) or LEGACY_ACTIONS.get(action)
        if target is None:
            return None, f"动作「{action}」不属于升压站监视可执行范围；恢复送电请走「{POWER_ON_ACTION}」"

        current = str(entry["母线状态"])
        cur_idx = BUS_STATES.index(current)
        tgt_idx = BUS_STATES.index(target)

        if tgt_idx == cur_idx:
            return None, f"母线当前已是「{current}」状态，无需重复操作"
        if tgt_idx < cur_idx:
            return None, (
                f"母线状态只能按{'→'.join(BUS_STATES)}单向逐档流转，"
                f"不允许从「{current}」回退到「{target}」；停运站恢复送电须先申请调度指令"
            )
        if tgt_idx > cur_idx + 1:
            return None, (
                f"不允许跨档流转：「{current}」只能先转为「{BUS_STATES[cur_idx + 1]}」，"
                f"不能直接跳到「{target}」"
            )

        ok, reason = self._apply_state(entry, target, enforce_breaker=True)
        if not ok:
            return None, reason
        self._sync_bus_change(entry, current, target)
        return entry, (
            f"母线已由「{current}」转为「{target}」，"
            f"断路器同步为「{entry['断路器状态']}」，已同步调度待处理清单"
        )

    # ---------- 站内送电 ----------

    def request_power_on(
        self, entry_id: int
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        """停运站申请站内送电：在调度待处理清单落一条指令，重复申请只认同一条。

        返回 (升压站, 调度指令, 说明)；升压站为 None 表示申请被驳回。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, None, f"升压站 {entry_id} 不存在或已归档"
        self._normalize(entry)

        current = str(entry["母线状态"])
        if current != BUS_STATES[-1]:
            return None, None, (
                f"只有「{BUS_STATES[-1]}」状态的站才能申请站内送电，"
                f"当前为「{current}」，未停运的站不能直接送电"
            )

        existing = self._find_open_dispatch(int(entry["id"]), DISPATCH_POWER_ON)
        if existing is not None:
            return entry, existing, "站内送电申请已在调度指令待处理清单中，重复提交不再生成新指令"

        order = self._append_dispatch(
            entry,
            DISPATCH_POWER_ON,
            f"{entry['升压站编号']}申请站内送电，请调度下令后逐级恢复母线",
        )
        return entry, order, "站内送电申请已进入调度指令待处理清单"

    # ---------- 内部：唯一状态回写点 ----------

    def _apply_state(
        self, entry: dict[str, Any], bus_state: str, *, enforce_breaker: bool
    ) -> tuple[bool, str]:
        """把母线档位连同断路器等镜像字段一次性回写到同一条记录上。

        全模块只有这里改状态，列表、详情、导出因此永远一致。
        """
        if bus_state not in BUS_STATES:
            return False, f"目标状态「{bus_state}」不在允许的母线状态序列里"
        if enforce_breaker and bus_state == BUS_STATES[0]:
            breaker = str(entry.get("断路器状态") or "").strip()
            if breaker == BREAKER_OPEN:
                return False, "断路器尚未合上，不允许将母线推为运行状态"
        entry["母线状态"] = bus_state
        entry["断路器状态"] = breaker_state_for(bus_state)
        entry["运行状态"] = bus_state
        entry["status"] = bus_state
        entry["pending"] = bus_state != BUS_STATES[-1]
        return True, ""

    def _normalize(self, entry: dict[str, Any]) -> None:
        """把历史数据（旧状态名、脏断路器值）就地归一到同源结构。"""
        raw = str(entry.get("母线状态") or entry.get("status") or "").strip()
        bus_state = LEGACY_BUS_STATES.get(raw, raw)
        if bus_state not in BUS_STATES:
            bus_state = BUS_STATES[0]
        breaker_ok = entry.get("断路器状态") in (BREAKER_CLOSED, BREAKER_OPEN)
        consistent = (
            entry.get("母线状态") == bus_state
            and breaker_ok
            and entry.get("断路器状态") == breaker_state_for(bus_state)
            and entry.get("运行状态") == bus_state
            and entry.get("status") == bus_state
        )
        if not consistent:
            # 存量数据修复：以母线为准强制同源，不受断路器前提校验限制。
            self._apply_state(entry, bus_state, enforce_breaker=False)
        entry.setdefault("abnormal", bus_state == BUS_STATES[-1])

    # ---------- 内部：调度指令清单同步 ----------

    def _find_open_dispatch(
        self, station_id: int, dispatch_type: str
    ) -> dict[str, Any] | None:
        for row in store.rows(DISPATCH_MODULE):
            if (
                row.get("来源") == MODULE
                and row.get("来源ID") == station_id
                and row.get("指令类型") == dispatch_type
                and row.get("status") == DISPATCH_PENDING
            ):
                return row
        return None

    def _append_dispatch(
        self, station: dict[str, Any], dispatch_type: str, subject: str
    ) -> dict[str, Any]:
        rows = store.rows(DISPATCH_MODULE)
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        prefix = "SD" if dispatch_type == DISPATCH_POWER_ON else "BUS"
        order = {
            "id": next_id,
            "status": DISPATCH_PENDING,
            "pending": True,
            "abnormal": False,
            "指令编号": f"DISP-{prefix}-{int(station['id']):04d}-{next_id:04d}",
            "下发单位": "升压站监视",
            "指令类型": dispatch_type,
            "下发时间": date.today().isoformat(),
            "执行时限": "—",
            "执行人": "—",
            "执行结果": "待处理",
            "指令状态": DISPATCH_PENDING,
            "事项": subject,
            "来源": MODULE,
            "来源ID": int(station["id"]),
        }
        rows.append(order)
        return order

    def _sync_bus_change(
        self, station: dict[str, Any], from_state: str, to_state: str
    ) -> dict[str, Any]:
        """母线变化同步到调度待处理清单。

        同一条待执行变更单只刷新内容（不重复落单）；已经被调度接单或结案、
        驳回的老指令结论一律不动，仅新增一条待处理指令。
        """
        subject = (
            f"{station['升压站编号']}母线状态由「{from_state}」转为「{to_state}」，请调度确认"
        )
        waiting = self._find_open_dispatch(int(station["id"]), DISPATCH_BUS_CHANGE)
        if waiting is not None:
            waiting["事项"] = subject
            waiting["执行结果"] = "待处理"
            waiting["下发时间"] = date.today().isoformat()
            return waiting
        return self._append_dispatch(station, DISPATCH_BUS_CHANGE, subject)
