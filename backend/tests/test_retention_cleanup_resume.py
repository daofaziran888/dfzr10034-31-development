"""保留清理中断恢复：分区到期校验、调查冻结、预演重算与释放量对账。"""
import app.store as store_mod
from app.routers import resource
from app.services.retention_job import RetentionJobService


def _fresh_job(rows: dict[str, int] | None = None):
    store_mod.store.reset()
    service = RetentionJobService()
    job, missing = service.create_entry({
        "计划编号": "RETE-TEST-001",
        "目标资产": "核心交易表",
        "保留天数": "90",
        "清理条件": "状态=归档",
        "预演模式": "是",
        "备份路径": "backup-hdfs",
        "下次执行": "-",
    })
    assert not missing
    counts = rows or {"2026-05": 120_000, "2026-06": 98_000, "2026-07": 105_000}
    planned = service.plan_partitions(job["id"], [
        {"分区": partition, "数据行数": count} for partition, count in counts.items()
    ])
    assert planned["ok"], planned
    return service, job


def _part(result: dict, partition: str) -> dict:
    return next(item for item in result["partitions"] if item["分区"] == partition)


def test_hold_needs_investigation_scope_covering_the_partition():
    service, job = _fresh_job()
    partial = service.register_hold(
        job["id"], "2026-05", "INV-2026-051", "反洗钱调查",
        start="2026-05-10", end="2026-05-31",
    )
    assert partial["ok"] is False
    assert "未完整覆盖" in partial["error"]

    ok = service.register_hold(
        job["id"], "2026-05", "INV-2026-051", "反洗钱调查",
        start="2026-05-01", end="2026-05-31",
    )
    assert ok["ok"] is True
    assert ok["partition"]["状态"] == "暂缓"
    assert ok["partition"]["暂缓理由"] == "反洗钱调查"


def test_hold_only_freezes_its_own_asset_and_partition():
    service, job = _fresh_job()
    wrong_asset = service.register_hold(
        job["id"], "2026-05", "INV-OTHER", "其它资产调查",
        start="2026-05-01", end="2026-05-31", asset="日汇总表",
    )
    assert wrong_asset["ok"] is False

    service.register_hold(
        job["id"], "2026-05", "INV-2026-051", "反洗钱调查",
        start="2026-05-01", end="2026-05-31",
    )
    preview = service.dry_run_cleanup(job["id"], as_of="2026-06-30")
    assert preview["held_partitions"] == ["2026-05"]
    assert preview["due_partitions"] == ["2026-05", "2026-06"]
    assert preview["will_clean"] == 98_000
    assert _part(preview, "2026-06")["状态"] == "待清理"
    assert _part(preview, "2026-06")["可释放行数"] == 98_000


def test_interrupted_run_resumes_without_releasing_rows_twice():
    service, job = _fresh_job()
    service.register_hold(
        job["id"], "2026-05", "INV-2026-051", "反洗钱调查",
        start="2026-05-01", end="2026-05-31",
    )
    service.dry_run_cleanup(job["id"], as_of="2026-06-30")

    first = service.execute_cleanup(job["id"], max_partitions=0)
    assert first["interrupted"] is True
    assert first["released"] == 0
    assert first["held_rows"] == 120_000
    assert first["status"] == "执行中"
    assert job["中断标记"] is True

    second = service.execute_cleanup(job["id"])
    assert second["interrupted"] is False
    assert second["released"] == 98_000
    assert [item["分区"] for item in second["released_partitions"]] == ["2026-06"]
    assert second["released_rows_total"] == 98_000
    assert second["held_rows"] == 120_000
    assert _part(second, "2026-05")["已释放行数"] == 0
    assert _part(second, "2026-05")["状态"] == "暂缓"
    assert _part(second, "2026-07")["状态"] == "未到期"

    third = service.execute_cleanup(job["id"])
    # 六月已经释放过，重跑不会再清理同一段行；五月仍被调查申请冻结。
    assert third["released"] == 0
    assert third["released_rows_total"] == 98_000
    assert len(job["执行批次"]) == 3


def test_interruption_between_two_due_partitions_does_not_release_released_rows():
    service, job = _fresh_job()
    service.dry_run_cleanup(job["id"], as_of="2026-12-31")

    first = service.execute_cleanup(job["id"], max_partitions=2)
    assert first["interrupted"] is True
    assert [item["分区"] for item in first["released_partitions"]] == ["2026-05", "2026-06"]
    assert first["released_rows_total"] == 218_000
    assert first["held_rows"] == 0

    second = service.execute_cleanup(job["id"])
    assert second["interrupted"] is False
    assert [item["分区"] for item in second["released_partitions"]] == ["2026-07"]
    assert second["released"] == 105_000
    assert second["released_rows_total"] == 323_000
    assert _part(second, "2026-05")["已释放行数"] == 120_000
    assert _part(second, "2026-06")["已释放行数"] == 98_000
    assert second["status"] == "已清理"


def test_hold_added_after_interruption_blocks_resume_of_other_partition():
    service, job = _fresh_job()
    service.dry_run_cleanup(job["id"], as_of="2026-06-30")
    first = service.execute_cleanup(job["id"], max_partitions=1)
    assert first["released"] == 120_000  # 五月按登记顺序先释放

    service.register_hold(
        job["id"], "2026-06", "INV-2026-061", "税务协查",
        start="2026-06-01", end="2026-06-30",
    )
    second = service.execute_cleanup(job["id"])
    assert second["released"] == 0
    assert second["held_rows"] == 98_000
    assert _part(second, "2026-06")["状态"] == "暂缓"

    service.release_hold(job["id"], "2026-06", "协查结束")
    third = service.execute_cleanup(job["id"])
    assert third["released"] == 98_000
    assert third["held_rows"] == 0
    assert third["status"] == "已清理"
    assert third["released_rows_total"] == 218_000


def test_dry_run_recomputes_after_release_and_keeps_traceable_reason():
    service, job = _fresh_job()
    service.register_hold(
        job["id"], "2026-05", "INV-2026-051", "反洗钱调查",
        start="2026-05-01", end="2026-05-31",
    )
    service.dry_run_cleanup(job["id"], as_of="2026-06-30")
    service.execute_cleanup(job["id"])
    service.release_hold(job["id"], "2026-05")

    replay = service.dry_run_cleanup(job["id"], as_of="2026-06-30")
    may = _part(replay, "2026-05")
    assert may["可释放行数"] == 120_000
    assert may["状态"] == "待清理"
    assert may["暂缓理由"] == ""  # 冻结已解除，不再冒充本轮暂缓
    assert replay["held_rows"] == 0
    assert replay["released_rows"] == 98_000  # 六月已释放量仍可追查
    assert replay["not_due_partitions"] == ["2026-07"]
    assert _part(replay, "2026-07")["可释放行数"] == 0
    # 解除后的申请不再算仍有效的调查范围，仅在审批记录里留痕。
    assert replay["holds"] == []
    assert [hold["状态"] for hold in job["例外分区清单"]] == ["已解除"]

    service.execute_cleanup(job["id"])
    report = service.get_cleanup_report(job["id"], as_of="2026-06-30")
    assert report["totals"]["已释放行数"] == 218_000
    assert report["totals"]["留存行数"] == 0
    assert _part(report, "2026-07")["状态"] == "未到期"


def test_report_lists_hold_reason_and_retained_rows_for_clerk():
    service, job = _fresh_job()
    service.register_hold(
        job["id"], "2026-05", "INV-2026-051", "反洗钱调查",
        start="2026-05-01", end="2026-05-31",
    )
    service.dry_run_cleanup(job["id"], as_of="2026-06-30")
    service.execute_cleanup(job["id"], max_partitions=1)

    report = service.get_cleanup_report(job["id"], as_of="2026-06-30")
    assert report["totals"]["已释放行数"] == 98_000
    assert report["totals"]["留存行数"] == 120_000
    assert report["totals"]["到期分区数"] == 2
    assert report["active_holds"][0]["调查申请编号"] == "INV-2026-051"
    assert report["active_holds"][0]["暂缓理由"] == "反洗钱调查"
    assert report["active_holds"][0]["留存行数"] == 120_000
    assert [item["分区"] for item in report["batches"][0]["释放分区"]] == ["2026-06"]
    assert any(entry["变更类型"] == "释放分区" for entry in report["audit"])


def test_execute_requires_preview_before_release():
    service, job = _fresh_job()
    blocked = service.execute_cleanup(job["id"])
    assert blocked["ok"] is False
    assert "预演" in blocked["error"]
    assert all(part["已释放行数"] == 0 for part in job["分区明细"])


def test_router_exposes_partition_preview_execute_and_report():
    store_mod.store.reset()
    job, _ = RetentionJobService().create_entry({
        "计划编号": "RETE-HTTP-001", "目标资产": "核心交易表",
        "保留天数": "90", "清理条件": "状态=归档",
        "预演模式": "是", "备份路径": "-", "下次执行": "-",
    })
    planned = resource.plan_partitions("retention_job", job["id"], {"partitions": [
        {"分区": "2026-05", "数据行数": 100},
        {"分区": "2026-06", "数据行数": 200},
        {"分区": "2026-07", "数据行数": 300},
    ]})
    assert planned.ok
    held = resource.register_hold("retention_job", job["id"], {
        "partition": "2026-05", "request_id": "INV-1", "reason": "调查",
        "start": "2026-05-01", "end": "2026-05-31",
    })
    assert held.ok
    preview = resource.dry_run("retention_job", job["id"], {"as_of": "2026-06-30"})
    assert preview["will_clean"] == 200
    executed = resource.execute_cleanup("retention_job", job["id"], {"max_partitions": 1})
    assert executed["released"] == 200
    report = resource.get_cleanup_report("retention_job", job["id"], as_of="2026-06-30")
    assert report["totals"]["留存行数"] == 100


def test_seeded_job_resumes_the_interrupted_preview():
    store_mod.store.reset()
    service = RetentionJobService()
    job = service.get_entry(1)
    assert job["中断标记"] is True
    assert job["分区明细"][1]["已释放行数"] == 98_000

    replay = service.dry_run_cleanup(1)
    assert replay["as_of"] == "2026-06-30"
    assert replay["will_clean"] == 0
    assert replay["released_rows"] == 98_000
    assert replay["held_partitions"] == ["2026-05"]
    assert replay["not_due_partitions"] == ["2026-07"]
    may = _part(replay, "2026-05")
    assert may["暂缓理由"] == "反洗钱调查取证中"
    assert may["调查申请编号"] == "INV-2026-051"
    assert _part(replay, "2026-07")["可释放行数"] == 0

    resumed = service.execute_cleanup(1)
    assert resumed["released"] == 0
    assert resumed["released_rows_total"] == 98_000

    report = service.get_cleanup_report(1)
    assert report["totals"]["已释放行数"] == 98_000
    assert report["totals"]["留存行数"] == 120_000
    assert [item["分区"] for item in report["batches"][0]["释放分区"]] == ["2026-06"]
