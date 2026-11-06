"""M5：行程修订。

两个入口，同一套机制（都是「拿着现有行程 + 一段改动要求，产出改动尽量小的新版」）：

- auto_repair：把 M3 质检的 error 当作改动要求，循环修到没有 error（或到上限）。
- revise_itinerary：把用户的自然语言反馈当作改动要求，产出 V2。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Template

from app.llm import LLMClient, get_client
from app.planner import PlanResult, _iter_dates, run_attempts
from app.schemas import Itinerary, TravelRequest
from app.validator import Finding, ValidationReport, ValidatorConfig, validate_itinerary

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_REVISE_SYSTEM = (_PROMPT_DIR / "revise_system.txt").read_text(encoding="utf-8")
_REVISE_USER = Template((_PROMPT_DIR / "revise_user.jinja").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 结构化 diff（给版本历史和「只改受影响部分」的核对用）
# --------------------------------------------------------------------------- #
_SHOWN = 4      # 一类改动在一行里最多点几个名字，其余写「等 N 项」


@dataclass
class ChangeSummary:
    changed_days: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)      # "日期 时间 标题"
    removed: list[str] = field(default_factory=list)
    booking_delta: int = 0
    lines: list[str] = field(default_factory=list)      # 按天写清楚改了哪些地点（给版本历史显示）
    counts: dict[str, int] = field(default_factory=dict)  # 新增 / 移除 / 搬天 / 调时间：搬走再搬来不算一增一删

    @property
    def touched(self) -> int:
        return len(self.added) + len(self.removed)

    def as_text(self) -> str:
        if not self.changed_days and not self.touched and not self.lines:
            return "无结构性改动"
        added = self.counts.get("新增", len(self.added))
        removed = self.counts.get("移除", len(self.removed))
        bits = [f"{len(self.changed_days)} 天有改动"] if self.changed_days else []
        if added:
            bits.append(f"新增 {added} 项")
        if removed:
            bits.append(f"移除 {removed} 项")
        if self.counts.get("搬天"):
            bits.append(f"换天 {self.counts['搬天']} 项")
        if self.counts.get("调时间"):
            bits.append(f"调时间 {self.counts['调时间']} 项")
        if self.booking_delta:
            bits.append(f"预约清单 {self.booking_delta:+d}")
        return "\n".join(["，".join(bits) or "有改动", *self.lines])


def _day_map(it: Itinerary) -> dict[str, set[tuple[str, str]]]:
    return {
        d.date.isoformat(): {(i.time, i.title) for i in d.items} for d in it.days
    }


def _names(names: list[str]) -> str:
    shown = "、".join(names[:_SHOWN])
    return f"{shown} 等 {len(names)} 项" if len(names) > _SHOWN else shown


def _describe(old: Itinerary, new: Itinerary) -> tuple[list[str], dict[str, int]]:
    """按天说清楚：移除 / 新增 / 搬到别的天 / 时间变了 / 锁定变了。只写地点库里的点，午餐、交通跟着变的不一条条列。"""
    number = {d.date.isoformat(): i + 1 for i, d in enumerate(new.days)}
    number.update({d.date.isoformat(): i + 1 for i, d in enumerate(old.days) if d.date.isoformat() not in number})

    def places(it: Itinerary) -> dict[str, tuple[str, str, str, bool]]:
        return {i.place_id: (d.date.isoformat(), i.title, i.time, i.locked)
                for d in it.days for i in d.items if i.place_id}

    before, after = places(old), places(new)
    per_day: dict[str, dict[str, list[str]]] = {}

    def note(date: str, kind: str, text: str) -> None:
        per_day.setdefault(date, {}).setdefault(kind, []).append(text)

    counts = {"新增": 0, "移除": 0, "搬天": 0, "调时间": 0}

    def others(it: Itinerary) -> set[tuple[str, str]]:
        return {(d.date.isoformat(), i.title) for d in it.days for i in d.items if not i.place_id}

    counts["新增"] += len(others(new) - others(old))                      # 午餐、交通这类没有地点 id 的，只计数
    counts["移除"] += len(others(old) - others(new))
    for pid in sorted(set(before) | set(after), key=lambda k: (after.get(k) or before[k])[:3:2]):
        if pid not in after:
            counts["移除"] += 1
            note(before[pid][0], "移除", before[pid][1])
        elif pid not in before:
            counts["新增"] += 1
            note(after[pid][0], "新增", after[pid][1])
        else:
            (d0, title, t0, lock0), (d1, _, t1, lock1) = before[pid], after[pid]
            if d0 != d1:
                counts["搬天"] += 1
                note(d1, "搬来", f"{title} 第 {number[d0]} 天 → 第 {number[d1]} 天")
            elif t0 != t1:
                counts["调时间"] += 1
                note(d1, "时间", f"{title} {t0} → {t1}")
            if lock0 != lock1:
                note(d1, "锁定" if lock1 else "解除锁定", title)
    lines = []
    for date in sorted(per_day):
        parts = [f"{kind} {_names(names)}" if kind in {"移除", "新增", "锁定", "解除锁定"} else _names(names)
                 for kind, names in per_day[date].items()]
        lines.append(f"第 {number[date]} 天：" + "；".join(parts))
    return lines, counts


def diff_itineraries(old: Itinerary, new: Itinerary) -> ChangeSummary:
    old_map, new_map = _day_map(old), _day_map(new)
    summary = ChangeSummary(booking_delta=len(new.booking_checklist) - len(old.booking_checklist))
    for date in sorted(set(old_map) | set(new_map)):
        before, after = old_map.get(date, set()), new_map.get(date, set())
        if before == after:
            continue
        summary.changed_days.append(date)
        for time, title in sorted(after - before):
            summary.added.append(f"{date} {time} {title}")
        for time, title in sorted(before - after):
            summary.removed.append(f"{date} {time} {title}")
    summary.lines, summary.counts = _describe(old, new)
    return summary


# --------------------------------------------------------------------------- #
# 核心修订
# --------------------------------------------------------------------------- #
@dataclass
class RevisionResult:
    itinerary: Itinerary
    change: ChangeSummary
    attempts: int
    warnings: list[str] = field(default_factory=list)


def revise_itinerary(
    itinerary: Itinerary,
    request: TravelRequest,
    *,
    instructions: str,
    client: LLMClient | None = None,
    max_retries: int = 1,
) -> RevisionResult:
    client = client or get_client()
    user = _REVISE_USER.render(
        current_json=itinerary.model_dump_json(indent=1),
        instructions=instructions.strip(),
        r=request,
        dates=_iter_dates(request),
    )
    result: PlanResult = run_attempts(
        client, _REVISE_SYSTEM, user, request, max_retries=max_retries
    )
    revised = result.itinerary.model_copy(update={"version": itinerary.version})
    return RevisionResult(
        itinerary=revised,
        change=diff_itineraries(itinerary, revised),
        attempts=result.attempts,
        warnings=result.warnings,
    )


# --------------------------------------------------------------------------- #
# 自动修复循环
# --------------------------------------------------------------------------- #
def findings_to_instructions(findings: list[Finding]) -> str:
    lines = ["请针对下列质检问题逐条修正，其余不动："]
    for f in findings:
        where = f" （{f.day}）" if f.day else ""
        lines.append(f"- {f.message}{where}")
    return "\n".join(lines)


@dataclass
class RepairRound:
    round: int
    fixed_codes: list[str]
    remaining_errors: int
    change: ChangeSummary


@dataclass
class RepairResult:
    itinerary: Itinerary
    report: ValidationReport
    rounds: list[RepairRound] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.report.ok


def auto_repair(
    itinerary: Itinerary,
    request: TravelRequest,
    *,
    client: LLMClient | None = None,
    max_rounds: int = 2,
    config: ValidatorConfig | None = None,
) -> RepairResult:
    client = client or get_client()
    current = itinerary
    rounds: list[RepairRound] = []

    for n in range(1, max_rounds + 1):
        report = validate_itinerary(current, request, config=config)
        if report.ok:
            break
        before_codes = {f.code for f in report.errors}
        revision = revise_itinerary(
            current, request,
            instructions=findings_to_instructions(report.errors),
            client=client,
        )
        current = revision.itinerary
        after = validate_itinerary(current, request, config=config)
        rounds.append(RepairRound(
            round=n,
            fixed_codes=sorted(before_codes - {f.code for f in after.errors}),
            remaining_errors=len(after.errors),
            change=revision.change,
        ))
        if after.ok:
            break

    return RepairResult(
        itinerary=current,
        report=validate_itinerary(current, request, config=config),
        rounds=rounds,
    )
