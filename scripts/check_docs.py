"""文档体检：提交前跑一遍，把过期、断链、孤立的文档找出来。

    .venv\\Scripts\\python scripts/check_docs.py

检查什么（都是机器能判断的事，不用人眼去对）：
1. Markdown 相对链接指向的文件必须存在；
2. 文档里用反引号写的仓库路径（app/…、scripts/…、tests/…、data/…、web/…、docs/…、specs/…）必须存在；
3. 规格里不能写带日期的「状态 / 现状」行（进度只写在 specs/05）；
4. specs/05 和 docs/progress-overview.md 的更新日期必须一致；
5. specs/ 和 docs/ 下的每份文档至少被另一份文档链接到（没人链接的就是孤立文档，要么接上要么删）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 参与体检的文档
DOC_GLOBS = [
    "README.md",
    "README.zh-CN.md",
    "AGENTS.md",
    "VALIDATION.md",
    "docs/**/*.md",
    "specs/**/*.md",
    "web/README.md",
    "data/knowledge/README.md",
    "tests/regression/README.md",
]

# 这些是运行时才生成 / 本机才有的路径，文档里提到不算断链
GENERATED_PREFIXES = (
    "data/generated/",
    "data/plans",
    "data/city-seeds/",
    "data/media/",
    "web/dist",
    "web/node_modules",
)
# 孤立检查时不要求被链接的文档（入口文档、归档、报告）
ORPHAN_EXEMPT_PREFIXES = ("docs/archive/", "docs/reports/")
ORPHAN_EXEMPT = {"README.md", "README.zh-CN.md", "AGENTS.md", "VALIDATION.md"}

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CODE_PATH_RE = re.compile(r"`([A-Za-z0-9_./\-]+)`")
TOP_DIRS = ("app/", "scripts/", "tests/", "data/", "web/", "docs/", "specs/")
STATUS_RE = re.compile(r"^>?\s*\*{0,2}(状态|现状)（20\d\d")


def doc_files() -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in DOC_GLOBS:
        for p in sorted(ROOT.glob(pattern)):
            if p.is_file():
                seen[p] = None
    return list(seen)


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def check_links(path: Path, text: str, problems: list[str], linked: set[str]) -> None:
    for m in LINK_RE.finditer(text):
        target = m.group(1)
        if re.match(r"^(https?:|mailto:|#)", target):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            problems.append(f"断链：{rel(path)} → {target}")
        else:
            try:
                linked.add(rel(resolved))
            except ValueError:
                pass


def check_code_paths(path: Path, text: str, problems: list[str]) -> None:
    for m in CODE_PATH_RE.finditer(text):
        token = m.group(1).rstrip(".,;:")
        if not token.startswith(TOP_DIRS):
            continue
        if token.startswith(GENERATED_PREFIXES):
            continue
        # 目录写法（以 / 结尾）或带扩展名的文件
        if not token.endswith("/") and "." not in Path(token).name:
            continue
        if not (ROOT / token).exists():
            problems.append(f"路径不存在：{rel(path)} 提到 `{token}`")


def check_status_lines(path: Path, text: str, problems: list[str]) -> None:
    r = rel(path)
    if not r.startswith("specs/") or r == "specs/05-progress-checklist.md":
        return
    for i, line in enumerate(text.splitlines(), 1):
        if STATUS_RE.match(line):
            problems.append(f"规格里有带日期的状态行（进度只写在 specs/05）：{r}:{i}")
        if "- [ ] ⏳" in line or "- [x] ~~" in line:
            problems.append(f"规格里出现进度勾选（只能写在 specs/05）：{r}:{i}")


def check_update_dates(problems: list[str]) -> None:
    checklist = (ROOT / "specs/05-progress-checklist.md").read_text(encoding="utf-8")
    overview = (ROOT / "docs/progress-overview.md").read_text(encoding="utf-8")
    a = re.search(r"最近更新：(\d{4}-\d{2}-\d{2})", checklist)
    b = re.search(r"最后更新\s*(\d{4}-\d{2}-\d{2})", overview)
    if not a:
        problems.append("specs/05 缺少「最近更新：YYYY-MM-DD」")
    if not b:
        problems.append("docs/progress-overview.md 缺少「最后更新 YYYY-MM-DD」")
    if a and b and a.group(1) != b.group(1):
        problems.append(f"更新日期不一致：specs/05 是 {a.group(1)}，进度总览是 {b.group(1)}")


def check_orphans(files: list[Path], linked: set[str], problems: list[str]) -> None:
    for p in files:
        r = rel(p)
        if r in ORPHAN_EXEMPT or r.startswith(ORPHAN_EXEMPT_PREFIXES):
            continue
        if not r.startswith(("docs/", "specs/")):
            continue
        if r not in linked:
            problems.append(f"孤立文档（没有任何文档链接到它）：{r}")


def run() -> list[str]:
    problems: list[str] = []
    linked: set[str] = set()
    files = doc_files()
    for p in files:
        text = p.read_text(encoding="utf-8")
        check_links(p, text, problems, linked)
        check_code_paths(p, text, problems)
        check_status_lines(p, text, problems)
    check_update_dates(problems)
    check_orphans(files, linked, problems)
    return problems


def main() -> int:
    problems = run()
    if not problems:
        print("文档体检通过。")
        return 0
    print(f"文档体检发现 {len(problems)} 个问题：")
    for item in problems:
        print(" -", item)
    return 1


if __name__ == "__main__":
    sys.exit(main())
