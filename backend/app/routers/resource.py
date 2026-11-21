"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.get("/{module}/{entry_id}/cleanup-report", response_model=dict)
def get_cleanup_report(module: str, entry_id: int, as_of: str | None = Query(default=None)) -> dict[str, Any]:
    """保留清理对账：清理明细、留存行数与仍有效的调查范围。"""
    service = require_service(module)
    if not hasattr(service, "get_cleanup_report"):
        raise HTTPException(status_code=404, detail=f"{service.ENTITY}不支持清理对账")
    report = service.get_cleanup_report(entry_id, as_of=as_of)
    if not report.get("ok"):
        raise HTTPException(status_code=404, detail=str(report.get("error")))
    return report


@router.post("/{module}/{entry_id}/partitions", response_model=ActionResult)
def plan_partitions(module: str, entry_id: int, payload: dict[str, Any]) -> ActionResult:
    """登记按月分区及数据行数；重跑时保留已释放进度。"""
    service = require_service(module)
    if not hasattr(service, "plan_partitions"):
        raise HTTPException(status_code=404, detail=f"{service.ENTITY}不支持分区登记")
    result = service.plan_partitions(entry_id, list(payload.get("partitions") or []))
    return ActionResult(
        ok=bool(result.get("ok")),
        message="分区已登记" if result.get("ok") else str(result.get("error")),
        entry=service.get_entry(entry_id),
    )


@router.post("/{module}/{entry_id}/holds", response_model=ActionResult)
def register_hold(module: str, entry_id: int, payload: dict[str, Any]) -> ActionResult:
    """按调查申请登记暂缓；只冻结对应资产与日期覆盖的分区。"""
    service = require_service(module)
    if not hasattr(service, "register_hold"):
        raise HTTPException(status_code=404, detail=f"{service.ENTITY}不支持调查暂缓")
    result = service.register_hold(
        entry_id,
        partition=str(payload.get("partition") or ""),
        request_id=str(payload.get("request_id") or ""),
        reason=str(payload.get("reason") or ""),
        start=payload.get("start"),
        end=payload.get("end"),
        asset=payload.get("asset"),
    )
    return ActionResult(
        ok=bool(result.get("ok")),
        message="调查暂缓已登记" if result.get("ok") else str(result.get("error")),
        entry=service.get_entry(entry_id),
    )


@router.post("/{module}/{entry_id}/dry-run", response_model=dict)
def dry_run(module: str, entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """预演清理，返回可释放范围、暂缓分区与留存行数。"""
    service = require_service(module)
    if not hasattr(service, "dry_run_cleanup"):
        raise HTTPException(status_code=404, detail=f"{service.ENTITY}不支持预演清理")
    body = payload or {}
    result = service.dry_run_cleanup(
        entry_id, data_rows=body.get("data_rows"), as_of=body.get("as_of"))
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=str(result.get("error")))
    return result


@router.post("/{module}/{entry_id}/execute", response_model=dict)
def execute_cleanup(module: str, entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行清理，可按分区数上限模拟中断；再次调用即从剩余范围恢复。"""
    service = require_service(module)
    if not hasattr(service, "execute_cleanup"):
        raise HTTPException(status_code=404, detail=f"{service.ENTITY}不支持执行清理")
    body = payload or {}
    limit = body.get("max_partitions")
    result = service.execute_cleanup(
        entry_id,
        create_backup=bool(body.get("create_backup", True)),
        max_partitions=int(limit) if limit is not None else None,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error")))
    return result


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
