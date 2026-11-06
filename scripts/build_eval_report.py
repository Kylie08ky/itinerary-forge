"""生成评测报告 docs/evaluation.md——报告里的数字全部由这个脚本现算，不手写，所以不会过期。

用法：python scripts/build_eval_report.py [--check] [--skip-test-count]

量什么（都是确定性的，不调模型，不联网）：
1. 数据可信度：每座策展城市有多少地点、官网 / 坐标 / 开放时间 / 价格来源核对了多少；
2. 排程：tests/fixtures/schedule/baseline.json 里那批固定请求的硬约束违例、午晚饭、重复地点、空档；
3. 一句话填表的规则项：tests/fixtures/intake/samples.json 里每句话的日期 / 天数 / 人数 / 预算等必须全对；
4. 测试规模：单元测试和浏览器验收各多少条（--skip-test-count 跳过，数字写「未统计」）。
需要真模型（Ollama）的评测不在这里跑，报告里只说明它们是什么、去哪看。

--check：不写文件，只检查 docs/evaluation.md 里除「生成于」之外的内容是否和现在的数据一致（过期就退出码 1）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 只看仓库里提交的数据：自动现建的城市（data/generated/）是每台机器自己建的，不进报告，否则别人复现不出同样的数字
os.environ["CITY_GENERATED_DIR"] = tempfile.mkdtemp(prefix="atp_eval_empty_")

from app.cities import supported_cities  # noqa: E402
from app.library import load_library  # noqa: E402

OUT = ROOT / "docs" / "evaluation.md"
BASELINE = ROOT / "tests" / "fixtures" / "schedule" / "baseline.json"
TIERS = [("full", "精校（完整库）"), ("light", "轻量（一日游目的地）")]
OVERLAYS = ROOT / "data" / "city-overlays"


def pct(n: int, total: int) -> str:
    return "—" if total == 0 else f"{round(100 * n / total)}%"


def data_section() -> list[str]:
    rows: dict[str, list[tuple[str, dict]]] = {t: [] for t, _ in TIERS}
    for city in supported_cities():
        places = list(load_library(city.id).places.values())
        if not places:
            continue
        n = len(places)
        with_url = [p for p in places if p.official_url]
        rows[city.tier].append((city.name_zh, {
            "n": n,
            "draft": sum(p.data_status == "draft" for p in places),
            "stamped": sum(p.verified_at is not None for p in places),
            "url": len(with_url),
            "url_ok": sum(p.url_status is not None for p in with_url),
            "coords": sum(p.coords_verified for p in places),
            "hours": sum(p.hours_verified for p in places),
            "paid": sum(1 for p in places if p.price_eur),
            "price_src": sum(1 for p in places if p.price_eur and p.price_source_url and p.price_checked_at),
        }))
    out = ["## 一、数据可信度", "",
           "事实（官网、坐标、闭馆日、价格）只信带核对记录的策展数据。下表是每座城市里这些核对的覆盖率；",
           "没核对的在页面上写「以官网为准」，不编。", ""]
    out[2] = "事实（官网、坐标、闭馆日、价格）只信带核对记录的策展数据。下表是仓库里提交的城市数据的核对覆盖率；"
    for tier, label in TIERS:
        cities = rows[tier]
        if not cities:
            continue
        total = {k: sum(c[1][k] for c in cities) for k in cities[0][1]}
        out += [f"**{label}**：{len(cities)} 座城市，共 {total['n']} 个地点；"
                f"自动现建的草稿 {pct(total['draft'], total['n'])}，"
                f"有核对日期的 {pct(total['stamped'], total['n'])}，"
                f"官网链接已自动核对 {pct(total['url_ok'], total['url'])}，"
                f"坐标已核对 {pct(total['coords'], total['n'])}，"
                f"开放时间已核对 {pct(total['hours'], total['n'])}，"
                f"收费地点带价格来源和日期 {pct(total['price_src'], total['paid'])}。", ""]
        if True:
            out += ["| 城市 | 地点 | 自动现建的草稿 | 有核对日期 | 官网已核对 | 坐标已核对 | "
                    "开放时间已核对 | 价格有来源 |",
                    "|---|---:|---:|---:|---:|---:|---:|---:|"]
            for name, c in sorted(cities, key=lambda x: -x[1]["n"]):
                out.append(f"| {name} | {c['n']} | {pct(c['draft'], c['n'])} | {pct(c['stamped'], c['n'])} | "
                           f"{pct(c['url_ok'], c['url'])} | "
                           f"{pct(c['coords'], c['n'])} | {pct(c['hours'], c['n'])} | "
                           f"{pct(c['price_src'], c['paid'])} |")
            out.append("")
    out += overlay_section()
    out += ["「有核对日期」指这个地点至少有一项事实（官网、坐标或闭馆日）被脚本核对过，并记了日期。"
            "意大利十座城市的闭馆日和官网是这样补核的，所以它们属于标准档但已有部分核对。", "",
            "核对脚本见 `scripts/verify_library.py`、`scripts/apply_italy_facts.py`、`scripts/build_route.py` 等；",
            "流程都是「写提纲 → 一条命令核对（来源能打开、数字在来源里出现、坐标和官网能核对）→ 通过才写入」。", ""]
    return out


def overlay_section() -> list[str]:
    rows = []
    for f in sorted(OVERLAYS.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        places = list(d.get("places", {}).values())
        if not places:
            continue
        rows.append((d.get("city", f.stem), len(places),
                     sum(bool(p.get("closed_weekdays") or p.get("closed_dates")) for p in places),
                     sum(bool(p.get("hours_verified")) for p in places),
                     sum(bool(p.get("official_url")) for p in places)))
    if not rows:
        return []
    out = ["**精校补充（`data/city-overlays/`）**：自动现建的城市里，对重点景点补写的、带来源和日期的事实"
           "（闭馆日、开放时间、官网）。这些城市本身的数据不在仓库里，第一次用到时按需现建，"
           "所以这里只统计补充的部分。", "",
           "| 城市 | 补充的地点 | 其中有闭馆日 | 开放时间已核对 | 有官网 |", "|---|---:|---:|---:|---:|"]
    for city, n, closed, hours, url in rows:
        out.append(f"| {city} | {n} | {closed} | {hours} | {url} |")
    return [*out, f"合计 {sum(r[1] for r in rows)} 个地点。", ""]


def schedule_section() -> list[str]:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    cases = data["cases"]
    ok = [c for c in cases.values() if c.get("status") == "ok"]
    n = len(ok)
    days = sum(c["days"] for c in ok)
    short = sum(c["short_days"] for c in ok)
    out = ["## 二、排程", "",
           "排程器是纯算法，不调模型，同样的输入每次得到同样的结果。下面是 20 份固定请求"
           "（8 个主题、混选、2 / 5 / 7 / 10 天、周一周二开头、必去 + 不去）上的指标，"
           "快照在 `tests/fixtures/schedule/baseline.json`。", "",
           "| 指标 | 结果 |", "|---|---|",
           f"| 请求数 / 总天数 | {n} / {days} |",
           f"| 硬约束违例（质检 error） | {sum(c['errors'] for c in ok)} |",
           f"| 质检警告 | {sum(c['warnings'] for c in ok)} |",
           f"| 重复地点 | {sum(c['repeat_places'] for c in ok)} |",
           f"| 该有午饭却没有的天 | {sum(c['lunch_missing'] for c in ok)} |",
           f"| 整天不足 2 个地点的天（到达 / 离开日要求 1 个） | {short} |",
           f"| 最长空档（分钟，最大值） | {max(c['idle_max_min'] for c in ok)} |",
           f"| 单段直线超过 3 公里的次数 | {sum(c['legs_over_3km'] for c in ok)} |", "",
           "改动排程器、主题或地点库后，`tests/test_eval_schedule.py` 会指出是哪份请求的哪个指标变了；"
           "确认是想要的变化，再用 `python scripts/eval_schedule.py --write-baseline` 重录。完整表格和解读见 "
           "[`docs/reports/q1-schedule-baseline.md`](reports/q1-schedule-baseline.md)。", ""]
    return out


def intake_section() -> list[str]:
    from scripts.eval_intake import DEFAULT_FILE, load_samples, run_eval

    today, samples = load_samples(DEFAULT_FILE)
    stats = run_eval(None, today, samples, load_library("paris"))
    rows = stats["rows"]
    rule_total = sum(len(r["rule_bad"]) for r in rows)
    out = ["## 三、一句话填表（规则部分）", "",
           "用户写一句话，系统读出日期、天数、人数、预算、必去、不去等。这些由规则读，**必须全对**；"
           "主题和节奏的理解才交给模型补充，且模型的输出只能落在固定的几个字段里。", "",
           "| 指标 | 结果 |", "|---|---|",
           f"| 评测句数 | {len(samples)} |",
           f"| 规则项错误 | {stats['rule_errors']} |",
           f"| 被注入的句子带偏了字段 | {stats['led_astray']} |",
           f"| 缺少该有的提醒 | {stats['notes_missing']} |", "",
           f"评测集 `tests/fixtures/intake/samples.json`，日期固定为 {today}。"
           + ("全部规则项读对。" if rule_total == 0 else f"有 {rule_total} 处规则项读错，需要修。"), ""]
    return out


def tests_section(count: bool) -> list[str]:
    def collected(args: list[str]) -> str:
        if not count:
            return "未统计"
        r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        m = re.search(r"(\d+)(?:/\d+)? tests? collected", r.stdout) or re.search(r"^(\d+) tests", r.stdout, re.M)
        return m.group(1) if m else "未统计"

    unit = collected([])
    e2e = collected(["-m", "e2e", "tests/e2e"])
    return ["## 四、测试规模", "",
            "| 类型 | 条数 | 怎么跑 |", "|---|---:|---|",
            f"| 单元与接口测试 | {unit} | `python -m pytest -q` |",
            f"| 浏览器验收（Playwright，独立前后端 + 临时数据库） | {e2e} | `python -m pytest -m e2e tests/e2e` |", "",
            "一次完整运行的结果和没覆盖到的部分见 [`VALIDATION.md`](../VALIDATION.md)。", ""]


def model_section() -> list[str]:
    return ["## 五、需要真模型的评测（不在这份报告里重跑）", "",
            "下面这些要本机开着 Ollama，会跑几十分钟，结果只打印，换模型或改提示词后自己重跑对比：", "",
            "| 评测 | 命令 | 量什么 |", "|---|---|---|",
            "| 本地模型对比 | `python -m scripts.eval_models` | 7b 与 14b 的生成质量与耗时，"
            "结论见 [`model-eval.md`](model-eval.md) |",
            "| 一句话填表（含模型） | `python scripts/eval_intake.py` | 主题 / 节奏的召回与多读 |",
            "| 笔记抽取 | `python scripts/eval_notes.py` | 库内召回 / 精确率、库外地点进待核实、提示注入是否带偏 |",
            "| 一句话改行程 | `python scripts/eval_nl_edit.py` | 做对了、只改该改的、新增地点都来自地点库 |",
            "| 主题契合度评委 | `python scripts/eval_judge.py` | 评委能否区分「对得上」与「冒充」 |", "",
            "已有结论（见 `model-eval.md`）：默认的 `qwen2.5:7b-instruct` 不比 14b 差且快 5–10 倍；"
            "剩下的硬错误是约束问题，不是模型尺寸问题。模型写的说明只是表达层，生成后逐条检查，"
            "不合格的整段丢掉换成模板，所以模型质量不影响行程是否正确。", ""]


def limits_section() -> list[str]:
    return ["## 六、没有覆盖的", "",
            "- 真实模型输出的文笔和稳定性（见上一节，要自己跑）。",
            "- 高德、维基媒体等外部服务的实时调用：测试用录制的数据，不打真实接口。",
            "- 数据的「句子有没有被曲解」：脚本只能核对数字、名字和来源能不能打开，措辞要人看。",
            "- 自动现建的城市（标准档）：价格、预约、部分闭馆日没有核对，行程里会写「以官网为准」。", ""]


def build(count: bool) -> str:
    head = ["# 评测报告", "",
            f"> 由 `scripts/build_eval_report.py` 生成于 {date.today().isoformat()}，数字都是现算的；不要手改。", "",
            "这个项目的质量靠三件事保证：事实只信核对过的数据、排程交给确定性算法、模型只负责表达且有检查。"
            "这份报告量前两件，以及模型相关的评测去哪看。", "",
            "复现：`python scripts/build_eval_report.py`（不调模型、不联网、一两分钟）。", ""]
    return "\n".join(head + data_section() + schedule_section() + intake_section()
                     + tests_section(count) + model_section() + limits_section())


def strip_date(text: str) -> str:
    return re.sub(r"生成于 \d{4}-\d{2}-\d{2}", "生成于 —", text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只检查 docs/evaluation.md 是否过期")
    ap.add_argument("--skip-test-count", action="store_true")
    args = ap.parse_args()
    text = build(count=not (args.skip_test_count or args.check))
    if args.check:
        # 检查不数测试条数（慢），其余部分必须一致
        have = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        drop = re.compile(r"^\| (单元与接口测试|浏览器验收).*\n", re.M)
        a = drop.sub("", strip_date(have))
        b = drop.sub("", strip_date(text))
        if a != b:
            print("docs/evaluation.md 已过期，请重新运行 python scripts/build_eval_report.py")
            return 1
        print("评测报告是最新的。")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"已写入 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
