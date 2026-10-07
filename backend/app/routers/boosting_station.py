"""升压站监视接口：母线状态回写、站内送电、登记与导出。

路由顺序注意：/export 必须排在 /{entry_id} 之前，否则导出请求会被当成 entry_id 解析。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.boosting_station import BUS_STATES, BREAKER_BY_BUS, BoostingStationService

router = APIRouter(prefix="/api/boosting_station", tags=["升压站监视"])

service = BoostingStationService()

LIST_FIELDS = ["升压站编号", "进线电压", "出线电压", "主变容量", "母线状态", "断路器状态", "无功补偿", "运行状态"]
STATUSES = BUS_STATES
EXPORT_FIELDS = ["id", *LIST_FIELDS]


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip() in ("合闸", "1", "true", "True", "是", "yes")


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按升压站编号检索"),
    status: str | None = Query(default=None, description="运行、热备用、检修、停运"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按升压站编号与母线状态过滤；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出升压站监视清单：按编号排序的全量数据，字段口径与列表、详情完全一致。"""
    items = service.all_entries()
    clean = [{field: row.get(field, "") for field in EXPORT_FIELDS} for row in items]
    return {
        "module": "boosting_station",
        "total": len(clean),
        "bus_states": BUS_STATES,
        "breaker_by_bus": BREAKER_BY_BUS,
        "items": clean,
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条升压站明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"升压站 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条升压站，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="升压站已登记，母线默认热备用、断路器分闸", entry=entry)


@router.post("/{entry_id}/bus-state", response_model=ActionResult)
def change_bus_state(entry_id: int, payload: EntryPayload) -> ActionResult:
    """母线状态回写：严格按 运行→热备用→检修→停运 逐档流转，跨档/回退一律拦截。"""
    target = str(payload.values.get("target") or payload.values.get("母线状态") or "").strip()
    if not target:
        return ActionResult(ok=False, message="缺少目标母线状态 target")
    entry, message = service.change_bus_state(entry_id, target)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/{entry_id}/energize", response_model=ActionResult)
def energize(entry_id: int, payload: EntryPayload) -> ActionResult:
    """站内送电：仅热备用可送、必须确认断路器合闸；重复提交只落一条调度待处理单。"""
    breaker_closed = _parse_bool(payload.values.get("breaker_closed", payload.values.get("断路器状态")))
    request_id = str(payload.values.get("request_id") or "").strip() or None
    entry, message, changed = service.energize(
        entry_id, breaker_closed=breaker_closed, request_id=request_id
    )
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """老动作入口：恢复正常、检查保护、安排检修；结论兼容既有判定，内部走同一状态机。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    return ActionResult(ok=entry is not None, message=message, entry=entry)
