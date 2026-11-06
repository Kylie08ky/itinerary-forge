"""排程评测基线（规格 07 二点五，Q1）——开发者工具，不在产品界面里。

用法：python scripts/eval_schedule.py [--write-baseline] [--no-report]

对 tests/fixtures/schedule/requests.json 里的 20 份固定请求（外加几个「探针」）跑确定性排程器（不调模型），
量：硬约束违例、每天主要点数、空档、午 / 晚餐、直线公里、通勤、重复地点、混选时各主题的占比和权重偏差、耗时。
结果打印成表格；默认写 docs/reports/q1-schedule-baseline.md（保留文件里「解读」那一段）；
--write-baseline 把确定性部分（不含耗时）写进 tests/fixtures/schedule/baseline.json，
tests/test_eval_schedule.py 拿它当快照。

改了排程器、主题包或地点库之后重跑：指标变了先看是不是想要的变化，再重录基线。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from app.generator import generate_from_theme  # noqa: E402
from app.geo import haversine_km  # noqa: E402
from app.library import PlaceLibrary, load_library  # noqa: E402
from app.scheduler.engine import SchedulerConfig, _build_windows, idle_gaps, is_lunch, lunch_expected  # noqa: E402
from app.schemas import ThemeWeight, TravelRequest  # noqa: E402
from app.validator import ValidatorConfig  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "schedule" / "requests.json"
BASELINE = ROOT / "tests" / "fixtures" / "schedule" / "baseline.json"
REPORT = ROOT / "docs" / "reports" / "q1-schedule-baseline.md"
FINDINGS_MARK = "<!-- findings -->"
MAIN_ROLES = ("anchor", "must_go")


def load_cases(path: Path = FIXTURE) -> tuple[dict, list[dict], list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["base"], data["cases"], data["probes"]


def build_request(base: dict, case: dict) -> TravelRequest:
    """请求 = 共有字段 + 这份的日期（抵达 / 离开用「日期 + 时刻」）+ 这份自己的字段；探针可以绕过请求校验。"""
    start = date.fromisoformat(case["start"])
    end = start + timedelta(days=case["days"] - 1)
    body = {**base, **case["request"], "start_date": start.isoformat(), "end_date": end.isoformat(),
            "arrival_time": f"{start.isoformat()} {base['arrival_time']}",
            "departure_time": f"{end.isoformat()} {base['departure_time']}"}
    req = TravelRequest.model_validate(body)
    if case.get("bypass_validation"):
        # 不经过「最多 3 个主题」的校验
        req = req.model_copy(update={"themes": [ThemeWeight(**t) for t in case["themes"]]})
    return req


def _round(x: float, n: int = 2) -> float:
    return round(float(x), n)


def measure(req: TravelRequest, theme: str, lib: PlaceLibrary) -> dict:
    """生成一份行程（不调模型）并量各项指标。返回的字段里只有 elapsed_ms 每次会变。"""
    t0 = time.perf_counter()
    trip = generate_from_theme(req, theme, client=None, library=lib)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    draft, report = trip.draft, trip.report

    windows = {w.day: w for w in _build_windows(req, SchedulerConfig(), ValidatorConfig())}
    place_ids: list[str] = []
    per_day_main: list[int] = []
    per_day_places: list[int] = []
    fillers = 0
    idle_all: list[int] = []
    lunch_days = dinner_days = 0
    lunch_missing = short_days = 0
    km_days: list[float] = []
    legs: list[float] = []
    travel_min = 0
    for day in draft.days:
        stops = day.stops
        places = [s for s in stops if s.kind == "place"]
        place_ids += [s.place_id for s in places if s.place_id]
        per_day_places.append(len(places))
        per_day_main.append(sum(1 for s in places if s.role in MAIN_ROLES))
        # 「该有的」午饭（规格 07 P16）：这天窗口覆盖午饭时段（到得晚 / 走得早的不要求）；郊区一日游在景区内吃，不要求
        trip_day = any(lib.places[s.place_id].zone.value == "day_trip" for s in places if s.place_id in lib.places)
        if places and not trip_day and lunch_expected(windows[day.date]):
            lunch_missing += not any(is_lunch(s) for s in stops)
        # 「该有的」点数：能排景点的整天至少 2 个，到达 / 离开的半天至少 1 个；整天只留接驳的不算
        edge = any(s.kind == "logistics" for s in stops)
        if not any("不排景点" in r for r in day.reasons) and not trip_day:
            short_days += len(places) < (1 if edge else 2)
        fillers += sum(1 for s in places if s.role == "filler")
        idle_all += idle_gaps(stops)
        lunch_days += any(is_lunch(s) for s in stops)
        dinner_days += any(s.kind == "meal" and "晚餐" in s.title for s in stops)
        travel_min += sum(s.travel_min for s in places)
        pts = [(lib.places[s.place_id].lat, lib.places[s.place_id].lng) for s in places if s.place_id in lib.places]
        d = [haversine_km(a, b) for a, b in zip(pts, pts[1:], strict=False)]
        legs += d
        km_days.append(sum(d))

    selected = [t.id for t in req.themes] or [theme]
    weights = {t.id: t.weight for t in req.themes} or {theme: 3}
    members = {tid: {p.id for p, _m in lib.theme_members(tid)} for tid in selected}
    counts = {tid: sum(1 for pid in place_ids if pid in members[tid]) for tid in selected}
    total_c, total_w = sum(counts.values()), sum(weights.values())
    gap = 0.5 * sum(abs(weights[t] / total_w - (counts[t] / total_c if total_c else 0)) for t in selected)

    # 「主要归属」：一个点同时属于几个所选主题时只算给权重最高的那个（主要点优先，并列取先选的主题），避免重复计数
    strength = {tid: {p.id: (m.role.value == "anchor", m.weight) for p, m in lib.theme_members(tid)}
                for tid in selected}
    primary = dict.fromkeys(selected, 0)
    for pid in place_ids:
        best = max((tid for tid in selected if pid in members[tid]),
                   key=lambda tid: (strength[tid][pid], -selected.index(tid)), default=None)
        if best is not None:
            primary[best] += 1
    total_p = sum(primary.values())
    gap_primary = 0.5 * sum(abs(weights[t] / total_w - (primary[t] / total_p if total_p else 0)) for t in selected)

    days_n = len(draft.days)
    return {
        "status": "ok",
        "days": days_n,
        "places": len(place_ids),
        "distinct_places": len(set(place_ids)),
        "repeat_places": len(place_ids) - len(set(place_ids)),
        "errors": len(report.errors),
        "error_codes": sorted({f.code for f in report.errors}),
        "warnings": len(report.warnings),
        "warning_codes": sorted({f.code for f in report.warnings}),
        "main_per_day": per_day_main,
        "main_min": min(per_day_main),
        "main_avg": _round(statistics.fmean(per_day_main)),
        "places_per_day": per_day_places,
        "days_under_2_places": sum(1 for n in per_day_places if n < 2),
        "fillers": fillers,
        "idle_total_min": sum(idle_all),
        "idle_max_min": max(idle_all, default=0),
        "lunch_days": lunch_days,
        "lunch_missing": lunch_missing,
        "short_days": short_days,
        "min_theme_points": min(counts.values()),
        "dinner_days": dinner_days,
        "km_total": _round(sum(km_days)),
        "km_max_day": _round(max(km_days, default=0)),
        "legs_over_3km": sum(1 for x in legs if x > 3),
        "travel_total_min": travel_min,
        "themes": selected,
        "theme_counts": counts,
        "themes_covered": sum(1 for c in counts.values() if c > 0),
        "theme_weight_gap": _round(gap),
        "theme_counts_primary": primary,
        "theme_weight_gap_primary": _round(gap_primary),
        "notes": list(draft.notes),
        "elapsed_ms": _round(elapsed_ms, 1),
    }


def run_case(base: dict, case: dict, lib: PlaceLibrary) -> dict:
    try:
        req = build_request(base, case)
    except ValidationError as exc:
        msg = "；".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
        return {"status": "rejected", "reason": msg}
    try:
        return measure(req, case["theme"], lib)
    except Exception as exc:  # noqa: BLE001 —— 排程器撑不住也是要记下来的结果，不是评测脚本的错
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}


def run_all(path: Path = FIXTURE) -> dict:
    base, cases, probes = load_cases(path)
    lib = load_library("paris")
    return {
        "cases": {c["id"]: {"kind": c["kind"], "desc": c["desc"], **run_case(base, c, lib)} for c in cases},
        "probes": {c["id"]: {"desc": c["desc"], **run_case(base, c, lib)} for c in probes},
    }


def deterministic(results: dict) -> dict:
    """基线快照：去掉每次都会变的耗时。"""
    def strip(group: dict) -> dict:
        return {k: {f: v for f, v in r.items() if f != "elapsed_ms"} for k, r in group.items()}
    return {"cases": strip(results["cases"]), "probes": strip(results["probes"])}


# --------------------------------------------------------------------------- #
# 汇总与报告
# --------------------------------------------------------------------------- #
def summarize(results: dict) -> dict:
    ok = [r for r in results["cases"].values() if r["status"] == "ok"]
    return {
        "cases": len(results["cases"]),
        "ok": len(ok),
        "cases_with_errors": sum(1 for r in ok if r["errors"] > 0),
        "total_errors": sum(r["errors"] for r in ok),
        "total_warnings": sum(r["warnings"] for r in ok),
        "cases_with_short_days": sum(1 for r in ok if r["days_under_2_places"] > 0),
        "cases_with_repeats": sum(1 for r in ok if r["repeat_places"] > 0),
        "median_ms": _round(statistics.median(r["elapsed_ms"] for r in ok), 1) if ok else 0,
        "max_ms": _round(max((r["elapsed_ms"] for r in ok), default=0), 1),
    }


# 表格列：(表头, 取值函数)
COLUMNS = [
    ("天", lambda r: r["days"]),
    ("点", lambda r: r["places"]),
    ("主要点 最少/平均", lambda r: f"{r['main_min']}/{r['main_avg']}"),
    ("补的小点", lambda r: r["fillers"]),
    ("错误/警告", lambda r: f"{r['errors']}/{r['warnings']}"),
    ("空档分钟 总/最长", lambda r: f"{r['idle_total_min']}/{r['idle_max_min']}"),
    ("午/晚餐天数", lambda r: f"{r['lunch_days']}/{r['dinner_days']}"),
    ("缺午饭/短天", lambda r: f"{r['lunch_missing']}/{r['short_days']}"),
    ("主题最少点", lambda r: r["min_theme_points"]),
    ("公里 总/最远一天", lambda r: f"{r['km_total']}/{r['km_max_day']}"),
    (">3km 段", lambda r: r["legs_over_3km"]),
    ("重复点", lambda r: r["repeat_places"]),
    ("主题覆盖", lambda r: f"{r['themes_covered']}/{len(r['themes'])}"),
    ("权重偏差 总/主", lambda r: f"{r['theme_weight_gap']}/{r['theme_weight_gap_primary']}"),
    ("毫秒", lambda r: f"{r['elapsed_ms']:.0f}"),
]
HEADER = "| 请求 | " + " | ".join(c[0] for c in COLUMNS) + " |" + chr(10) + "|" + "---|" * (len(COLUMNS) + 1)


def _row(cid: str, r: dict) -> str:
    if r["status"] != "ok":
        return f"| {cid} | {r['status']}：{r.get('reason', '')[:60]} |" + " |" * (len(COLUMNS) - 1)
    return f"| {cid} | " + " | ".join(str(c[1](r)) for c in COLUMNS) + " |"


def render_report(results: dict, findings: str) -> str:
    s = summarize(results)
    lines = [
        "# Q1 排程评测基线",
        "",
        "> 由 `scripts/eval_schedule.py` 生成（确定性排程器，不调模型）。"
        "请求集见 `tests/fixtures/schedule/requests.json`，快照见 `tests/fixtures/schedule/baseline.json`。"
        "规格：[07 二点五](../../specs/07-polish-backlog.md)。",
        "",
        "## 总览",
        "",
        f"- 请求 {s['cases']} 份，能排出行程 {s['ok']} 份；"
        f"有质检**错误**的 {s['cases_with_errors']} 份（共 {s['total_errors']} 条），警告共 {s['total_warnings']} 条",
        f"- 有「某天不足 2 个点」的 {s['cases_with_short_days']} 份；出现重复地点的 {s['cases_with_repeats']} 份",
        f"- 排程耗时：中位数 {s['median_ms']:.0f} ms，最慢 {s['max_ms']:.0f} ms",
        "",
        "## 请求（20 份）",
        "",
        HEADER,
    ]
    lines += [_row(cid, r) for cid, r in results["cases"].items()]
    lines += ["", "## 探针（现在的校验会拒绝的、或超出建议范围的请求）", "", HEADER]
    lines += [_row(cid, r) for cid, r in results["probes"].items()]
    lines += ["", "## 每天的点数（看长行程后面几天是不是越来越少）", "",
              "| 请求 | 每天的点数 | 每天的主要点数 |", "|---|---|---|"]
    for cid, r in {**results["cases"], **results["probes"]}.items():
        if r["status"] == "ok" and r["days"] >= 5:
            lines.append(f"| {cid} | {r['places_per_day']} | {r['main_per_day']} |")
    lines += ["", "## 各主题的点数（混选 / 探针）", "", "| 请求 | 点数 |", "|---|---|"]
    for cid, r in {**results["cases"], **results["probes"]}.items():
        if r["status"] == "ok" and len(r["themes"]) > 1:
            lines.append(f"| {cid} | {r['theme_counts']} |")
    lines += [
        "",
        "指标说明：主要点 = 主要点 / 必去；补的小点 = 主要点不够排满时顺手补的公园、街区漫步这类（不算主要点）；"
        "空档 = 相邻两项之间扣掉通勤后还空着的分钟；公里 = 按顺序经过各点的直线距离；",
        "主题覆盖 = 所选主题里至少有一个点进了行程的个数；"
        "权重偏差 = 各主题点数占比与权重占比之差（0 = 完全按权重）。",
        "",
        FINDINGS_MARK,
    ]
    lines.append(findings)
    return "\n".join(lines) + "\n"


def existing_findings() -> str:
    if REPORT.exists():
        text = REPORT.read_text(encoding="utf-8")
        if FINDINGS_MARK in text:
            return text.split(FINDINGS_MARK, 1)[1].lstrip("\n").rstrip("\n")
    return "## 解读\n\n（待补）"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write-baseline", action="store_true", help="把确定性结果写进 baseline.json")
    ap.add_argument("--no-report", action="store_true", help="不写 docs/reports/q1-schedule-baseline.md")
    args = ap.parse_args()
    results = run_all()
    text = render_report(results, existing_findings())
    print(text.split(FINDINGS_MARK)[0])
    if not args.no_report:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(text, encoding="utf-8")
        print(f"\n报告：{REPORT.relative_to(ROOT)}")
    if args.write_baseline:
        BASELINE.write_text(json.dumps(deterministic(results), ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"基线：{BASELINE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
