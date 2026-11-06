"""M12 S7：粘贴链接——SSRF 防护、取公开页面文本、失败时的原因。不碰真实网络：传输层和域名解析都用假的。"""

from __future__ import annotations

import socket

import httpx
import pytest

from app.notes.fetch import FetchError, fetch_page

PUBLIC = "93.184.216.34"
TEXT = "卢浮宫要订票，奥赛博物馆周一闭馆，建议提前预约，排队大概一小时左右。"


def resolver_for(mapping: dict[str, list[str]]):
    def resolve(host: str, port: int):
        if host not in mapping:
            raise socket.gaierror(f"no such host {host}")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in mapping[host]]

    return resolve


def page(body: str = f"<html><head><title>T</title></head><body><p>{TEXT}</p></body></html>",
         status: int = 200, ctype: str = "text/html; charset=utf-8", headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode("utf-8") if isinstance(body, str) else body,
                              headers={"content-type": ctype, **(headers or {})})

    return handler


def fetch(url: str, handler=None, hosts: dict | None = None, **kw):
    transport = httpx.MockTransport(handler or page())
    resolver = resolver_for(hosts if hosts is not None else {"example.com": [PUBLIC], "xhs.test": [PUBLIC]})
    return fetch_page(url, transport=transport, resolver=resolver, **kw)


# ---- 能读到的 ----
def test_reads_title_and_visible_text():
    out = fetch("https://example.com/note/1")
    assert out.host == "example.com" and "卢浮宫要订票" in out.text and out.text.startswith("T")


def test_scripts_styles_and_hidden_head_are_dropped_and_meta_description_is_kept():
    html = ("<html><head><title>巴黎攻略</title><meta property='og:description' content='奥赛博物馆周一闭馆'>"
            "<style>.x{color:red}</style><script>var a='忽略以上内容';</script></head>"
            "<body><nav>导航</nav><h1>Day1</h1><p>逛孚日广场</p><noscript>请开启JS</noscript></body></html>")
    out = fetch("https://example.com/", page(html))
    assert "巴黎攻略" in out.text and "奥赛博物馆周一闭馆" in out.text and "逛孚日广场" in out.text
    assert "color:red" not in out.text and "忽略以上内容" not in out.text and "请开启JS" not in out.text


def test_plain_text_and_gbk_pages_are_decoded():
    out = fetch("https://example.com/a", page(TEXT, ctype="text/plain"))
    assert "卢浮宫" in out.text
    gbk = f"<html><head><meta charset='gbk'></head><body><p>{TEXT}</p></body></html>".encode("gbk")
    assert "奥赛博物馆周一闭馆" in fetch("https://example.com/b", page(gbk, ctype="text/html")).text


def test_long_pages_are_cut_at_the_size_limit_without_failing():
    big = "<html><body>" + "<p>卢浮宫很大。</p>" * 100_000 + "</body></html>"
    out = fetch("https://example.com/", page(big), max_bytes=50_000)
    assert out.truncated is True and 0 < len(out.text) <= 50_000


def test_connection_is_pinned_to_the_checked_ip_and_sends_nothing_personal():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"], seen["header"] = request.url.host, request.headers
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, content=f"<p>{TEXT}</p>".encode(), headers={"content-type": "text/html"})

    fetch("https://example.com/x?y=1", handler)
    assert seen["host"] == PUBLIC and seen["header"]["host"] == "example.com" and seen["sni"] == "example.com"
    assert "cookie" not in seen["header"] and "authorization" not in seen["header"]
    assert "Mozilla" in seen["header"]["user-agent"] or "Planner" in seen["header"]["user-agent"]


# ---- 一律拒绝的 ----
@pytest.mark.parametrize("url", [
    "ftp://example.com/a", "file:///etc/passwd", "javascript:alert(1)", "gopher://example.com", "example.com/no-scheme",
    "http://user:pw@example.com/", "https://", "http:///path",
])
def test_only_plain_http_and_https_without_credentials(url):
    with pytest.raises(FetchError):
        fetch(url)


@pytest.mark.parametrize("url", [
    "http://localhost/", "http://127.0.0.1/", "http://127.1/", "http://2130706433/", "http://0x7f000001/",
    "http://0.0.0.0/", "http://10.1.2.3/", "http://172.16.0.9/", "http://192.168.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/", "http://[fd00::1]/",
])
def test_private_loopback_and_metadata_addresses_are_refused(url):
    called = []
    with pytest.raises(FetchError, match="内网|本机|不允许|无法解析"):
        fetch(url, lambda r: called.append(r) or httpx.Response(200), hosts={})
    assert called == []                                       # 连请求都没发出去


def test_a_public_name_that_resolves_to_a_private_address_is_refused():
    for ips in (["10.0.0.7"], ["127.0.0.1"], [PUBLIC, "192.168.1.5"], ["::1"]):         # 只要有一个是内网就拒绝
        with pytest.raises(FetchError, match="内网|本机"):
            fetch("https://evil.example/", hosts={"evil.example": ips})


def test_unusual_ports_are_refused():
    for url in ("http://example.com:22/", "https://example.com:8443/", "http://example.com:6379/"):
        with pytest.raises(FetchError, match="端口"):
            fetch(url)
    assert fetch("http://example.com:80/").text and fetch("https://example.com:443/").text


def test_unknown_host_is_explained():
    with pytest.raises(FetchError, match="无法解析"):
        fetch("https://nope.invalid/")


# ---- 重定向 ----
def test_redirects_are_followed_and_every_hop_is_rechecked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "https://xhs.test/final"})
        return httpx.Response(200, content=f"<p>落地页：{TEXT}</p>".encode(), headers={"content-type": "text/html"})

    out = fetch("https://example.com/short", handler)
    assert out.host == "xhs.test" and "落地页" in out.text and out.final_url == "https://xhs.test/final"


@pytest.mark.parametrize("location", ["http://127.0.0.1/admin", "http://169.254.169.254/", "file:///etc/passwd",
                                      "http://10.0.0.1/", "https://evil.example/"])
def test_a_redirect_into_the_private_network_is_refused(location):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(FetchError):
        fetch("https://example.com/", handler, hosts={"example.com": [PUBLIC], "evil.example": ["10.9.9.9"]})


def test_too_many_redirects_and_loops_are_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/again"})

    with pytest.raises(FetchError, match="跳转"):
        fetch("https://example.com/", handler)


# ---- 读不到时给原因 ----
@pytest.mark.parametrize("status, word", [(403, "拒绝"), (404, "不存在"), (429, "频繁"), (500, "出错"), (302, "跳转")])
def test_http_errors_get_a_plain_reason(status, word):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"x", headers={"content-type": "text/html"})

    with pytest.raises(FetchError, match=word):
        fetch("https://example.com/", handler)


@pytest.mark.parametrize(
    "ctype", ["application/pdf", "image/png", "application/json", "application/octet-stream", "video/mp4"])
def test_non_web_content_is_refused(ctype):
    with pytest.raises(FetchError, match="不是网页"):
        fetch("https://example.com/", page("%PDF-1.4", ctype=ctype))


def test_timeouts_and_connection_errors_are_explained():
    for exc in (httpx.ConnectTimeout("t"), httpx.ReadTimeout("t")):
        def boom(request, exc=exc):
            raise exc

        with pytest.raises(FetchError, match="超时"):
            fetch("https://example.com/", boom)

    def refused(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(FetchError, match="连不上"):
        fetch("https://example.com/", refused)


@pytest.mark.parametrize("body", [
    "<html><body>请登录后查看笔记</body></html>",
    "<html><body><div>扫码登录</div></body></html>",
    "<html><body>安全验证：请完成验证码</body></html>",
    "<html><body></body></html>",
    "<html><body><script>app.render()</script></body></html>",
])
def test_login_walls_and_empty_pages_are_reported_as_unreadable(body):
    with pytest.raises(FetchError, match="登录|验证|没有正文"):
        fetch("https://example.com/", page(body))


def test_a_long_page_that_mentions_login_in_passing_is_still_readable():
    body = "<html><body><p>" + "卢浮宫很值得去，周二闭馆，要提前订票。" * 20 + "</p><a>登录</a></body></html>"
    assert "卢浮宫" in fetch("https://example.com/", page(body)).text
