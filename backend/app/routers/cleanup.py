"""数据过期清理接口：维护清理策略，覆盖启用清理、立即执行、继续清理、关闭清理等动作。

额外提供：
- GET /api/cleanup/results 清理结果清单（快照模块与清理列表共用同一份数据）
- GET /api/cleanup/audits  清理审计清单（恢复结论 + 其它模块回写的版本依据）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, PageResult
from app.services.cleanup import CleanupService


def _extract_values(request_body: dict[str, Any]) -> dict[str, Any]:
    """兼容两种提交方式：{"values": {...}} 与扁平的 {"action": ...}。"""
    values = request_body.get("values")
    if isinstance(values, dict) and values:
        return values
    return request_body

router = APIRouter(prefix="/api/cleanup", tags=["数据过期清理"])

service = CleanupService()

LIST_FIELDS = ["策略编号", "目标表名", "过期条件", "清理方式", "执行周期", "最后清理", "清理行数", "策略状态"]
STATUSES = ["草稿", "已启用", "执行中", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、执行中、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤数据过期清理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/results")
def list_results(
    policy: str | None = Query(default=None, description="按策略编号过滤"),
    batch: str | None = Query(default=None, description="按批次编号过滤"),
    table: str | None = Query(default=None, description="按目标表名过滤"),
) -> dict[str, Any]:
    """清理结果清单：按实际删除行数保留被恢复链保护的分区（已跳过/已暂停同样留痕）。"""
    items = service.list_results(policy_code=policy, batch_id=batch, table=table)
    deleted_rows = sum(int(item.get("删除行数") or 0) for item in items)
    return {
        "module": "cleanup",
        "total": len(items),
        "删除行数合计": deleted_rows,
        "items": items,
    }


@router.get("/audits")
def list_audits(
    policy: str | None = Query(default=None, description="按策略编号过滤"),
    batch: str | None = Query(default=None, description="按批次编号过滤"),
    table: str | None = Query(default=None, description="按目标表名过滤"),
) -> dict[str, Any]:
    """清理审计清单：恢复结论、跳过的恢复引用与分区范围，以及其它模块同步的版本依据。"""
    items = service.list_audits(policy_code=policy, batch_id=batch, table=table)
    return {"module": "cleanup_audit", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据过期清理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "cleanup", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条清理策略明细（含分区条件与清理范围）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"清理策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
async def create_entry(request: Request) -> ActionResult:
    """登记一条清理策略，缺字段时说明原因而不是静默丢弃。"""
    body = await request.json()
    entry, missing = service.create_entry(_extract_values(body))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="清理策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
async def run_action(entry_id: int, request: Request) -> ActionResult:
    """对单条清理策略执行启用清理、立即执行、继续清理、关闭清理；不允许的动作会被拦下并说明原因。"""
    body = await request.json()
    action = str(_extract_values(body).get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
