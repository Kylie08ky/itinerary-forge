"""读链接时按网站区分的几件事：哪些网站可以带登录 Cookie、Cookie 从哪来、小红书页面里的笔记怎么取。

Cookie 是用户自己的登录凭证（规格 M12 S7b）：只放 `.env`（`XHS_COOKIE=...`），只在读小红书域名时带上；
跳转到别的域名、别的网站一律不带；不进仓库、不进日志、不出现在任何接口的返回里。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = _PROJECT_ROOT / ".env"
XHS_ENV = "XHS_COOKIE"
MAX_COOKIE_CHARS = 8000


def is_xhs(host: str) -> bool:
    h = host.lower().rstrip(".")
    return h in ("xiaohongshu.com", "xhslink.com") or h.endswith((".xiaohongshu.com", ".xhslink.com"))


def _clean(raw: str | None) -> str | None:
    """Cookie 只能是一行可见字符：带换行 / 控制字符的（可能是想往请求头里塞东西）一律当作没配。"""
    if not raw:
        return None
    raw = raw.strip().strip('"').strip("'").strip()
    if not raw or len(raw) > MAX_COOKIE_CHARS or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return None
    return raw


def _read_env_file(name: str, path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value
    return None


def xhs_cookie(env_path: Path | None = None) -> str | None:
    """现读 .env（改了立刻生效，不用重启），没有再看进程环境变量。"""
    return _clean(_read_env_file(XHS_ENV, env_path or ENV_PATH)) or _clean(os.environ.get(XHS_ENV))


def cookie_for(host: str, env_path: Path | None = None) -> str | None:
    """这个主机该带的 Cookie；只有小红书域名有。每一跳都按那一跳的主机重新决定。"""
    # 短链服务 xhslink.com 只负责跳转，不需要登录，也不给它 Cookie；Cookie 只给 xiaohongshu.com
    h = host.lower().rstrip(".")
    if h == "xiaohongshu.com" or h.endswith(".xiaohongshu.com"):
        return xhs_cookie(env_path)
    return None


# 小红书会把「程序」标识的请求跳去登录页，哪怕带着有效的登录 Cookie（2026-10-22 实测）；
# 用户已经配了自己的登录 Cookie、在替自己读一篇笔记时，就和他的浏览器用同样的普通标识。
# 只对小红书域名，且只在配了 Cookie 时。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def headers_for(host: str, env_path: Path | None = None) -> dict[str, str]:
    if not is_xhs(host) or xhs_cookie(env_path) is None:
        return {}
    out = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml",
           "Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie_for(host, env_path) is not None:                      # xiaohongshu.com（不是短链服务）
        out["Referer"] = "https://www.xiaohongshu.com/"
    return out


# ---------------------------------------------------------------------------
# 小红书页面里的笔记：页面自带的 window.__INITIAL_STATE__（登录后的页面里有整篇正文，meta 描述里往往只有开头）
# ---------------------------------------------------------------------------
_STATE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*", re.DOTALL)
_TOPIC = re.compile(r"\[话题\]|\[[^\]\s]{1,8}R\]")      # 「#巴黎[话题]#」「[笑哭R]」这类标记


def _json_object_at(s: str, start: int) -> str | None:
    """从 start 处的 `{` 起，取出配平的一整个 JSON 对象（字符串里的括号不算）。"""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def extract_xhs_note(html: str) -> str | None:
    """登录后的小红书笔记页：标题 + 正文 + 话题。取不到（结构变了 / 没登录）返回 None，调用方退回到通用提取。"""
    m = _STATE.search(html)
    if not m or html[m.end():m.end() + 1] != "{":
        return None
    blob = _json_object_at(html, m.end())
    if not blob:
        return None
    try:
        state = json.loads(re.sub(r"\bundefined\b", "null", blob))
        detail = state["note"]["noteDetailMap"]
        note = next(iter(detail.values()))["note"]
    except (ValueError, KeyError, StopIteration, TypeError, AttributeError):
        return None
    title, desc = str(note.get("title") or "").strip(), str(note.get("desc") or "").strip()
    if not desc and not title:
        return None
    tags = [str(t.get("name")) for t in (note.get("tagList") or []) if isinstance(t, dict) and t.get("name")]
    body = _TOPIC.sub("", desc)
    return "\n".join(x for x in (title, body, " ".join(f"#{t}" for t in tags)) if x)
