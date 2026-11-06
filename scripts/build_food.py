"""批量核对并写入「美食」名菜名点和招牌（规格 09 十七）：写好提纲 → 一条命令核对来源 → 通过才写进 data/food/<城市>.json。

用法：
    python scripts/build_food.py data/food/drafts/paris.json            # 核对并写入（同 id 的名菜被替换）
    python scripts/build_food.py data/food/drafts/paris.json --dry-run  # 只核对，不写文件
    python scripts/build_food.py data/food/drafts/paris.json --explain  # 同时列出店名和菜名在来源里出现的那一句，给人复核
    python scripts/build_food.py --check-only                           # 只核对已经写进去的

提纲 = `{"city": "paris", "dishes": [...]}`，格式见 app/food.py；提纲里的 `match`（名菜在来源里的写法）、
招牌里的 `match` / `dish_match`（店名 / 菜名在那个来源里的写法）只给脚本用，不写进数据文件。

脚本替你做的：
1. **来源能打开**：来源网址真的请求一次（维基百科取全文，其他网址取页面文字）。
2. **名菜真实存在**：名菜的写法至少有一个在它的来源页面里出现。
3. **名气够**：维基百科的语言版本数达到门槛（app/food.py 的 MIN_POPULARITY）；顺便记下维基数据编号。
4. **中文译名有据可查**：`zh_wiki` 在中文维基百科里有这个标题。
5. **招牌有出处**：**同一个来源页面**里既有这家店（英文 / 当地名 / 中文名的各种写法）又有这样东西。
6. **数字有出处**：简介和每条说明里的年份（3–4 位数字）必须在对应来源页面里出现。
7. 通过后再用 `load_food` 完整校验（场所在库里、没有长期闭馆）。

它**核对不了**的：来源里「同时提到」不等于「这家店今天还卖」——说明里的措辞要人看。`--explain` 把来源里提到的那一句列出来，逐条对过再放行。
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.culture import mentions  # noqa: E402
from app.food import MIN_POPULARITY, FoodError, FoodFile, load_food  # noqa: E402
from app.library import load_library  # noqa: E402
from app.names import fold  # noqa: E402
from scripts.build_culture import attach_fame, zh_title_exists  # noqa: E402
from scripts.build_route import UA, fetch_text, ground  # noqa: E402

DATA = ROOT / "data" / "food"
HELPER_KEYS = ("match", "dish_match")                       # 只给脚本用，写数据文件前去掉


def snippet(text: str, key: str, width: int = 70) -> str:
    folded = fold(text)
    i = folded.find(fold(key))
    if i == -1:
        return ""
    # fold 会去掉标点和重音，位置对不上原文时退回原文里不区分大小写的查找
    j = text.lower().find(key.lower())
    pos = j if j != -1 else 0
    return " ".join(text[max(0, pos - width):pos + width].split())


def has_any(text: str, keys: list[str]) -> str | None:
    folded = fold(text)
    return next((k for k in keys if fold(k) in folded), None)


def check_dish(raw: dict, client: httpx.Client, today: str, lib, *, explain: bool) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    out: list[str] = []
    did = raw.get("id", "?")
    src = raw.get("source", {})
    src.setdefault("checked_at", today)
    try:
        text = fetch_text(client, src["url"])
    except Exception as exc:  # noqa: BLE001
        return [f"{did}：来源打不开 {src.get('url')}：{exc}"], out
    found = has_any(text, raw.get("match") or [raw.get("name_local", "")])
    if not found:
        problems.append(f"{did}：来源页面里找不到这道菜（试过：{'、'.join(raw.get('match') or [raw.get('name_local', '')])}）")
    elif explain:
        out.append(f"  {did}: …{snippet(text, found)}…")
    attach_fame(raw, client)
    if raw.get("popularity", 0) < MIN_POPULARITY:
        problems.append(f"{did}：名气不够（维基百科只有 {raw.get('popularity', 0)} 个语言版本，门槛 {MIN_POPULARITY}），不收")
    if not zh_title_exists(client, raw.get("zh_wiki", "")):
        problems.append(f"{did}：中文译名依据「{raw.get('zh_wiki')}」在中文维基百科里找不到")
    ground(f"{did} 简介", [raw.get("summary", "")], [text], problems, [src.get("title", src["url"])])

    for s in raw.get("signatures", []):
        pid = s.get("place_id", "")
        s.setdefault("source", {}).setdefault("checked_at", today)
        p = lib.places.get(pid)
        if p is None:
            problems.append(f"{did}：招牌场所 {pid!r} 不在地点库里")
            continue
        try:
            stext = fetch_text(client, s["source"]["url"])
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{did} 招牌 {pid}：来源打不开 {s['source'].get('url')}：{exc}")
            continue
        place_hit = has_any(stext, s.get("match") or []) or (p.name_en if mentions(p, stext) else None)
        dish_hit = has_any(stext, s.get("dish_match") or raw.get("match") or [raw.get("name_local", "")])
        if not place_hit:
            problems.append(f"{did} 招牌 {pid}：这条招牌的来源页面里找不到这家店「{p.name_zh}」")
        if not dish_hit:
            problems.append(f"{did} 招牌 {pid}：这条招牌的来源页面里找不到这样东西「{raw.get('name_zh')}」")
        if explain and place_hit and dish_hit:
            out.append(f"  {did} @ {pid}: 店 …{snippet(stext, place_hit)}…  菜 …{snippet(stext, dish_hit)}…")
        ground(f"{did} 招牌 {pid}", [s.get("note", "")], [stext], problems, [s["source"].get("title", "")])
    return problems, out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("drafts", nargs="*", help="名菜提纲文件")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()
    today = date.today().isoformat()
    client = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True)

    jobs: list[tuple[str, list[dict]]] = []
    if args.check_only:
        for path in sorted(DATA.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            jobs.append((data["city"], data["dishes"]))
    else:
        if not args.drafts:
            ap.error("给一个提纲文件，或用 --check-only")
        for d in args.drafts:
            data = json.loads(Path(d).read_text(encoding="utf-8"))
            jobs.append((data["city"], data["dishes"]))

    failed = False
    for city, dishes in jobs:
        lib = load_library(city)
        all_problems: list[str] = []
        for raw in dishes:
            problems, notes = check_dish(raw, client, today, lib, explain=args.explain)
            all_problems += problems
            print(("✗ " if problems else "✓ ") + raw.get("id", "?"))
            for n in notes:
                print(n)
        if all_problems:
            failed = True
            print(f"\n== {city}：{len(all_problems)} 个问题")
            for p in all_problems:
                print("  -", p)
            continue
        if args.dry_run or args.check_only:
            print(f"== {city}：{len(dishes)} 样名菜全部通过")
            continue
        for raw in dishes:
            for k in HELPER_KEYS:
                raw.pop(k, None)
            for s in raw.get("signatures", []):
                for k in HELPER_KEYS:
                    s.pop(k, None)
        FoodFile.model_validate({"city": city, "dishes": dishes})
        DATA.mkdir(parents=True, exist_ok=True)
        path = DATA / f"{city}.json"
        existing = json.loads(path.read_text(encoding="utf-8"))["dishes"] if path.exists() else []
        by_id = {d["id"]: d for d in existing}
        by_id.update({d["id"]: d for d in dishes})
        path.write_text(json.dumps({"city": city, "dishes": list(by_id.values())}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        try:
            load_food.cache_clear()
            load_food(city)
        except FoodError as exc:
            print(f"✗ 写入后完整校验失败：{exc}")
            return 1
        print(f"== {city}：{len(dishes)} 样名菜通过并写入 {path.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
