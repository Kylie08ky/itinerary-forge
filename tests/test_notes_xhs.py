"""M12 S7b：用自己的登录 Cookie 读小红书笔记——Cookie 去向、失效提示、页面提取、保护账号的三道闸。

不碰真实网络，也不用真 Cookie：传输层、域名解析、时钟都是假的；Cookie 是一串一眼能认出来的假值。
"""

from __future__ import annotations

import json
import logging
import socket

import httpx
import pytest

from app.api import create_app
from app.notes.fetch import AccessDenied, FetchedPage, FetchError, LoginRequired, fetch_page
from app.notes.guard import SiteGuard
from app.notes.sites import cookie_for, extract_xhs_note, xhs_cookie

FAKE = "web_session=FAKE-SECRET-VALUE-123; a1=FAKE-A1"
PUBLIC = "93.184.216.34"
NOTE_URL = "https://www.xiaohongshu.com/discovery/item/6a81ab01000000002800616c?xsec_token=abc"


def resolve(host, port):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]


DESC = "1.皮诺美术馆 官网 #巴黎[话题]#\n2.奥赛博物馆，周一闭馆，提前订票"


def note_html(title="🇫🇷巴黎博物馆学生免费攻略", desc=DESC):
    state = {"note": {"noteDetailMap": {"6a81ab": {"note": {
        "title": title, "desc": desc, "undefinedField": "undefined",
        "tagList": [{"name": "巴黎"}, {"name": "学生优惠"}]}}}}}
    blob = json.dumps(state, ensure_ascii=False).replace('"undefined"', "undefined")
    script = f"<script>window.__INITIAL_STATE__={blob}</script>"
    return f"<html><head><title>{title} - 小红书</title></head><body>{script}</body></html>"


def read(url, handler, cookie=FAKE, **kw):
    return fetch_page(url, transport=httpx.MockTransport(handler), resolver=resolve,
                      cookie_for=lambda host: cookie if host.endswith("xiaohongshu.com") else None, **kw)


# ---- Cookie 只给小红书，每一跳重新决定 ----
def test_cookie_goes_only_to_xiaohongshu_and_never_to_other_hosts_or_after_a_redirect_away():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        seen.append((host, request.headers.get("cookie")))
        if host == "xhslink.com":
            return httpx.Response(302, headers={"location": NOTE_URL})
        if host == "www.xiaohongshu.com":
            return httpx.Response(302, headers={"location": "https://other.example/landing"})
        return httpx.Response(200, content=("<p>" + "卢浮宫要订票，奥赛周一闭馆。" * 5 + "</p>").encode(),
                              headers={"content-type": "text/html"})

    read("https://xhslink.com/o/abc", handler)
    assert seen == [("xhslink.com", None), ("www.xiaohongshu.com", FAKE), ("other.example", None)]


def test_cookie_for_only_returns_a_cookie_for_xiaohongshu_domains(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"LLM_BACKEND=ollama\nXHS_COOKIE={FAKE}\n", encoding="utf-8")
    assert cookie_for("www.xiaohongshu.com", env) == FAKE and cookie_for("xiaohongshu.com", env) == FAKE
    for host in ("xhslink.com", "example.com", "xiaohongshu.com.evil.example", "notxiaohongshu.com", "localhost"):
        assert cookie_for(host, env) is None


def test_env_file_is_read_fresh_and_bad_values_are_treated_as_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("XHS_COOKIE", raising=False)
    env = tmp_path / ".env"
    assert xhs_cookie(env) is None                                           # 没有文件
    env.write_text("# XHS_COOKIE=commented\nXHS_COOKIE=\n", encoding="utf-8")
    assert xhs_cookie(env) is None                                           # 注释掉的、空的
    env.write_text('XHS_COOKIE="a=1; b=2"\n', encoding="utf-8")
    assert xhs_cookie(env) == "a=1; b=2"                                     # 引号去掉
    env.write_text("XHS_COOKIE=a=1\r\nb: injected\n", encoding="utf-8")
    assert xhs_cookie(env) == "a=1"
    env.write_text("XHS_COOKIE=" + "x" * 9000 + "\n", encoding="utf-8")
    assert xhs_cookie(env) is None                                           # 太长
    monkeypatch.setenv("XHS_COOKIE", "fromenv=1")
    env.write_text("OTHER=1\n", encoding="utf-8")
    assert xhs_cookie(env) == "fromenv=1"                                    # 文件里没有就看进程环境变量
    monkeypatch.setenv("XHS_COOKIE", "bad\nvalue")
    assert xhs_cookie(env) is None                                           # 带换行的：不信


def test_set_cookie_from_the_site_is_not_kept_or_replayed():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/b",
                                                "set-cookie": "x=1; Path=/"})
        return httpx.Response(200, content=("<p>" + "笔记正文很长很长。" * 10 + "</p>").encode(),
                              headers={"content-type": "text/html"})

    read("https://www.xiaohongshu.com/a", handler, cookie=None)
    assert seen == [None, None]


def serve_note(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=note_html().encode(), headers={"content-type": "text/html"})


# ---- 失效 / 没配 ----
def login_redirect(request: httpx.Request) -> httpx.Response:
    return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/login?redirectPath=%2Fx"})


def test_login_redirect_without_a_cookie_tells_the_user_how_to_configure_one():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return login_redirect(request)

    with pytest.raises(LoginRequired, match="XHS_COOKIE"):
        read(NOTE_URL, handler, cookie=None)
    assert calls == ["/discovery/item/6a81ab01000000002800616c"]               # 登录页本身都不去请求


def test_login_redirect_with_a_cookie_says_it_probably_expired():
    with pytest.raises(LoginRequired, match="失效"):
        read(NOTE_URL, login_redirect)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_denials_are_access_denied_errors(status):
    with pytest.raises(AccessDenied):
        read(NOTE_URL, lambda r: httpx.Response(status, content=b"x", headers={"content-type": "text/html"}))


def test_the_cookie_never_shows_up_in_errors_logs_or_results(caplog):
    caplog.set_level(logging.DEBUG)
    for handler in (login_redirect, lambda r: httpx.Response(403, content=b"x"),
                    lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused"))):
        with pytest.raises(FetchError) as err:
            read(NOTE_URL, handler)
        assert "FAKE-SECRET" not in str(err.value) and "FAKE-SECRET" not in repr(err.value)
    ok = read(NOTE_URL, serve_note)
    assert "FAKE-SECRET" not in ok.text and "FAKE-SECRET" not in repr(ok)
    assert "FAKE-SECRET" not in caplog.text


# ---- 页面提取 ----
def test_logged_in_note_page_yields_title_body_and_tags():
    page = read(NOTE_URL, serve_note)
    assert page.host == "www.xiaohongshu.com"
    assert page.text.startswith("🇫🇷巴黎博物馆学生免费攻略")
    assert "奥赛博物馆，周一闭馆，提前订票" in page.text and "#巴黎 #学生优惠" in page.text
    assert "[话题]" not in page.text and "undefined" not in page.text


@pytest.mark.parametrize("html", [
    "<html><body>nothing</body></html>", "<script>window.__INITIAL_STATE__={broken</script>",
    '<script>window.__INITIAL_STATE__={"note":{}}</script>',
    '<script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{}}}</script>',
    '<script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{"a":{"note":{"title":"","desc":""}}}}}</script>',
])
def test_extractor_returns_none_when_the_page_has_no_note(html):
    assert extract_xhs_note(html) is None


def test_extractor_handles_braces_and_quotes_inside_strings():
    html = note_html(desc='用「{」和"引号"都不影响 } 解析。奥赛周一闭馆')
    assert "奥赛周一闭馆" in extract_xhs_note(html)


def test_when_the_state_is_missing_the_generic_extraction_still_works_via_meta():
    meta = "奥赛博物馆周一闭馆，卢浮宫要订票。" * 3
    html = f"<html><head><meta property='og:description' content='{meta}'></head><body></body></html>"
    page = read(NOTE_URL, lambda r: httpx.Response(200, content=html.encode(), headers={"content-type": "text/html"}))
    assert "奥赛博物馆周一闭馆" in page.text


# ---- 保护账号的三道闸 ----
class Clock:
    def __init__(self):
        self.t, self.slept = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def guard(clock, **kw):
    return SiteGuard(clock=clock.now, sleep=clock.sleep, **kw)


def page(url="u"):
    return FetchedPage(text="正文" * 20, host="www.xiaohongshu.com", final_url=url)


def test_same_link_is_cached_and_not_requested_again():
    clock, calls = Clock(), []
    g = guard(clock)
    fetcher = lambda u: calls.append(u) or page(u)  # noqa: E731
    a = g.fetch("https://xhslink.com/o/abc#frag", fetcher)
    b = g.fetch("https://xhslink.com/o/abc", fetcher)
    assert a is b and calls == ["https://xhslink.com/o/abc#frag"]
    clock.t += 6 * 3600 + 1
    g.fetch("https://xhslink.com/o/abc", fetcher)
    assert len(calls) == 2                                                    # 过期了才重读


def test_requests_are_spaced_out():
    clock = Clock()
    g = guard(clock)
    g.fetch("https://xhslink.com/o/a", lambda u: page(u))
    g.fetch("https://xhslink.com/o/b", lambda u: page(u))
    assert clock.slept and 2.9 < clock.slept[0] <= 3.0


def test_three_denials_in_a_row_pause_further_requests_for_a_while():
    clock, calls = Clock(), []
    g = guard(clock)

    def denied(url):
        calls.append(url)
        raise LoginRequired("要登录")

    for i in range(3):
        with pytest.raises(LoginRequired):
            g.fetch(f"https://xhslink.com/o/{i}", denied)
    with pytest.raises(FetchError, match="暂停 10 分钟"):
        g.fetch("https://xhslink.com/o/next", denied)
    assert len(calls) == 3                                                    # 第 4 次根本没发请求
    clock.t += 601
    with pytest.raises(LoginRequired):
        g.fetch("https://xhslink.com/o/after", denied)
    assert len(calls) == 4


def test_a_success_resets_the_failure_count_and_ordinary_errors_do_not_count():
    clock = Clock()
    g = guard(clock)

    def denied(url):
        raise LoginRequired("要登录")

    def gone(url):
        raise FetchError("页面不存在或已被删除")

    for i in range(2):
        with pytest.raises(LoginRequired):
            g.fetch(f"https://xhslink.com/o/d{i}", denied)
    g.fetch("https://xhslink.com/o/ok", lambda u: page(u))
    for i in range(5):
        with pytest.raises(FetchError, match="不存在"):
            g.fetch(f"https://xhslink.com/o/g{i}", gone)                      # 删了的笔记不算「被拒」
    for i in range(2):
        with pytest.raises(LoginRequired):
            g.fetch(f"https://xhslink.com/o/e{i}", denied)                    # 清零后又只失败 2 次：还没到停手
    g.fetch("https://xhslink.com/o/fine", lambda u: page(u))


def test_other_sites_are_not_cached_throttled_or_paused():
    clock, calls = Clock(), []
    g = guard(clock)
    for _ in range(3):
        g.fetch("https://blog.example/post", lambda u: calls.append(u) or page(u))
    assert len(calls) == 3 and clock.slept == []


# ---- 接口：只说「有没有」 ----
def test_sources_endpoint_reports_only_whether_a_cookie_is_configured(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.versions import PlanStore

    class NoModel:
        def complete_json(self, **kw):
            raise RuntimeError

    env = tmp_path / ".env"
    env.write_text(f"XHS_COOKIE={FAKE}\n", encoding="utf-8")
    import app.notes.sites as sites

    monkeypatch.setattr(sites, "ENV_PATH", env)
    c = TestClient(create_app(store=PlanStore(root=tmp_path / "plans"), client_factory=NoModel))
    r = c.get("/materials/sources")
    assert r.json() == {"xhs_cookie": True} and "FAKE" not in r.text
    env.write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.delenv("XHS_COOKIE", raising=False)
    assert c.get("/materials/sources").json() == {"xhs_cookie": False}


# ---- 请求头：配了 Cookie 的小红书域名用普通浏览器标识，其他一律不变 ----
def test_xhs_hosts_use_a_browser_identity_only_when_a_cookie_is_configured(tmp_path):
    from app.notes.sites import BROWSER_UA, headers_for

    env = tmp_path / ".env"
    assert headers_for("www.xiaohongshu.com", env) == {}                       # 没配 Cookie：照旧用说明用途的标识
    env.write_text(f"XHS_COOKIE={FAKE}\n", encoding="utf-8")
    h = headers_for("www.xiaohongshu.com", env)
    assert h["User-Agent"] == BROWSER_UA and h["Referer"] == "https://www.xiaohongshu.com/"
    short = headers_for("xhslink.com", env)
    assert short["User-Agent"] == BROWSER_UA and "Referer" not in short
    assert headers_for("example.com", env) == {} and headers_for("xiaohongshu.com.evil.example", env) == {}


def test_fetch_applies_those_headers_hop_by_hop():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["host"], "Mozilla/5.0 X" == request.headers["user-agent"],
                     request.headers.get("referer")))
        if request.headers["host"] == "xhslink.com":
            return httpx.Response(302, headers={"location": NOTE_URL})
        return serve_note(request)

    fetch_page("https://xhslink.com/o/abc", transport=httpx.MockTransport(seen_handler := handler), resolver=resolve,
               cookie_for=lambda h: FAKE if h.endswith("xiaohongshu.com") else None,
               headers_for=lambda h: {"User-Agent": "Mozilla/5.0 X", "Referer": "https://www.xiaohongshu.com/"}
               if h == "www.xiaohongshu.com" else {})
    assert seen_handler and seen == [("xhslink.com", False, None),
                                     ("www.xiaohongshu.com", True, "https://www.xiaohongshu.com/")]
