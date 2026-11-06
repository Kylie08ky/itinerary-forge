"""命令行入口。

    python -m app.cli plan   <request.json> [--backend stub] [--repair] [--save] [--render] [--out F]
    python -m app.cli validate <itinerary.json> <request.json>
    python -m app.cli render   <itinerary.json> [request.json] [--out-dir D] [--formats md,html,pdf]
    python -m app.cli revise   <plan_id> "不想早起，多加一个咖啡馆" [--render]
    python -m app.cli history  <plan_id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.intake import run_intake
from app.llm import LLMConfig, LLMError, get_client
from app.planner import generate_itinerary
from app.renderer import RenderError, render_files
from app.revision import auto_repair, revise_itinerary
from app.schemas import Itinerary, TravelRequest
from app.validator import Severity, ValidationReport, validate_itinerary
from app.versions import PlanStore

_ICON = {Severity.error: "✗", Severity.warning: "!", Severity.info: "i"}


def _print_report(report: ValidationReport) -> None:
    print(f"\n[质检] {report.summary()}", file=sys.stderr)
    for f in report.findings:
        loc = f" @{f.day}" + (f"#{f.item_index}" if f.item_index is not None else "") if f.day else ""
        print(f"  {_ICON[f.severity]} [{f.code}]{loc} {f.message}", file=sys.stderr)
    print("[质检] 无 error，可进入人工审核" if report.ok
          else "[质检] 存在 error，需要修正或重新生成", file=sys.stderr)


def _render(itinerary: Itinerary, out_base: Path, formats: tuple[str, ...],
            request: TravelRequest | None, report: ValidationReport | None) -> None:
    try:
        res = render_files(itinerary, out_base.parent, name=out_base.stem, formats=formats,
                           request=request, report=report)
    except RenderError as exc:
        print(f"[渲染] PDF 跳过：{exc}", file=sys.stderr)
        res = render_files(itinerary, out_base.parent, name=out_base.stem,
                           formats=tuple(f for f in formats if f != "pdf"),
                           request=request, report=report)
    for label, path in (("md", res.markdown), ("html", res.html), ("pdf", res.pdf)):
        if path:
            print(f"[渲染] {label} → {path}", file=sys.stderr)


def _load_request(path: str | None) -> TravelRequest | None:
    if not path:
        return None
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    raw.pop("_meta", None)
    return TravelRequest.model_validate(raw)


def _make_client(backend: str | None):
    config = LLMConfig.from_env()
    if backend:
        config = LLMConfig(**{**config.__dict__, "backend": backend})
    print(f"[LLM] backend={config.backend} model={config.ollama_model}", file=sys.stderr)
    return get_client(config), config


# --------------------------------------------------------------------------- #
def _cmd_plan(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.request).read_text(encoding="utf-8"))
    intake = run_intake(raw)
    if not intake.ok:
        print("需求还不完整，需要先补充：\n", file=sys.stderr)
        for q in intake.questions:
            print(f"  · {q}", file=sys.stderr)
        for e in intake.errors:
            print(f"  ! {e}", file=sys.stderr)
        return 2
    for q in intake.soft_questions:
        print(f"[建议补充] {q}", file=sys.stderr)

    client, config = _make_client(args.backend)
    try:
        result = generate_itinerary(intake.request, client=client, max_retries=2)
    except LLMError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        if config.backend == "stub":
            print("  提示：stub 样例只对 persona-01 有效。", file=sys.stderr)
        return 1
    print(f"[ok] {result.attempts} 次尝试后得到合法行程", file=sys.stderr)
    itinerary = result.itinerary

    if args.repair:
        report0 = validate_itinerary(itinerary, intake.request)
        if report0.ok:
            print("[修复] 首版已无 error，跳过", file=sys.stderr)
        else:
            repair = auto_repair(itinerary, intake.request, client=client, max_rounds=args.repair_rounds)
            for r in repair.rounds:
                fixed = "、".join(r.fixed_codes) or "（无）"
                print(f"[修复] 第 {r.round} 轮：修掉 {fixed}；{r.change.as_text()}；剩余 error {r.remaining_errors}",
                      file=sys.stderr)
            itinerary = repair.itinerary

    report = None
    if not args.no_validate:
        report = validate_itinerary(itinerary, intake.request)
        _print_report(report)

    plan_id = None
    if args.save:
        store = PlanStore()
        plan_id = store.create(intake.request, itinerary, note="via cli plan")
        print(f"[保存] plan_id = {plan_id}", file=sys.stderr)

    out_path = Path(args.out) if args.out else None
    if args.render and out_path is None:
        base = plan_id or Path(args.request).stem
        out_path = Path("samples/generated") / f"{base}.json"

    output = itinerary.model_dump_json(indent=2)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        print(f"已写入 {out_path}", file=sys.stderr)
    elif not args.save:
        print(output)

    if args.render:
        _render(itinerary, out_path.with_suffix(""), ("md", "html", "pdf"), intake.request, report)

    return 0 if (report is None or report.ok) else 3


def _cmd_validate(args: argparse.Namespace) -> int:
    itinerary = Itinerary.model_validate_json(Path(args.itinerary).read_text(encoding="utf-8"))
    report = validate_itinerary(itinerary, _load_request(args.request))
    _print_report(report)
    return 0 if report.ok else 3


def _cmd_render(args: argparse.Namespace) -> int:
    itinerary = Itinerary.model_validate_json(Path(args.itinerary).read_text(encoding="utf-8"))
    request = _load_request(args.request)
    report = validate_itinerary(itinerary, request) if request else None
    formats = tuple(f.strip() for f in args.formats.split(",") if f.strip())
    name = args.name or Path(args.itinerary).stem
    _render(itinerary, Path(args.out_dir) / name, formats, request, report)
    return 0


def _cmd_revise(args: argparse.Namespace) -> int:
    store = PlanStore()
    request = store.request(args.plan_id)
    current = store.load(args.plan_id)
    v_from = store.latest_version(args.plan_id)
    errors_before = len(validate_itinerary(current, request).errors)

    client, _ = _make_client(args.backend)
    revision = revise_itinerary(current, request, instructions=args.feedback, client=client)
    report = validate_itinerary(revision.itinerary, request)

    version = store.add_version(
        args.plan_id, revision.itinerary,
        trigger="user-feedback", note=args.feedback,
        change_summary=revision.change.as_text(),
        errors_before=errors_before, errors_after=len(report.errors),
    )
    print(f"[修订] v{v_from} → v{version}", file=sys.stderr)
    print(f"[修订] 改动：{revision.change.as_text()}", file=sys.stderr)
    for line in revision.change.added:
        print(f"    + {line}", file=sys.stderr)
    for line in revision.change.removed:
        print(f"    - {line}", file=sys.stderr)
    _print_report(report)

    if args.render:
        plan_dir = PlanStore().root / args.plan_id
        _render(revision.itinerary, plan_dir / f"v{version}", ("md", "html", "pdf"), request, report)
    return 0 if report.ok else 3


def _cmd_history(args: argparse.Namespace) -> int:
    store = PlanStore()
    print(f"plan_id: {args.plan_id}")
    for r in store.history(args.plan_id):
        note = f"  「{r.note}」" if r.note else ""
        delta = f"  error {r.errors_before}→{r.errors_after}" if r.trigger != "initial" else ""
        print(f"  v{r.version}  {r.created_at}  {r.trigger}{delta}")
        if r.change_summary:
            print(f"        {r.change_summary}")
        if note:
            print(f"      {note.strip()}")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="根据需求文件生成行程 JSON")
    p_plan.add_argument("request", help="需求 JSON 文件路径（可用 data/personas 下的画像）")
    p_plan.add_argument("--backend", choices=["ollama", "stub"], help="覆盖 .env 的 LLM_BACKEND")
    p_plan.add_argument("--no-validate", action="store_true", help="跳过质检")
    p_plan.add_argument("--repair", action="store_true", help="质检有 error 时自动喂回 LLM 修复")
    p_plan.add_argument("--repair-rounds", type=int, default=2, help="自动修复最多轮数")
    p_plan.add_argument("--save", action="store_true", help="存入版本库并打印 plan_id")
    p_plan.add_argument("--render", action="store_true", help="生成后一并渲染 md/html/pdf")
    p_plan.add_argument("--out", help="把行程写到该文件")
    p_plan.set_defaults(func=_cmd_plan)

    p_val = sub.add_parser("validate", help="对已有行程跑质检")
    p_val.add_argument("itinerary")
    p_val.add_argument("request", help="对应的需求 JSON 文件路径")
    p_val.set_defaults(func=_cmd_validate)

    p_ren = sub.add_parser("render", help="把行程 JSON 渲染成 md/html/pdf")
    p_ren.add_argument("itinerary")
    p_ren.add_argument("request", nargs="?", help="对应需求文件，用于表头信息和质检徽标")
    p_ren.add_argument("--out-dir", default="samples/generated")
    p_ren.add_argument("--formats", default="md,html,pdf")
    p_ren.add_argument("--name", help="输出文件名（默认取行程文件名）")
    p_ren.set_defaults(func=_cmd_render)

    p_rev = sub.add_parser("revise", help="按反馈修订某个 plan，生成新版本")
    p_rev.add_argument("plan_id")
    p_rev.add_argument("feedback", help="自然语言修改要求")
    p_rev.add_argument("--backend", choices=["ollama", "stub"])
    p_rev.add_argument("--render", action="store_true")
    p_rev.set_defaults(func=_cmd_revise)

    p_his = sub.add_parser("history", help="查看某个 plan 的版本历史")
    p_his.add_argument("plan_id")
    p_his.set_defaults(func=_cmd_history)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
