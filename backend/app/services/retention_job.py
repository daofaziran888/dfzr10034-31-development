"""保留清理领域服务：过期数据的合规保留、安全清理和预演确认。

接收生命周期策略的销毁阶段流转事件，执行数据保留清理操作。

按月分区清理的核心约束：
- 分区只有「截止日」不晚于预演基准日时才到期；
- 调查申请只冻结同一资产、且日期范围完整覆盖该分区的分区，其余分区照常清理；
- 预演可重算：已释放的行数从剩余范围里扣除，中断后恢复不会重复释放；
- 暂缓分区保留暂缓理由与调查申请编号，便于清理员单独核对留存行数。
"""
from __future__ import annotations

import calendar
import time as _time
from datetime import date
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 调查申请中视为「仍在冻结」的状态；解除或撤销后分区恢复可清理。
ACTIVE_HOLD_STATUSES = {"有效", "已批准", "批准"}


def _as_int(value: Any, default: int = 0) -> int:
    """把界面上的字符串行数安全转成整数（"-" / "" / None 都回退到默认值）。"""
    try:
        text = str(value).strip()
        if not text or text == "-":
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _month_bounds(partition: str) -> tuple[date, date]:
    """把 ``2026-05`` 解析成该月的首日与末日。"""
    text = str(partition).strip()
    try:
        year_text, month_text = text.split("-", 1)
        year, month = int(year_text), int(month_text)
        first = date(year, month, 1)
    except (TypeError, ValueError):
        raise ValueError(f"分区格式应为 YYYY-MM：{partition}")
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class RetentionJobService(BaseModuleService):
    MODULE = "retention_job"
    ENTITY = "清理计划"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["计划编号", "目标资产", "保留天数", "清理条件", "预演模式", "清理行数", "暂缓行数", "备份路径", "下次执行"]
    REQUIRED_FIELDS = ["计划编号", "目标资产", "保留天数"]
    STATUS_ORDER = ["草稿", "执行中", "已清理", "跳过"]
    ACTION_RULES = {"预演清理": "执行中", "执行清理": "已清理", "跳过清理": "跳过"}
    NEGATIVE_ACTIONS = []

    # ── 分区与例外登记 ────────────────────────────────────────

    def _partitions(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        return job.setdefault("分区明细", [])

    def _find_partition(self, job: dict[str, Any], partition: str) -> dict[str, Any] | None:
        key = str(partition).strip()
        for item in self._partitions(job):
            if item.get("分区") == key:
                return item
        return None

    def plan_partitions(self, job_id: int, partitions: list[dict[str, Any]]) -> dict[str, Any]:
        """登记清理计划下的按月分区；重跑时保留已释放行数，不重置清理进度。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "清理计划不存在"}
        if not partitions:
            return {"ok": False, "error": "至少需要一个按月分区"}

        planned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in partitions:
            if isinstance(raw, str):
                raw = {"分区": raw}
            partition = str(raw.get("分区") or "").strip()
            try:
                first, last = _month_bounds(partition)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            normalized = f"{first.year:04d}-{first.month:02d}"
            if normalized in seen:
                return {"ok": False, "error": f"分区重复：{normalized}"}
            seen.add(normalized)

            rows = _as_int(raw.get("数据行数"), 0)
            if rows < 0:
                return {"ok": False, "error": f"分区 {normalized} 的数据行数不能为负数"}
            cutoff = _parse_date(raw.get("截止日")) or last
            if cutoff < first:
                return {"ok": False, "error": f"分区 {normalized} 的截止日不能早于分区起始日"}

            existing = self._find_partition(job, normalized)
            if existing:
                released = _as_int(existing.get("已释放行数"), 0)
                if rows < released:
                    return {"ok": False, "error": f"分区 {normalized} 的数据行数不能小于已释放行数 {released}"}
                existing["数据行数"] = rows
                existing["起始日"] = first.isoformat()
                existing["截止日"] = cutoff.isoformat()
                planned.append(existing)
                continue

            entry = {
                "分区": normalized,
                "目标资产": job.get("目标资产", ""),
                "起始日": first.isoformat(),
                "截止日": cutoff.isoformat(),
                "数据行数": rows,
                "已释放行数": 0,
                "状态": "待清理",
                "暂缓理由": "",
                "调查申请编号": "",
                "暂缓登记时间": "",
                "释放批次": [],
            }
            planned.append(entry)

        # 保留未重新登记的历史分区（例如已释放分区），避免重跑时丢失清理明细。
        for existing in self._partitions(job):
            if existing.get("分区") not in seen:
                planned.append(existing)
        job["分区明细"] = planned
        job.setdefault("例外分区清单", [])
        job.setdefault("执行批次", [])
        job["updated_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")

        return {"ok": True, "job_id": job_id, "partitions": self._partitions(job)}

    def _matching_active_hold(
        self, job: dict[str, Any], part: dict[str, Any],
    ) -> dict[str, Any] | None:
        """找出冻结该分区的调查申请：同一资产且日期范围完整覆盖分区。"""
        part_start = _parse_date(part.get("起始日"))
        part_end = _parse_date(part.get("截止日"))
        for hold in job.get("例外分区清单", []):
            if hold.get("状态") not in ACTIVE_HOLD_STATUSES:
                continue
            if hold.get("分区") != part.get("分区"):
                continue
            if str(hold.get("目标资产", "")) != str(job.get("目标资产", "")):
                continue
            if str(hold.get("目标资产", "")) != str(part.get("目标资产", "")):
                continue
            hold_start = _parse_date(hold.get("起始日"))
            hold_end = _parse_date(hold.get("结束日"))
            if not part_start or not part_end or not hold_start or not hold_end:
                continue
            if hold_start <= part_start and part_end <= hold_end:
                return hold
        return None

    def register_hold(
        self,
        job_id: int,
        partition: str,
        request_id: str,
        reason: str,
        start: str | None = None,
        end: str | None = None,
        asset: str | None = None,
    ) -> dict[str, Any]:
        """按调查申请登记暂缓：只冻结该资产、该分区日期范围内的清理。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "清理计划不存在"}
        part = self._find_partition(job, partition)
        if not part:
            return {"ok": False, "error": f"分区 {partition} 未登记到该清理计划"}
        request_id = str(request_id or "").strip()
        reason = str(reason or "").strip()
        if not request_id:
            return {"ok": False, "error": "缺少调查申请编号"}
        if not reason:
            return {"ok": False, "error": "缺少暂缓理由"}
        if _as_int(part.get("已释放行数")) >= _as_int(part.get("数据行数")):
            return {"ok": False, "error": f"分区 {part['分区']} 已释放，不能再登记暂缓"}

        job_asset = str(job.get("目标资产", ""))
        if asset is not None and str(asset) != job_asset:
            return {"ok": False, "error": "调查申请资产与清理计划目标资产不一致，冻结不生效"}
        if str(part.get("目标资产", job_asset)) != job_asset:
            return {"ok": False, "error": "调查申请资产与分区资产不一致，冻结不生效"}

        part_start = _parse_date(part.get("起始日"))
        part_end = _parse_date(part.get("截止日"))
        hold_start = _parse_date(start) or part_start
        hold_end = _parse_date(end) or part_end
        if not hold_start or not hold_end:
            return {"ok": False, "error": "调查申请日期格式应为 YYYY-MM-DD"}
        if hold_start > hold_end:
            return {"ok": False, "error": "调查申请起始日不能晚于结束日"}
        if not part_start or not part_end:
            return {"ok": False, "error": f"分区 {part.get('分区')} 缺少有效日期范围"}
        if hold_start > part_start or hold_end < part_end:
            return {
                "ok": False,
                "error": (
                    f"调查申请 {hold_start}~{hold_end} 未完整覆盖分区 "
                    f"{part['分区']}（{part_start}~{part_end}），冻结不生效"
                ),
            }

        existing = self._matching_active_hold(job, part)
        if existing:
            return {
                "ok": False,
                "error": f"分区 {part['分区']} 已由调查申请 {existing.get('调查申请编号')} 暂缓",
            }

        hold = {
            "分区": part["分区"],
            "目标资产": job_asset,
            "调查申请编号": request_id,
            "起始日": hold_start.isoformat(),
            "结束日": hold_end.isoformat(),
            "暂缓理由": reason,
            "状态": "有效",
            "登记时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        job.setdefault("例外分区清单", []).append(hold)
        before = part.get("状态", "待清理")
        part["状态"] = "暂缓"
        part["暂缓理由"] = reason
        part["调查申请编号"] = request_id
        part["暂缓登记时间"] = hold["登记时间"]
        job["updated_at"] = hold["登记时间"]

        store.log_change(
            self.MODULE, job_id, "登记暂缓",
            before=before, after="暂缓",
            source="investigation_hold",
            scope=f"retention_job:{job_asset}:{part['分区']}",
        )
        return {"ok": True, "job_id": job_id, "hold": hold, "partition": part}

    def release_hold(self, job_id: int, partition: str, reason: str = "调查结束") -> dict[str, Any]:
        """解除调查冻结，分区恢复到可清理范围。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "清理计划不存在"}
        part = self._find_partition(job, partition)
        if not part:
            return {"ok": False, "error": f"分区 {partition} 未登记到该清理计划"}
        active = next(
            (
                hold for hold in job.get("例外分区清单", [])
                if hold.get("分区") == part.get("分区")
                and hold.get("状态") in ACTIVE_HOLD_STATUSES
            ),
            None,
        )
        if not active:
            return {"ok": False, "error": f"分区 {part['分区']} 当前没有有效的调查冻结"}

        active["状态"] = "已解除"
        active["解除时间"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        active["解除说明"] = str(reason or "调查结束")
        remaining = _as_int(part.get("数据行数")) - _as_int(part.get("已释放行数"))
        part["状态"] = "待清理" if remaining > 0 else "已释放"
        part["暂缓结束时间"] = active["解除时间"]

        store.log_change(
            self.MODULE, job_id, "解除暂缓",
            before="暂缓", after=part["状态"],
            source="investigation_hold",
            scope=f"retention_job:{job.get('目标资产', '')}:{part['分区']}",
        )
        return {"ok": True, "job_id": job_id, "hold": active, "partition": part}

    # ── 预演 / 执行 ───────────────────────────────────────────

    def _default_as_of(self, job: dict[str, Any], as_of: str | date | None) -> date:
        parsed = _parse_date(as_of) or _parse_date(job.get("预演基准日"))
        return parsed or date.today()

    def _evaluate_partition(
        self, job: dict[str, Any], part: dict[str, Any], as_of: date,
    ) -> dict[str, Any]:
        total = _as_int(part.get("数据行数"))
        released = _as_int(part.get("已释放行数"))
        remaining = max(total - released, 0)
        cutoff = _parse_date(part.get("截止日"))
        due = bool(cutoff) and cutoff <= as_of
        hold = self._matching_active_hold(job, part)

        if released >= total and total > 0:
            state = "已释放"
        elif not due:
            # 未到期分区不参与本轮清理，调查例外只作为提示保留，不冒充「暂缓」。
            state = "未到期"
        elif hold:
            state = "暂缓"
        else:
            state = "待清理"
        part["状态"] = state
        if hold:
            part["暂缓理由"] = hold.get("暂缓理由", "")
            part["调查申请编号"] = hold.get("调查申请编号", "")
        else:
            part["暂缓理由"] = ""
            part["调查申请编号"] = ""

        return {
            "分区": part.get("分区"),
            "目标资产": part.get("目标资产", job.get("目标资产", "")),
            "截止日": part.get("截止日"),
            "数据行数": total,
            "已释放行数": released,
            "剩余行数": remaining,
            "到期": due,
            "状态": state,
            "可释放行数": remaining if (due and not hold and remaining > 0) else 0,
            "暂缓理由": part.get("暂缓理由", ""),
            "调查申请编号": part.get("调查申请编号", ""),
            "暂缓登记时间": part.get("暂缓登记时间", ""),
        }

    def dry_run_cleanup(self, job_id: int, data_rows: int | None = None, as_of: str | date | None = None) -> dict[str, Any]:
        """预演清理：不实际删除，只统计将要清理的行数（Pipeline 4 方法）。

        分区模式下消费调查例外并重算可释放范围；已释放的行不会被重复计入。
        """
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "清理计划不存在"}

        partitions = self._partitions(job)
        if not partitions:
            # 未登记分区的旧计划沿用按整表行数预演的行为。
            rows = _as_int(data_rows if data_rows is not None else job.get("清理行数"))
            job["预演模式"] = "是"
            job["清理行数"] = str(rows)
            job["暂缓行数"] = "0"
            job["status"] = "执行中"
            job["pending"] = True
            return {
                "ok": True,
                "job_id": job_id,
                "asset": job.get("目标资产"),
                "will_clean": rows,
                "held_rows": 0,
                "mode": "dry_run",
                "as_of": self._default_as_of(job, as_of).isoformat(),
                "partitions": [],
                "releasable_partitions": [],
                "due_partitions": [],
                "held_partitions": [],
                "not_due_partitions": [],
                "released_rows": 0,
                "remaining_rows": rows,
                "holds": [],
                "validation": [],
            }

        as_of_date = self._default_as_of(job, as_of)
        evaluated = [self._evaluate_partition(job, part, as_of_date) for part in partitions]

        releasable = [item for item in evaluated if item["可释放行数"] > 0]
        held = [item for item in evaluated if item["状态"] == "暂缓"]
        due = [item for item in evaluated if item["到期"] and item["状态"] != "已释放"]
        not_due = [item for item in evaluated if not item["到期"] and item["状态"] != "已释放"]

        will_clean = sum(item["可释放行数"] for item in releasable)
        held_rows = sum(item["剩余行数"] for item in held)
        released_rows = sum(item["已释放行数"] for item in evaluated)
        remaining_rows = sum(item["剩余行数"] for item in evaluated if not item["状态"] == "已释放")

        # 校验仍有效的调查范围：分区未到期、申请失效等情况单独说明，便于清理员追查。
        validation: list[dict[str, Any]] = []
        active_holds = [h for h in job.get("例外分区清单", []) if h.get("状态") in ACTIVE_HOLD_STATUSES]
        for hold in active_holds:
            part = self._find_partition(job, hold.get("分区", ""))
            if part is None:
                validation.append({
                    "调查申请编号": hold.get("调查申请编号"),
                    "分区": hold.get("分区"),
                    "结论": "分区未登记，冻结不生效",
                })
                continue
            covered_start = _parse_date(hold.get("起始日"))
            covered_end = _parse_date(hold.get("结束日"))
            part_start = _parse_date(part.get("起始日"))
            part_end = _parse_date(part.get("截止日"))
            if not (covered_start and covered_end and part_start and part_end
                    and covered_start <= part_start and part_end <= covered_end):
                validation.append({
                    "调查申请编号": hold.get("调查申请编号"),
                    "分区": hold.get("分区"),
                    "结论": "调查申请日期未完整覆盖分区，冻结不生效",
                })
            elif not (hold.get("目标资产") == job.get("目标资产") == part.get("目标资产", job.get("目标资产"))):
                validation.append({
                    "调查申请编号": hold.get("调查申请编号"),
                    "分区": hold.get("分区"),
                    "结论": "调查申请资产与分区不一致，冻结不生效",
                })
            elif not part_end or part_end > as_of_date:
                validation.append({
                    "调查申请编号": hold.get("调查申请编号"),
                    "分区": hold.get("分区"),
                    "结论": "分区尚未到期，调查例外暂不参与本轮清理",
                })

        preview = {
            "as_of": as_of_date.isoformat(),
            "due_partitions": [item["分区"] for item in due],
            "held_partitions": [item["分区"] for item in held],
            "not_due_partitions": [item["分区"] for item in not_due],
            "will_clean": will_clean,
            "held_rows": held_rows,
            "released_rows": released_rows,
            "remaining_rows": remaining_rows,
            "partitions": evaluated,
            "holds": [dict(h) for h in active_holds],
            "validation": validation,
            "previewed_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        job["预演模式"] = "是"
        job["预演基准日"] = as_of_date.isoformat()
        job["预演快照"] = preview
        job["清理行数"] = str(will_clean)
        job["暂缓行数"] = str(held_rows)
        job["已释放行数"] = str(released_rows)
        job["status"] = "执行中"
        job["pending"] = True
        job["updated_at"] = preview["previewed_at"]

        return {
            "ok": True,
            "job_id": job_id,
            "asset": job.get("目标资产"),
            "mode": "dry_run",
            "as_of": as_of_date.isoformat(),
            "will_clean": will_clean,
            "held_rows": held_rows,
            "released_rows": released_rows,
            "remaining_rows": remaining_rows,
            "releasable_partitions": [
                {"分区": item["分区"], "可释放行数": item["可释放行数"]} for item in releasable
            ],
            "due_partitions": preview["due_partitions"],
            "held_partitions": preview["held_partitions"],
            "not_due_partitions": preview["not_due_partitions"],
            "partitions": evaluated,
            "holds": preview["holds"],
            "validation": validation,
        }

    def execute_cleanup(
        self,
        job_id: int,
        create_backup: bool = True,
        max_partitions: int | None = None,
    ) -> dict[str, Any]:
        """执行清理操作并创建备份（Pipeline 4 核心方法）。

        只释放本轮仍在可释放范围内的分区：已释放的行不会被再次清理，
        暂缓分区即使预演数据过期也不会被释放。
        """
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "清理计划不存在"}

        partitions = self._partitions(job)
        if not partitions:
            return self._execute_legacy(job_id, job, create_backup)

        if not job.get("预演快照"):
            return {"ok": False, "error": "缺少预演数据，请先执行「预演清理」以核对剩余可释放范围"}

        as_of_date = self._default_as_of(job, job.get("预演基准日"))
        # 执行始终沿用预演确认过的截止日边界；冻结与是否到期在释放前重新校验，
        # 因此中断后恢复不会再次释放上一段已经释放的行，暂缓分区也不会被误释放。
        evaluated = [self._evaluate_partition(job, part, as_of_date) for part in partitions]
        eligible = [item for item in evaluated if item["可释放行数"] > 0]

        interrupt_after = max_partitions if max_partitions is not None else len(eligible)
        interrupted = max_partitions is not None and len(eligible) > max_partitions
        batch_no = _as_int(job.get("执行批次序号")) + 1
        batch = f"B{batch_no}"

        released_this_run = 0
        released_batches: list[dict[str, Any]] = []
        backup_path = job.get("备份路径", "")

        if create_backup and eligible and interrupt_after > 0:
            snap = store.create_snapshot(
                f"pre-cleanup-{job_id}-{batch}",
                [self.MODULE, "change_capture"],
            )
            backup_path = f"backup/clenaup-snap-{snap['snapshot_id']}/"
            job["备份路径"] = backup_path

        for item in eligible[:max(interrupt_after, 0)]:
            part = self._find_partition(job, item["分区"])
            if part is None:
                continue
            released = item["可释放行数"]
            part["已释放行数"] = _as_int(part.get("已释放行数")) + released
            part["释放批次"] = [*part.get("释放批次", []), batch]
            part["释放时间"] = _time.strftime("%Y-%m-%d %H:%M:%S")
            remaining = _as_int(part.get("数据行数")) - _as_int(part.get("已释放行数"))
            part["状态"] = "已释放" if remaining <= 0 else "部分释放"
            released_this_run += released
            released_batches.append({"分区": item["分区"], "释放行数": released})

            store.log_change(
                self.MODULE, job_id, "释放分区",
                before=item["状态"], after=part["状态"],
                source="pipeline_4",
                scope=f"retention_job:{job.get('目标资产', '')}:{item['分区']}",
            )

        # 汇总跟随实际可释放范围重新计算：留存的暂缓行数保留在计划上。
        refreshed = [self._evaluate_partition(job, part, as_of_date) for part in partitions]
        held_rows = sum(item["剩余行数"] for item in refreshed if item["状态"] == "暂缓")
        remaining_rows = sum(item["剩余行数"] for item in refreshed if item["状态"] != "已释放")
        cumulative_released = sum(item["已释放行数"] for item in refreshed)

        job["清理行数"] = str(cumulative_released)
        job["已释放行数"] = str(cumulative_released)
        job["暂缓行数"] = str(held_rows)
        job["本轮释放行数"] = str(released_this_run)
        job["执行批次序号"] = batch_no
        job.setdefault("执行批次", []).append({
            "批次": batch,
            "基准日": as_of_date.isoformat(),
            "释放行数": released_this_run,
            "释放分区": released_batches,
            "备份路径": backup_path,
            "中断": interrupted,
            "时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        job["中断标记"] = interrupted
        job["预演模式"] = "否"
        # 预演基准日保持本轮确认过的边界，同时留一份最终预演快照，避免重跑时
        # 用旧预演结论覆盖实际释放结果。
        job["最终预演快照"] = job.get("预演快照") or {}
        job.setdefault("预演快照", {})["released_rows"] = cumulative_released
        job["预演快照"]["held_rows"] = held_rows
        job["预演快照"]["will_clean"] = sum(item["可释放行数"] for item in refreshed)
        job["updated_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")

        has_pending = any(item["可释放行数"] > 0 for item in refreshed)
        if interrupted or has_pending or held_rows > 0:
            # 仍有可释放的到期分区或有效冻结时，状态保持执行中，便于继续恢复；
            # 尚未到期的分区不算待处理，不会拖住本轮结论。
            job["status"] = "执行中"
            job["pending"] = True
        else:
            job["status"] = "已清理"
            job["pending"] = False

        if interrupted:
            event_type = "cleanup_interrupted"
        elif job["status"] == "已清理":
            event_type = "cleanup_completed"
        else:
            event_type = "cleanup_partial"

        self.emit_event(event_type, {
            "job_id": job_id,
            "asset_code": job.get("目标资产", ""),
            "batch": batch,
            "released": released_this_run,
            "held_rows": held_rows,
            "remaining_rows": remaining_rows,
            "backup_path": backup_path,
        })

        return {
            "ok": True,
            "job_id": job_id,
            "asset": job.get("目标资产"),
            "mode": "execute",
            "batch": batch,
            "released": released_this_run,
            "released_partitions": released_batches,
            "released_rows_total": cumulative_released,
            "held_rows": held_rows,
            "remaining_rows": remaining_rows,
            "interrupted": interrupted,
            "status": job["status"],
            "backup_path": backup_path,
            "partitions": refreshed,
            "rehearsal": job.get("预演基准日"),
            "pipeline": "lifecycle_policy->retention_job",
        }

    def _execute_legacy(self, job_id: int, job: dict[str, Any], create_backup: bool) -> dict[str, Any]:
        """未登记分区的旧计划：沿用整表清理行为。"""
        rows = _as_int(job.get("清理行数"))
        job["status"] = "已清理"
        job["pending"] = False
        job["清理行数"] = str(rows)
        job["暂缓行数"] = "0"

        if create_backup:
            snap = store.create_snapshot(f"pre-cleanup-{job_id}", [self.MODULE])
            job["备份路径"] = f"backup/clenaup-snap-{snap['snapshot_id']}/"

        store.log_change(
            self.MODULE, job_id, "执行清理",
            before="执行中", after="已清理",
            source="pipeline_4", scope=f"retention_job:资产{job.get('目标资产', '')}"
        )
        self.emit_event("cleanup_completed", {
            "job_id": job_id,
            "asset_code": job.get("目标资产", ""),
            "backup_path": job.get("备份路径", ""),
        })
        return {
            "ok": True,
            "job_id": job_id,
            "asset": job.get("目标资产"),
            "cleaned": rows,
            "released": rows,
            "backup_path": job.get("备份路径"),
            "interrupted": False,
            "pipeline": "lifecycle_policy->retention_job",
        }

    def skip_cleanup(self, job_id: int, reason: str) -> dict[str, Any]:
        """跳过清理（保留数据）。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "清理计划不存在"}
        job["status"] = "跳过"
        job["pending"] = False
        job["清理条件"] = f"已跳过：{reason}"
        store.log_change(
            self.MODULE, job_id, "跳过清理",
            before="执行中", after="跳过",
            source="manual_skip", scope=f"retention_job:资产{job.get('目标资产', '')}"
        )
        return {"ok": True, "job_id": job_id, "reason": reason}

    # ── 对账明细 ──────────────────────────────────────────────

    def get_cleanup_report(self, job_id: int, as_of: str | date | None = None) -> dict[str, Any]:
        """汇总清理明细、留存行数与仍有效的调查范围，供清理员单独核对。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "清理计划不存在"}
        as_of_date = self._default_as_of(job, as_of)
        partitions = [self._evaluate_partition(job, part, as_of_date) for part in self._partitions(job)]

        active_holds = []
        for hold in job.get("例外分区清单", []):
            if hold.get("状态") not in ACTIVE_HOLD_STATUSES:
                continue
            part = self._find_partition(job, hold.get("分区", ""))
            covered = False
            if part:
                covered = self._matching_active_hold(job, part) is not None
            active_holds.append({
                **hold,
                "覆盖分区": hold.get("分区"),
                "冻结生效": covered,
                "留存行数": next(
                    (item["剩余行数"] for item in partitions if item["分区"] == hold.get("分区")), 0
                ),
            })

        audit = [
            entry for entry in store.get_change_log(self.MODULE, job_id)
            if entry.get("变更类型") in ("释放分区", "登记暂缓", "解除暂缓")
        ]

        return {
            "ok": True,
            "job_id": job_id,
            "asset": job.get("目标资产"),
            "as_of": as_of_date.isoformat(),
            "status": job.get("status"),
            "rehearsal": job.get("预演快照"),
            "partitions": partitions,
            "totals": {
                "分区数": len(partitions),
                "到期分区数": sum(1 for item in partitions if item["到期"]),
                "已释放行数": sum(item["已释放行数"] for item in partitions),
                "本轮可释放行数": sum(item["可释放行数"] for item in partitions),
                "留存行数": sum(item["剩余行数"] for item in partitions if item["状态"] == "暂缓"),
                "未释放行数": sum(item["剩余行数"] for item in partitions if item["状态"] != "已释放"),
            },
            "active_holds": active_holds,
            "batches": job.get("执行批次", []),
            "audit": audit,
        }

    # ── 通用动作 ──────────────────────────────────────────────

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """把工作台上的通用动作接到分区清理流程上。"""
        job = store.find(self.MODULE, entry_id)
        if job is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "预演清理":
            result = self.dry_run_cleanup(entry_id)
            return (job, "清理计划已预演") if result.get("ok") else (None, str(result.get("error")))
        if action == "执行清理":
            result = self.execute_cleanup(entry_id)
            if not result.get("ok"):
                return None, str(result.get("error"))
            if result.get("interrupted"):
                return job, "清理计划已中断，可再次执行从剩余分区恢复"
            if result.get("status") == "已清理":
                return job, "清理计划已执行"
            return job, "清理计划已执行，仍有分区等待调查结束或到期"
        if action == "跳过清理":
            result = self.skip_cleanup(entry_id, "人工跳过（未填写原因）")
            return job, f"{self.ENTITY}已跳过清理"
        return super().run_action(entry_id, action)
