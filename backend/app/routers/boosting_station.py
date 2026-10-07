"""升压站监视接口：维护升压站，覆盖母线逐档流转、站内送电申请与清单导出。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.boosting_station import (
    BUS_STATES,
    DISPATCH_POWER_ON,
    POWER_ON_ACTION,
    BoostingStationService,
)

router = APIRouter(prefix="/api/boosting_station", tags=["升压站监视"])

service = BoostingStationService()

LIST_FIELDS = ["升压站编号", "进线电压", "出线电压", "主变容量", "母线状态", "断路器状态", "无功补偿", "运行状态"]
STATUSES = BUS_STATES


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按升压站编号检索"),
    status: str | None = Query(default=None, description="运行、热备用、检修、停运"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按升压站编号与母线状态过滤列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# /export 必须在 /{entry_id} 之前注册，否则字面量 export 会被当成 entry_id 匹配掉。
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出升压站监视清单：固定列序、同源状态，避免导出结果错位混乱。"""
    items, total = service.list_entries(page=1, size=10000)
    columns = ["id", *LIST_FIELDS]
    ordered = [{column: item.get(column, "—") for column in columns} for item in items]
    return {"module": "boosting_station", "total": total, "columns": columns, "items": ordered}


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
    return ActionResult(ok=True, message="升压站已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条升压站执行母线状态流转；跨档、回退、断路器未合就送运行都会被拦下。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/power-on", response_model=ActionResult)
def request_power_on(entry_id: int) -> ActionResult:
    """停运站申请站内送电：进调度指令待处理清单，重复提交只保留一条。"""
    entry, order, message = service.request_power_on(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"升压站": entry, DISPATCH_POWER_ON: order, "action": POWER_ON_ACTION})
