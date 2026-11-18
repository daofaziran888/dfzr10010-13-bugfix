"""数据过期清理业务规则：状态流转、清理执行引擎与清理结果/审计清单。

关键口径（与快照恢复链保护保持一致）：
- 清理行数只按「一次执行批次」累计；中断后继续清理只补未完成分段，已确认结果不覆盖。
- 被回滚引用覆盖的分区一律不能删除，结果清单保留跳过依据（回滚单 + 快照编号 + 分区范围）。
- 清理批次与回滚引用在同一分区范围出现版本冲突时：
  * 「跳过冲突分区」只跳过被引用快照覆盖的分区，其余分区照常清理；
  * 「整表暂停」暂停整张表本轮清理，快照清单始终保留可恢复依据。
- 失败、中断、异常、空态、无数据分区都保留说明并可重试；清理行数对不上时不覆盖已确认结果。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.seed import PARTITION_CATALOG
from app.store import store

MODULE = "cleanup"
RESULT_MODULE = "cleanup_results"
AUDIT_MODULE = "cleanup_audits"

REQUIRED_FIELDS = ["策略编号", "目标表名", "过期条件"]
OPTIONAL_FIELDS = ["清理方式", "执行周期", "最后清理", "清理行数", "策略状态"]

STATUS_ORDER = ["草稿", "已启用", "执行中", "已关闭"]
# 普通动作：动作 -> (允许的来源状态, 目标状态)
ACTION_RULES = {
    "启用清理": (["草稿"], "已启用"),
    "关闭清理": (["草稿", "已启用", "执行中"], "已关闭"),
}
# 执行类动作：执行期间短暂进入「执行中」，同步落账后回到「已启用」，不残留执行态
EXEC_ACTIONS = ["立即执行", "继续清理"]
# 仍视为有效、可保护分区的回滚单状态
ACTIVE_ROLLBACK_STATUS = ["待回滚", "回滚中", "已回滚"]
# 版本冲突的两种处理口径
CONFLICT_SKIP = "跳过冲突分区"
CONFLICT_PAUSE = "整表暂停"

# 过期条件形如「dt < 2026-08-01」
CONDITION_PATTERN = re.compile(
    r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op><|<=|>|>=|=|==|!=)\s*(?P<value>\d{4}-\d{2}-\d{2})\s*$"
)


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def parse_condition(condition: str) -> tuple[str, str, str] | None:
    """解析过期条件，返回 (分区字段, 比较符, 阈值日期)；无法解析时返回 None。"""
    if not condition:
        return None
    match = CONDITION_PATTERN.match(str(condition))
    if not match:
        return None
    return match.group("field"), match.group("op"), match.group("value")


def _partition_in_scope(partition_value: str, op: str, threshold: str) -> bool:
    """按 ISO 日期字符串比较分区值是否落入过期范围。"""
    if op in ("<",):
        return partition_value < threshold
    if op == "<=":
        return partition_value <= threshold
    if op == ">":
        return partition_value > threshold
    if op == ">=":
        return partition_value >= threshold
    if op in ("=", "=="):
        return partition_value == threshold
    if op == "!=":
        return partition_value != threshold
    return False


def _active_rollback_refs(table: str) -> list[dict[str, Any]]:
    """读取该表仍有效的回滚单（回滚引用），用于恢复链保护。"""
    refs: list[dict[str, Any]] = []
    for rollback in store.rows("rollback"):
        if rollback.get("表名称") != table:
            continue
        if rollback.get("status") not in ACTIVE_ROLLBACK_STATUS:
            continue
        scope_text = str(rollback.get("影响范围") or "")
        covered: set[str] = set()
        for part in re.split(r"[,，;；\s]+", scope_text):
            token = part.strip()
            if not token:
                continue
            # 同时保留「dt=2026-07-31」与裸值「2026-07-31」两种写法，比对时任一命中即保护
            covered.add(token)
            if "=" in token:
                covered.add(token.split("=", 1)[1].strip())
        refs.append({
            "记录编号": rollback.get("记录编号"),
            "回滚版本": rollback.get("回滚版本"),
            "影响范围": scope_text,
            "覆盖分区": covered,
            "回滚状态": rollback.get("status"),
        })
    return refs


def _find_snapshot(snapshot_code: str) -> dict[str, Any] | None:
    for snapshot in store.rows("snapshot"):
        if snapshot.get("快照编号") == snapshot_code:
            return snapshot
    return None


class CleanupService:
    # ------------------------------------------------------------------ 查询
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
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": _next_id(rows)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry.setdefault("清理方式", CONFLICT_SKIP)
        entry.setdefault("执行周期", "未配置")
        entry.setdefault("最后清理", "—")
        entry.setdefault("清理行数", 0)
        entry["status"] = STATUS_ORDER[0]
        entry["策略状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["最后批次"] = None
        rows.append(entry)
        return entry, []

    # ---------------------------------------------------------- 普通状态流转
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清理策略 {entry_id} 不存在或已归档"
        if action in EXEC_ACTIONS:
            return self._run_execution(entry, action)
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据过期清理可执行范围"
        allowed, target = ACTION_RULES[action]
        if entry["status"] not in allowed:
            return None, f"策略当前为「{entry['status']}」，不允许执行{action}（允许来源：{'、'.join(allowed)}）"
        entry["status"] = target
        entry["策略状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        # 关闭策略时清掉执行态残留，但保留历史清理结果与审计清单
        if action == "关闭清理":
            entry["abnormal"] = False
        return entry, f"清理策略已{action}"

    # -------------------------------------------------------------- 执行引擎
    def _run_execution(
        self, entry: dict[str, Any], action: str
    ) -> tuple[dict[str, Any] | None, str]:
        if entry["status"] == "已关闭":
            return None, "策略已关闭，不能再执行清理；如需清理请重新启用"
        if entry["status"] == "草稿":
            return None, "策略尚未启用，请先执行「启用清理」后再执行清理"
        if entry["status"] == "执行中":
            return None, "策略已有一轮清理正在执行中，请等待该批次结束后再操作"

        parsed = parse_condition(str(entry.get("过期条件") or ""))
        if parsed is None:
            return None, f"过期条件「{entry.get('过期条件')}」无法解析，应为形如 dt < 2026-08-01 的分区条件"
        field, op, threshold = parsed
        table = str(entry["目标表名"])
        mode = str(entry.get("清理方式") or CONFLICT_SKIP)

        result_rows = store.rows(RESULT_MODULE)
        audit_rows = store.rows(AUDIT_MODULE)

        resume_batch_id = entry.get("最后批次") if action == "继续清理" else None
        existing = [
            row for row in result_rows
            if row.get("策略编号") == entry.get("策略编号") and row.get("批次编号") == resume_batch_id
        ] if resume_batch_id else []
        confirmed_keys = {
            (row.get("分区字段"), row.get("分区值"))
            for row in existing
            if row.get("结果状态") in ("已删除", "已跳过")
        }
        if action == "继续清理" and not existing:
            return None, "没有找到中断未完成的清理批次，无需继续清理"

        # 进入执行中（同步执行，结束后一定回到已启用，不残留执行态）
        entry["status"] = "执行中"
        entry["策略状态"] = "执行中"

        today = date.today().isoformat()
        batch_id = resume_batch_id or self._allocate_batch_id(today)
        partitions = PARTITION_CATALOG.get(table, [])
        refs = _active_rollback_refs(table)
        candidates = [
            part for part in partitions
            if part.get("分区字段") == field and _partition_in_scope(str(part["分区值"]), op, threshold)
        ]

        batch_deleted_rows = 0
        skipped_partitions: list[str] = []
        protected_partitions: list[dict[str, Any]] = []
        failed_partitions: list[str] = []
        empty_partitions: list[str] = []
        conflict_pause = False
        notes: list[str] = []

        if not partitions:
            notes.append(f"表 {table} 在分区目录中无登记分区，本轮无清理范围")
        elif not candidates:
            notes.append(f"表 {table} 没有满足过期条件「{entry.get('过期条件')}」的分区，本轮空态无数据可清理")

        # 「整表暂停」模式：先对全部分区做冲突预判，任一分区被回滚引用覆盖则本轮一个分区都不删除
        pause_ref: dict[str, Any] | None = None
        pause_partition: str | None = None
        if mode == CONFLICT_PAUSE:
            for part in candidates:
                hit = next(
                    (ref for ref in refs if str(part["分区值"]) in ref["覆盖分区"]),
                    None,
                )
                if hit is not None:
                    pause_ref = hit
                    pause_partition = str(part["分区值"])
                    conflict_pause = True
                    notes.append(
                        f"版本冲突：批次 {batch_id} 与回滚单 {hit['记录编号']}（快照 {hit['回滚版本']}）"
                        f"在分区 {pause_partition} 范围冲突，按「整表暂停」暂停表 {table} 本轮清理，"
                        f"本批次不删除任何分区，快照清单保留全部可恢复依据"
                    )
                    break

        for part in candidates:
            key = (part.get("分区字段"), part.get("分区值"))
            partition_value = str(part["分区值"])
            # 恢复链保护：分区被任一有效回滚引用覆盖则不能删除
            hit_refs = [ref for ref in refs if partition_value in ref["覆盖分区"]]
            if hit_refs:
                ref = hit_refs[0]
                snapshot = _find_snapshot(str(ref["回滚版本"]))
                protected_partitions.append({
                    "分区值": partition_value,
                    "回滚单": ref["记录编号"],
                    "快照编号": ref["回滚版本"],
                    "快照版本": snapshot.get("快照版本") if snapshot else None,
                })
                if conflict_pause:
                    # 整表暂停已在预判阶段确定：该分区登记为暂停，其余候选分区在循环外统一暂停
                    self._upsert_result(
                        result_rows, batch_id, entry, part,
                        result_status="已暂停", deleted_rows=0,
                        skip_reason="整表暂停",
                        rollback_ref=ref["记录编号"], snapshot_ref=ref["回滚版本"],
                        note=f"版本冲突分区 {partition_value} 被回滚单 {ref['记录编号']} 引用的快照 {ref['回滚版本']} 覆盖，整表暂停",
                        confirmed_keys=confirmed_keys,
                    )
                    skipped_partitions.append(partition_value)
                    continue
                self._upsert_result(
                    result_rows, batch_id, entry, part,
                    result_status="已跳过", deleted_rows=0,
                    skip_reason="恢复链保护",
                    rollback_ref=ref["记录编号"], snapshot_ref=ref["回滚版本"],
                    note=f"分区被回滚单 {ref['记录编号']} 引用的快照 {ref['回滚版本']} 覆盖，禁止删除",
                    confirmed_keys=confirmed_keys,
                )
                skipped_partitions.append(partition_value)
                continue

            # 整表暂停：冲突已在预判阶段命中，未直接命中引用的候选分区同样暂停，绝不删除
            if conflict_pause:
                self._upsert_result(
                    result_rows, batch_id, entry, part,
                    result_status="已暂停", deleted_rows=0,
                    skip_reason="整表暂停",
                    note=f"表 {table} 本轮整表暂停（冲突分区 {pause_partition}，回滚单 {pause_ref['记录编号'] if pause_ref else '—'}），该分区不清理",
                    confirmed_keys=confirmed_keys,
                )
                skipped_partitions.append(partition_value)
                continue

            physical = str(part.get("状态") or "ready")
            row_count = int(part.get("行数") or 0)

            # 空态 / 无数据分区：跳过并保留说明，可重试
            if physical == "empty" or row_count <= 0:
                self._upsert_result(
                    result_rows, batch_id, entry, part,
                    result_status="已跳过", deleted_rows=0,
                    skip_reason="空态无数据",
                    note="分区存在但无数据，按空态保留说明，可在数据补齐后重试",
                    confirmed_keys=confirmed_keys,
                )
                empty_partitions.append(partition_value)
                continue

            # 异常分区：清理失败，保留说明并可重试，不产生删除行数
            if physical == "exception":
                self._upsert_result(
                    result_rows, batch_id, entry, part,
                    result_status="失败", deleted_rows=0,
                    skip_reason="分区异常",
                    note="分区处于异常状态，本轮清理失败，已保留现场并允许重试",
                    confirmed_keys=confirmed_keys,
                )
                failed_partitions.append(partition_value)
                continue

            # 可恢复异常：首轮失败；对同一批次重试时清理成功（演示失败保留说明并可重试）
            if physical == "retryable":
                prior = next(
                    (row for row in result_rows
                     if row.get("批次编号") == batch_id and row.get("分区值") == partition_value),
                    None,
                )
                if prior is None:
                    self._upsert_result(
                        result_rows, batch_id, entry, part,
                        result_status="失败", deleted_rows=0,
                        skip_reason="分区异常",
                        note="分区临时异常，本轮清理失败，已保留现场；下一轮继续清理可重试",
                        confirmed_keys=confirmed_keys,
                    )
                    failed_partitions.append(partition_value)
                    continue
                self._upsert_result(
                    result_rows, batch_id, entry, part,
                    result_status="已删除", deleted_rows=row_count,
                    skip_reason=None,
                    note="重试成功：分区异常已恢复，继续清理时删除未完成分段",
                    confirmed_keys=confirmed_keys,
                )
                batch_deleted_rows += row_count
                continue

            # 中断遗留 / 正常分区：续跑时只处理未确认分段，已确认结果原样保留
            if key in confirmed_keys:
                continue
            if physical == "interrupted":
                note = "上一轮在该分区中断，本轮继续清理未完成分段"
            else:
                note = "分区满足过期条件且无回滚引用保护，按批次删除"
            self._upsert_result(
                result_rows, batch_id, entry, part,
                result_status="已删除", deleted_rows=row_count,
                skip_reason=None, note=note,
                confirmed_keys=confirmed_keys,
            )
            batch_deleted_rows += row_count

        # 批次汇总：清理行数只按本次执行批次累计（续跑批次沿用同一批次号，只补未完成分段）
        batch_rows = [row for row in result_rows if row.get("批次编号") == batch_id]
        batch_total_deleted = sum(int(r.get("删除行数") or 0) for r in batch_rows)
        # 整表暂停时被引用分区登记为「已暂停」，同样要进入审计清单的恢复引用
        if conflict_pause and pause_ref is not None:
            for row in batch_rows:
                if row.get("快照编号") == pause_ref["回滚版本"] and not any(
                    item["分区值"] == row.get("分区值") for item in protected_partitions
                ):
                    snapshot = _find_snapshot(pause_ref["回滚版本"])
                    protected_partitions.append({
                        "分区值": row.get("分区值"),
                        "回滚单": pause_ref["记录编号"],
                        "快照编号": pause_ref["回滚版本"],
                        "快照版本": snapshot.get("快照版本") if snapshot else None,
                    })
        has_unfinished = any(r.get("结果状态") == "失败" for r in batch_rows)
        if conflict_pause:
            batch_status = "已暂停"
        elif has_unfinished:
            batch_status = "中断"
        else:
            batch_status = "已完成"

        conclusion = self._build_conclusion(
            entry, batch_id, batch_total_deleted, skipped_partitions,
            protected_partitions, failed_partitions, empty_partitions,
            conflict_pause, notes, candidates,
        )
        self._upsert_audit(
            audit_rows, batch_id, entry,
            batch_status=batch_status,
            deleted_rows=batch_total_deleted,
            skipped=skipped_partitions,
            protected=protected_partitions,
            failed=failed_partitions,
            empty=empty_partitions,
            conclusion=conclusion,
            conflict_pause=conflict_pause,
        )

        # 落账：策略上的清理行数只反映最近一次执行批次，不做跨批次累加
        entry["清理行数"] = batch_total_deleted
        entry["最后清理"] = today
        entry["最后批次"] = batch_id
        entry["status"] = "已启用"
        entry["策略状态"] = "已启用"
        entry["pending"] = True
        entry["abnormal"] = has_unfinished
        return entry, conclusion

    def _allocate_batch_id(self, today: str) -> str:
        existing = [row for row in store.rows(RESULT_MODULE) if str(row.get("批次编号", "")).startswith(f"BATCH-{today}")]
        seq = len({row.get("批次编号") for row in existing}) + 1
        return f"BATCH-{today}-{seq:02d}"

    def _upsert_result(
        self,
        result_rows: list[dict[str, Any]],
        batch_id: str,
        entry: dict[str, Any],
        part: dict[str, Any],
        *,
        result_status: str,
        deleted_rows: int,
        skip_reason: str | None,
        note: str,
        confirmed_keys: set[tuple[Any, Any]],
        rollback_ref: str | None = None,
        snapshot_ref: str | None = None,
    ) -> None:
        key = (part.get("分区字段"), part.get("分区值"))
        # 清理行数对不上 / 重试时，已确认（已删除、已跳过）结果一律不覆盖
        for row in result_rows:
            if (
                row.get("策略编号") == entry.get("策略编号")
                and row.get("批次编号") == batch_id
                and (row.get("分区字段"), row.get("分区值")) == key
            ):
                if row.get("结果状态") in ("已删除", "已跳过"):
                    return
                row.update({
                    "结果状态": result_status,
                    "删除行数": deleted_rows,
                    "跳过原因": skip_reason,
                    "回滚记录编号": rollback_ref,
                    "快照编号": snapshot_ref,
                    "说明": note,
                })
                return
        result_rows.append({
            "id": _next_id(result_rows),
            "批次编号": batch_id,
            "策略编号": entry.get("策略编号"),
            "目标表名": entry.get("目标表名"),
            "分区字段": part.get("分区字段"),
            "分区值": part.get("分区值"),
            "分区范围": f"{part.get('分区字段')}={part.get('分区值')}",
            "结果状态": result_status,
            "删除行数": deleted_rows,
            "跳过原因": skip_reason,
            "回滚记录编号": rollback_ref,
            "快照编号": snapshot_ref,
            "说明": note,
            "执行日期": date.today().isoformat(),
        })
        if result_status in ("已删除", "已跳过"):
            confirmed_keys.add(key)

    def _upsert_audit(
        self,
        audit_rows: list[dict[str, Any]],
        batch_id: str,
        entry: dict[str, Any],
        *,
        batch_status: str,
        deleted_rows: int,
        skipped: list[str],
        protected: list[dict[str, Any]],
        failed: list[str],
        empty: list[str],
        conclusion: str,
        conflict_pause: bool,
    ) -> None:
        payload = {
            "批次编号": batch_id,
            "策略编号": entry.get("策略编号"),
            "目标表名": entry.get("目标表名"),
            "过期条件": entry.get("过期条件"),
            "批次状态": batch_status,
            "清理行数": deleted_rows,
            "跳过分区": skipped,
            "恢复引用": [
                {
                    "分区值": item["分区值"],
                    "回滚单": item["回滚单"],
                    "快照编号": item["快照编号"],
                    "快照版本": item["快照版本"],
                }
                for item in protected
            ],
            "失败分区": failed,
            "空态分区": empty,
            "版本冲突": conflict_pause,
            "恢复结论": conclusion,
            "版本依据": None,
            "兼容校验": None,
            "执行日期": date.today().isoformat(),
        }
        for row in audit_rows:
            if row.get("批次编号") == batch_id and row.get("策略编号") == entry.get("策略编号"):
                # 已确认的清理行数不覆盖；其它说明字段刷新为最新一轮结论
                kept_rows = row.get("清理行数")
                payload["清理行数"] = kept_rows if kept_rows is not None else deleted_rows
                row.update(payload)
                return
        audit_rows.append({"id": _next_id(audit_rows), **payload})

    def _build_conclusion(
        self,
        entry: dict[str, Any],
        batch_id: str,
        deleted_rows: int,
        skipped: list[str],
        protected: list[dict[str, Any]],
        failed: list[str],
        empty: list[str],
        conflict_pause: bool,
        notes: list[str],
        candidates: list[dict[str, Any]],
    ) -> str:
        parts = [
            f"批次 {batch_id} 按过期条件「{entry.get('过期条件')}」清理表 {entry.get('目标表名')}",
            f"候选分区 {len(candidates)} 个，实际删除 {deleted_rows} 行（仅按本批次累计）",
        ]
        if protected:
            detail = "；".join(
                f"{item['分区值']}←回滚单 {item['回滚单']}/快照 {item['快照编号']}" for item in protected
            )
            parts.append(f"恢复链保护跳过 {len(protected)} 个分区（{detail}）")
        if skipped and not protected:
            parts.append(f"跳过分区：{'、'.join(skipped)}")
        if empty:
            parts.append(f"空态/无数据分区保留说明：{'、'.join(empty)}")
        if failed:
            parts.append(f"失败分区可重试：{'、'.join(failed)}")
        if conflict_pause:
            parts.append("本轮按整表暂停处理，快照清单保留全部可恢复依据")
        parts.extend(notes)
        return "；".join(parts)

    # ------------------------------------------------------ 结果清单 / 审计清单
    def list_results(
        self,
        *,
        policy_code: str | None = None,
        batch_id: str | None = None,
        table: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(RESULT_MODULE)
        if policy_code:
            rows = [row for row in rows if row.get("策略编号") == policy_code]
        if batch_id:
            rows = [row for row in rows if row.get("批次编号") == batch_id]
        if table:
            rows = [row for row in rows if row.get("目标表名") == table]
        return rows

    def list_audits(
        self,
        *,
        policy_code: str | None = None,
        batch_id: str | None = None,
        table: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(AUDIT_MODULE)
        if policy_code:
            rows = [row for row in rows if row.get("策略编号") == policy_code]
        if batch_id:
            rows = [row for row in rows if row.get("批次编号") == batch_id]
        if table:
            rows = [row for row in rows if row.get("目标表名") == table]
        return rows

    def get_batch_summary(self, batch_id: str) -> dict[str, Any] | None:
        audits = [row for row in store.rows(AUDIT_MODULE) if row.get("批次编号") == batch_id]
        if not audits:
            return None
        results = [row for row in store.rows(RESULT_MODULE) if row.get("批次编号") == batch_id]
        audit = audits[0]
        return {
            "批次编号": batch_id,
            "批次状态": audit.get("批次状态"),
            "清理行数": sum(int(row.get("删除行数") or 0) for row in results),
            "结果明细数": len(results),
            "审计": audit,
        }
