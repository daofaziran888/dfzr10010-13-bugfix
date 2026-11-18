"""表快照管理接口：维护快照记录，并与数据过期清理的结果清单/审计清单对账。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, PageResult
from app.services.snapshot import SnapshotService


def _extract_values(request_body: dict[str, Any]) -> dict[str, Any]:
    """兼容两种提交方式：{"values": {...}} 与扁平的 {"action": ...}。"""
    values = request_body.get("values")
    if isinstance(values, dict) and values:
        return values
    return request_body

router = APIRouter(prefix="/api/snapshot", tags=["表快照管理"])

service = SnapshotService()

LIST_FIELDS = ["快照编号", "表名称", "快照时间", "快照版本", "快照大小", "保留天数", "创建方式", "快照状态"]
STATUSES = ["已创建", "保留中", "已过期", "已清理"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按快照编号检索"),
    status: str | None = Query(default=None, description="已创建、保留中、已过期、已清理"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按快照编号与状态过滤表快照管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/cleanup/recoverable-refs")
def list_recoverable_refs(
    table: str | None = Query(default=None, description="按表名称过滤"),
) -> dict[str, Any]:
    """可恢复依据清单：回滚引用、被保护分区范围与对应快照，快照清单始终保留。"""
    items = service.list_recoverable_refs(table=table)
    return {"module": "snapshot_recoverable", "total": len(items), "items": items}


@router.get("/cleanup/states")
def list_cleanup_states() -> dict[str, Any]:
    """快照模块消费清理结果清单后保留的稳定记录（中断续跑时保留上一稳定记录）。"""
    items = service.list_cleanup_states()
    return {"module": "snapshot_cleanup_state", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出表快照管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "snapshot", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条快照记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"快照记录 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/cleanup/compatibility")
def check_cleanup_compatibility(entry_id: int) -> dict[str, Any]:
    """快照模块先校验清理审计清单中清理结果清单的兼容性，再保留已有明细。"""
    report = service.check_cleanup_compatibility(entry_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"快照记录 {entry_id} 不存在或已归档")
    return report


@router.post("/{entry_id}/cleanup/consume")
async def consume_cleanup_results(entry_id: int, request: Request) -> ActionResult:
    """快照模块消费同一批清理结果清单，并把版本依据同步到清理审计清单。"""
    body = await request.json()
    values = _extract_values(body)
    batch_id = str(values.get("batch") or values.get("batch_id") or "").strip()
    if not batch_id:
        return ActionResult(ok=False, message="缺少批次编号 batch，无法消费清理结果清单")
    state, message = service.consume_cleanup_results(entry_id, batch_id)
    if state is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=state)


@router.post("", response_model=ActionResult)
async def create_entry(request: Request) -> ActionResult:
    """登记一条快照记录，缺字段时说明原因而不是静默丢弃。"""
    body = await request.json()
    entry, missing = service.create_entry(_extract_values(body))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="快照记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
async def run_action(entry_id: int, request: Request) -> ActionResult:
    """对单条快照记录执行创建快照、恢复快照、清理快照；不允许的动作会被拦下并说明原因。"""
    body = await request.json()
    action = str(_extract_values(body).get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
