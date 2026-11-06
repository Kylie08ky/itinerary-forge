"""把你的小红书登录 Cookie 写进 .env（规格 M12 S7b）。你只管粘贴，它负责写对位置。

用法：
    .venv\\Scripts\\python scripts\\set_xhs_cookie.py            # 提示你粘贴（输入时屏幕上不显示任何字符）
    .venv\\Scripts\\python scripts\\set_xhs_cookie.py --check    # 只看配好了没有（只显示有哪些项的名字，不显示值）

怎么拿到要粘贴的内容，见 README「导入小红书笔记链接」。本脚本绝不打印 Cookie 的值，也不把它写到别的地方。
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.notes.sites import ENV_PATH, XHS_ENV, _clean, xhs_cookie  # noqa: E402


def parse_pasted(raw: str) -> str:
    """整理粘贴的内容：去掉开头的「Cookie:」、首尾空白和引号、折成一行。不合格时抛 ValueError（说明原因，不含内容）。"""
    text = re.sub(r"\s*\r?\n\s*", " ", raw.strip())
    text = re.sub(r"^cookie\s*:\s*", "", text, flags=re.IGNORECASE)
    cookie = _clean(text)
    if cookie is None:
        raise ValueError("内容为空、太长（超过 8000 字符）或含有不能出现在请求头里的字符，请重新复制")
    if "=" not in cookie:
        raise ValueError("看起来不是 Cookie（应该是 名字=值; 名字=值 的样子），请重新复制请求头里 Cookie 那一行的值")
    return cookie


def names_of(cookie: str) -> list[str]:
    """Cookie 里各项的名字（不含值）。"""
    return [part.split("=", 1)[0].strip() for part in cookie.split(";") if "=" in part]


def write_env(cookie: str, path: Path | None = None) -> None:
    """把 XHS_COOKIE 写进 .env：已有这一行就替换，没有就追加，其他行原样保留（含换行风格）。"""
    path = path or ENV_PATH
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    nl = "\r\n" if "\r\n" in old else "\n"
    lines = old.replace("\r\n", "\n").split("\n") if old else []
    while lines and lines[-1] == "":
        lines.pop()
    new_line = f"{XHS_ENV}={cookie}"
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{XHS_ENV}=") or line.strip().startswith(f"# {XHS_ENV}="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    path.write_text(nl.join(lines) + nl, encoding="utf-8")


def describe(cookie: str | None) -> str:
    if not cookie:
        return "还没有配置 XHS_COOKIE。"
    names = names_of(cookie)
    login = ("含 web_session（登录凭证）" if "web_session" in names
             else "⚠ 没有 web_session，多半还没登录成功或复制得不完整")
    shown = ", ".join(names[:8]) + ("…" if len(names) > 8 else "")
    return f"已配置：共 {len(names)} 项（{shown}），{login}。"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只看配好了没有")
    args = ap.parse_args()
    if args.check:
        print(describe(xhs_cookie()))
        return 0
    print("请粘贴 Cookie（右键或 Ctrl+V；粘贴时屏幕上不会显示任何字符，这是正常的），然后按回车：")
    try:
        cookie = parse_pasted(getpass.getpass(""))
    except ValueError as exc:
        print(f"没写入：{exc}")
        return 1
    write_env(cookie)
    print(describe(cookie))
    print(f"已写入 {ENV_PATH}。改完立刻生效，不用重启；这个文件已被 git 忽略，请不要发给任何人。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
