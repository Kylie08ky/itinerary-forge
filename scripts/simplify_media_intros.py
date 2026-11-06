"""把已建好城市的地点简介（data/generated/media/<城市>.json 里的 intro.text）统一改成简体。

以前现建城市时只转了地点名和备注，简介是维基百科摘要原文，很多是繁体（「位於中華人民共和國」）。
现在建城市时会一起转（app/citybuild/fetch.py 的 simplify_raws）；这个脚本补转**已经建好**的城市。
用中文维基百科自己的繁简转换（和建城市时同一个函数），只在开发时联网跑。

用法：
    python scripts/simplify_media_intros.py --dry-run     # 只列出会改什么
    python scripts/simplify_media_intros.py               # 写回
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.citybuild.fetch import to_simplified  # noqa: E402
from app.citybuild.store import generated_dir  # noqa: E402

UA = "ai-travel-planner-agent-simplify/0.1 (https://github.com/Kylie08ky; personal project)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    paths = [p for p in sorted((generated_dir() / "media").glob("*.json")) if not p.name.endswith(".hero.json")]
    docs = {p: json.loads(p.read_text(encoding="utf-8")) for p in paths}
    texts = sorted({e["intro"]["text"] for d in docs.values() for e in d.get("places", {}).values() if e.get("intro")})
    print(f"{len(paths)} 个文件，{len(texts)} 段简介")
    with httpx.Client(headers={"User-Agent": UA}) as client:
        table = {s: t for s, t in to_simplified(client, texts).items() if t != s}
    print(f"其中 {len(table)} 段是繁体（或含繁体），要改")
    for src, dst in list(table.items())[:12]:
        print(f"  {src[:34]}  →  {dst[:34]}")
    if args.dry_run:
        return 0
    for p, d in docs.items():
        changed = 0
        for e in d.get("places", {}).values():
            if e.get("intro") and e["intro"]["text"] in table:
                e["intro"]["text"] = table[e["intro"]["text"]]
                changed += 1
        if changed:
            p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"写回 {p.name}：{changed} 段")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
