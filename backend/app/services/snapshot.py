"""表快照管理业务规则：状态流转、字段校验，以及与数据过期清理的对账。

取数链路：清理执行引擎产出「清理结果清单 cleanup_results」与「清理审计清单
cleanup_audits」，快照模块消费同一份结果清单：
- 先校验清理审计清单中清理结果清单的兼容性，再保留已有明细；
- 中断后只继续清理结果清单未完成分段，并保留快照模块上一稳定记录；
- 消费后把版本依据（快照编号/快照版本）同步回清理审计清单。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "snapshot"
RESULT_MODULE = "cleanup_results"
AUDIT_MODULE = "cleanup_audits"
REQUIRED_FIELDS = ["快照编号", "表名称", "快照时间"]
STATUS_ORDER = ["已创建", "保留中", "已过期", "已清理"]
ACTION_RULES = {"创建快照": "已创建", "恢复快照": "保留中", "清理快照": "已过期"}
NEGATIVE_ACTIONS = []

# 快照模块侧的消费记录（上一稳定记录），中断续跑时保留它而不是覆盖
SNAPSHOT_CLEANUP_STATE: list[dict[str, Any]] = []


class SnapshotService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("快照编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

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
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"快照记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表快照管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"快照记录已{action}"

    # ------------------------------------------------ 清理结果清单兼容性校验
    def check_cleanup_compatibility(self, entry_id: int) -> dict[str, Any] | None:
        """快照模块先校验清理审计清单中清理结果清单的兼容性，再保留已有明细。"""
        snapshot = store.find(MODULE, entry_id)
        if snapshot is None:
            return None
        table = snapshot.get("表名称")
        snapshot_code = snapshot.get("快照编号")
        audits = [row for row in store.rows(AUDIT_MODULE) if row.get("目标表名") == table]
        checks: list[dict[str, Any]] = []
        compatible = True
        for audit in audits:
            batch_id = audit.get("批次编号")
            results = [
                row for row in store.rows(RESULT_MODULE)
                if row.get("批次编号") == batch_id and row.get("目标表名") == table
            ]
            # 兼容性：审计清单登记的每个批次都必须能在同一份结果清单里找到明细
            detail_complete = len(results) > 0
            # 该快照若被回滚引用，则结果清单中对应分区必须仍是「已跳过/已暂停」，不能被删
            protected = [
                item for item in (audit.get("恢复引用") or [])
                if item.get("快照编号") == snapshot_code
            ]
            protected_kept = True
            for item in protected:
                hit = next(
                    (row for row in results
                     if row.get("分区值") == item.get("分区值")
                     and row.get("快照编号") == snapshot_code),
                    None,
                )
                if hit is None or hit.get("结果状态") not in ("已跳过", "已暂停"):
                    protected_kept = False
            batch_ok = detail_complete and protected_kept
            compatible = compatible and batch_ok
            checks.append({
                "批次编号": batch_id,
                "批次状态": audit.get("批次状态"),
                "结果明细数": len(results),
                "明细完整": detail_complete,
                "本快照保护分区": [item.get("分区值") for item in protected],
                "保护分区保留": protected_kept,
                "兼容": batch_ok,
            })
        return {
            "快照编号": snapshot_code,
            "表名称": table,
            "快照版本": snapshot.get("快照版本"),
            "兼容": compatible,
            "校验批次": checks,
            "说明": "清理审计清单引用的结果明细均可在同一份清理结果清单中核对，恢复链保护分区未被删除"
            if compatible else "存在审计清单与结果清单不一致或恢复链保护分区被清理的情况，请保留已有明细并人工复核",
        }

    # ------------------------------------------- 消费清理结果清单并回写版本依据
    def consume_cleanup_results(self, entry_id: int, batch_id: str) -> tuple[dict[str, Any] | None, str]:
        """快照模块消费同一批清理结果清单，再把版本依据同步到清理审计清单。

        中断批次只继续未完成分段；已确认（已删除/已跳过）的明细与快照模块上一稳定记录保留。
        """
        snapshot = store.find(MODULE, entry_id)
        if snapshot is None:
            return None, f"快照记录 {entry_id} 不存在或已归档"
        table = snapshot.get("表名称")
        results = [
            row for row in store.rows(RESULT_MODULE)
            if row.get("批次编号") == batch_id and row.get("目标表名") == table
        ]
        if not results:
            return None, f"批次 {batch_id} 在表 {table} 上没有清理结果清单可消费"
        audit = next(
            (row for row in store.rows(AUDIT_MODULE)
             if row.get("批次编号") == batch_id and row.get("目标表名") == table),
            None,
        )
        if audit is None:
            return None, f"批次 {batch_id} 的清理审计清单缺失，无法回写版本依据"

        unfinished = [row for row in results if row.get("结果状态") in ("失败", "中断")]
        # 中断后只继续未完成分段：已确认明细原样保留，不覆盖
        consumed = [
            {
                "分区范围": row.get("分区范围"),
                "结果状态": row.get("结果状态"),
                "删除行数": row.get("删除行数"),
                "快照编号": row.get("快照编号"),
                "回滚记录编号": row.get("回滚记录编号"),
            }
            for row in results
        ]
        version_basis = {
            "快照编号": snapshot.get("快照编号"),
            "快照版本": snapshot.get("快照版本"),
            "快照时间": snapshot.get("快照时间"),
            "消费批次": batch_id,
        }
        # 版本依据同步到清理审计清单（其它业务模块消费后同样落到这里）
        audit["版本依据"] = version_basis
        audit["兼容校验"] = self.check_cleanup_compatibility(entry_id)

        state = {
            "快照编号": snapshot.get("快照编号"),
            "批次编号": batch_id,
            "表名称": table,
            "版本依据": version_basis,
            "已消费明细数": len(consumed),
            "未完成分段": [row.get("分区范围") for row in unfinished],
        }
        # 保留快照模块上一稳定记录：同快照同批次只刷新，不新增不覆盖历史批次记录
        previous = next(
            (item for item in SNAPSHOT_CLEANUP_STATE
             if item["快照编号"] == snapshot.get("快照编号") and item["批次编号"] == batch_id),
            None,
        )
        if previous is None:
            SNAPSHOT_CLEANUP_STATE.append(state)
        else:
            previous.update(state)
        message = (
            f"快照 {snapshot.get('快照编号')} 已消费批次 {batch_id} 的清理结果清单（{len(consumed)} 条明细），"
            f"版本依据已同步到清理审计清单"
        )
        if unfinished:
            message += f"；{len(unfinished)} 个未完成分段仅续跑不重算：{'、'.join(row.get('分区范围', '') for row in unfinished)}"
        return state, message

    def list_cleanup_states(self) -> list[dict[str, Any]]:
        """快照模块侧保留的消费记录（含上一稳定记录）。"""
        return SNAPSHOT_CLEANUP_STATE

    # ------------------------------------------------------- 可恢复依据清单
    def list_recoverable_refs(self, table: str | None = None) -> list[dict[str, Any]]:
        """快照清单始终保留可恢复依据：回滚引用 + 被保护的分区范围 + 对应快照。"""
        refs: list[dict[str, Any]] = []
        for rollback in store.rows("rollback"):
            if table and rollback.get("表名称") != table:
                continue
            snapshot = next(
                (row for row in store.rows(MODULE) if row.get("快照编号") == rollback.get("回滚版本")),
                None,
            )
            refs.append({
                "表名称": rollback.get("表名称"),
                "回滚记录编号": rollback.get("记录编号"),
                "回滚状态": rollback.get("status"),
                "快照编号": rollback.get("回滚版本"),
                "快照版本": snapshot.get("快照版本") if snapshot else None,
                "分区范围": rollback.get("影响范围"),
            })
        return refs
