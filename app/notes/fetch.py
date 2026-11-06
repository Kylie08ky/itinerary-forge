"""M12 S7：读一个公开网页的文字。只用 httpx + 标准库。

笔记链接是用户粘贴的、不可信的输入，所以这里按 SSRF 的标准做法防护：
  - 只收 http / https，不带账号密码，只允许 80 / 443 端口；
  - 主机名解析出的**所有**地址都必须是公网地址（内网 / 回环 / 链路本地 / 云元数据 / 保留地址一律拒绝，
    含 IPv4 映射的 IPv6、十进制 / 十六进制写法的 IP）；
  - 连接固定到校验过的 IP（防 DNS 重绑定），HTTPS 的证书仍按原域名校验（SNI）；
  - 重定向最多 3 跳，**每一跳重新校验**；不读环境里的代理（代理会自己解析域名，绕过上面的检查）；
  - 超时 8 秒、总共最多读 1MB、只收 text/html / text/plain；不带 Cookie、不执行脚本。
失败时抛 FetchError，message 就是给用户看的原因。
"""

from __future__ import annotations

import http.cookiejar
import ipaddress
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from app.notes.sites import cookie_for as default_cookie_for
from app.notes.sites import extract_xhs_note, is_xhs
from app.notes.sites import headers_for as default_headers_for

MAX_BYTES = 1_000_000
TIMEOUT = 8.0
DEADLINE = 15.0                    # 一次读取（含重定向）最多花多久
MAX_REDIRECTS = 3
ALLOWED_PORTS = {80, 443}
USER_AGENT = "Mozilla/5.0 (compatible; TravelPlannerNoteReader/0.1; personal use; reads one page the user pasted)"  # noqa: E501
_TEXT_TYPES = {"text/html", "text/plain", "application/xhtml+xml"}
_REDIRECTS = {301, 302, 303, 307, 308}
_LOGIN_WORDS = ("请登录", "登录后", "登录查看", "扫码登录", "手机号登录", "验证码", "安全验证", "人机验证", "访问频繁",
                "Access Denied")
Resolver = Callable[[str, int], list]


class FetchError(RuntimeError):
    """没法读这个链接。message 是给用户看的原因。"""


class AccessDenied(FetchError):
    """网站拒绝了这次访问（401 / 403 / 429）：被限流或需要登录。连续出现时要停手，别把用户的账号搞出事。"""


class LoginRequired(AccessDenied):
    """网站要求登录（跳到了登录页）。"""


@dataclass
class FetchedPage:
    text: str
    host: str
    final_url: str
    title: str = ""
    truncated: bool = False


def _public_ips(host: str, port: int, resolver: Resolver) -> list[str]:
    """host 对应的所有地址；有一个不是公网地址就整体拒绝。"""
    addrs: list[str] = []
    try:
        addrs = [str(ipaddress.ip_address(host.strip("[]")))]     # 本来就是 IP
    except ValueError:
        try:                                              # 127.1 / 2130706433 / 0x7f000001 这类写法
            addrs = [socket.inet_ntoa(socket.inet_aton(host))]
        except OSError:
            try:
                addrs = sorted({info[4][0] for info in resolver(host, port)})
            except OSError as exc:
                raise FetchError(f"无法解析这个网址的域名（{host}）") from exc
    if not addrs:
        raise FetchError(f"无法解析这个网址的域名（{host}）")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise FetchError("这个网址指向本机或内网，出于安全不读取")
    return addrs


def _check(url: str, resolver: Resolver) -> tuple[str, str, int, str]:
    """校验一个网址，返回 (scheme, 域名, 端口, 要连的 IP)。"""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise FetchError("只支持 http / https 开头的网址")
    if parts.username or parts.password:
        raise FetchError("网址里不能带账号密码")
    host = parts.hostname
    if not host:
        raise FetchError("网址不完整，缺少域名")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise FetchError("网址里的端口不对") from exc
    if port not in ALLOWED_PORTS:
        raise FetchError(f"不允许访问 {port} 端口，只支持网页的默认端口")
    return parts.scheme, host, port, _public_ips(host, port, resolver)[0]


class _Text(HTMLParser):
    """可见文字 + 标题 + meta 描述。script / style / noscript / template / svg 里的东西不要。"""

    _SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article",
              "blockquote", "header", "footer", "nav"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: list[str] = []
        self.parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:description", "og:title", "twitter:description") and a.get("content"):
                self.meta.append(a["content"].strip())
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def _decode(raw: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    charset = m.group(1) if m else None
    if charset is None:
        meta = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.IGNORECASE)
        charset = meta.group(1).decode("ascii", "ignore") if meta else None
    for enc in ([charset] if charset else []) + ["utf-8", "gb18030"]:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _to_text(html: str) -> tuple[str, str, str]:
    p = _Text()
    p.feed(html)
    p.close()
    body = re.sub(r"[ \t　]+", " ", "".join(p.parts))
    body = re.sub(r"\s*\n\s*", "\n", body).strip()
    meta = "\n".join(dict.fromkeys(m for m in p.meta if m))
    return p.title.strip(), meta, body


def fetch_page(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    max_bytes: int = MAX_BYTES,
    timeout: float = TIMEOUT,
    cookie_for: Callable[[str], str | None] | None = None,
    headers_for: Callable[[str], dict[str, str]] | None = None,
) -> FetchedPage:
    """`cookie_for(主机)` 返回这个主机该带的登录 Cookie（默认只有小红书域名有，来自 .env）。

    每一跳按那一跳的主机重新决定，所以跳转到别的域名时不会带上。
    """
    cookie_for = cookie_for or default_cookie_for
    headers_for = headers_for or default_headers_for
    sent_cookie = False
    resolver = resolver or (lambda h, p: socket.getaddrinfo(h, p, type=socket.SOCK_STREAM))
    started = time.monotonic()
    current = url
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False) as client:
        client.cookies.jar.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))   # 不收、不存 Set-Cookie
        for _hop in range(MAX_REDIRECTS + 1):
            scheme, host, port, ip = _check(current, resolver)
            parts = urlsplit(current)
            target = httpx.URL(scheme=scheme, host=ip, port=port, raw_path=(parts.path or "/").encode()
                               + (b"?" + parts.query.encode() if parts.query else b""))
            default = 443 if scheme == "https" else 80
            headers = {"Host": host if port == default else f"{host}:{port}", "User-Agent": USER_AGENT,
                       "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"}
            headers.update(headers_for(host))
            if cookie := cookie_for(host):
                headers["Cookie"] = cookie
                sent_cookie = True
            ext = {"sni_hostname": host} if scheme == "https" else {}
            try:
                with client.stream("GET", target, headers=headers, extensions=ext) as resp:
                    if resp.status_code in _REDIRECTS:
                        loc = resp.headers.get("location")
                        if not loc:
                            raise FetchError("网站的跳转有问题，读不到页面")
                        current = urljoin(current, loc)
                        if urlsplit(current).path.rstrip("/").lower() == "/login":
                            raise _login_error(urlsplit(current).hostname or host, sent_cookie)    # 不必再请求登录页
                        continue
                    _raise_for_status(resp.status_code)
                    ctype = resp.headers.get("content-type", "text/html").split(";")[0].strip().lower()
                    if ctype not in _TEXT_TYPES:
                        raise FetchError(f"这个链接不是网页（{ctype or '未知类型'}），读不了")
                    raw, truncated = b"", False
                    for chunk in resp.iter_bytes():
                        raw += chunk
                        if len(raw) >= max_bytes:
                            raw, truncated = raw[:max_bytes], True
                            break
                        if time.monotonic() - started > DEADLINE:
                            raise FetchError("读取超时，网站太慢了")
                    content_type = resp.headers.get("content-type", "")
            except FetchError:
                raise
            except httpx.TimeoutException as exc:
                raise FetchError("读取超时，网站没有及时回应") from exc
            except httpx.HTTPError as exc:
                raise FetchError("连不上这个网站") from exc
            return _page(raw, content_type, ctype, host, current, truncated)
    raise FetchError("网站跳转太多次，读不到页面")


def _login_error(host: str, had_cookie: bool) -> LoginRequired:
    if is_xhs(host):
        if had_cookie:
            return LoginRequired(
                "小红书要求登录：.env 里的 XHS_COOKIE 可能已经失效（Cookie 一般一到四周过期），请重新复制后更新")
        return LoginRequired("小红书的笔记要登录才能看；可以把你自己的登录 Cookie 填进 .env 的 XHS_COOKIE（见 README）")
    return LoginRequired("这个页面需要登录才能看正文，读不到")


def _raise_for_status(code: int) -> None:
    if code < 400:
        return
    reasons = {
        401: "网站拒绝访问，需要登录", 403: "网站拒绝访问（可能需要登录，或有反爬限制）",
        404: "页面不存在或已被删除", 410: "页面已被删除", 429: "访问太频繁，被网站限制了",
    }
    if code in (401, 403, 429):
        raise AccessDenied(reasons[code])
    if code in reasons:
        raise FetchError(reasons[code])
    raise FetchError(f"网站那边出错了（HTTP {code}）" if code >= 500 else f"网站拒绝了请求（HTTP {code}）")


def _page(raw: bytes, content_type: str, ctype: str, host: str, final_url: str, truncated: bool) -> FetchedPage:
    decoded = _decode(raw, content_type)
    if is_xhs(host) and (note := extract_xhs_note(decoded)):
        title = note.partition(chr(10))[0]
        return FetchedPage(text=note, host=host, final_url=final_url, title=title, truncated=truncated)
    if ctype == "text/plain":
        title, meta, body = "", "", decoded.strip()
    else:
        title, meta, body = _to_text(decoded)
    text = "\n".join(x for x in (title, meta, body) if x)
    visible = len(body) + len(meta)
    if visible < 200 and any(w in text for w in _LOGIN_WORDS):
        raise FetchError("这个页面需要登录或验证才能看正文，读不到")
    if visible < 20:
        raise FetchError("页面里没有正文（可能需要登录，或内容靠脚本加载）")
    return FetchedPage(text=text, host=host, final_url=final_url, title=title, truncated=truncated)
