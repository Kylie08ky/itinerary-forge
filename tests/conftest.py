"""单元测试共用：不碰外网。

`create_app()` 没传天气 / 汇率服务时会建真的 WeatherService（连 Open-Meteo）
和 FxService（连 Frankfurter）。单元测试里统一换成「永远取不到」的桩，免得分享 / 导出等接口的测试
在联网时悄悄打外部接口、断网时变慢。要测天气 / 汇率本身的测试自己传 `weather=` / `fx=` 进 create_app。
"""

from __future__ import annotations

import os

import pytest


class OfflineWeather:
    def get(self, destination, dates, today):
        return None


class OfflineFx:
    def __init__(self, *args, **kwargs):
        pass

    def get(self, base="EUR", quote="CNY"):
        return None

    def rate(self, src, dst):
        return None


@pytest.fixture(autouse=True)
def _no_real_xhs_cookie(monkeypatch, tmp_path):
    """测试里绝不读到你真实的小红书 Cookie（它在你的 .env 里，也可能已被加载进进程环境变量）。"""
    import app.notes.sites as sites

    monkeypatch.delenv("XHS_COOKIE", raising=False)
    monkeypatch.setattr(sites, "ENV_PATH", tmp_path / "no-such.env")


@pytest.fixture(autouse=True)
def _isolated_generated_cities(monkeypatch, tmp_path_factory):
    """单元测试看不到你在应用里现建的城市（data/generated/）：那是你的数据，也会让「已配置城市」的断言随你建了什么而变。"""
    from app.cities import reload_cities

    monkeypatch.setenv("CITY_GENERATED_DIR", str(tmp_path_factory.mktemp("generated")))
    reload_cities()
    yield
    reload_cities()


@pytest.fixture(autouse=True)
def _no_real_external_requests(monkeypatch, tmp_path_factory):
    import app.api as api
    from app.maps import basemap

    monkeypatch.setenv("TILE_CACHE_DIR", str(tmp_path_factory.mktemp("tiles")))
    monkeypatch.setattr(basemap, "_download", lambda z, x, y: None)       # 测试里不连 OpenStreetMap

    monkeypatch.setattr(api, "WeatherService", OfflineWeather)
    monkeypatch.setattr(api, "FxService", OfflineFx)


# --------------------------------------------------------------------------- #
# 浏览器验收（-m e2e）：默认自己起一套独立的前后端 + 临时数据库（tests/e2e_stack.py）
# --------------------------------------------------------------------------- #
def _e2e_selected(config) -> bool:
    expr = config.getoption("markexpr") or ""
    return "e2e" in expr and "not e2e" not in expr


def _wants_running_services() -> bool:
    return bool(os.getenv("E2E_API") or os.getenv("E2E_WEB") or os.getenv("E2E_USE_RUNNING") == "1")


def pytest_configure(config):
    if not _e2e_selected(config) or _wants_running_services():
        return
    from e2e_stack import IsolatedStack

    stack = IsolatedStack()
    try:
        stack.start()
    except Exception as exc:                                  # noqa: BLE001
        pytest.exit(f"没能起独立的前后端：{exc}\n（想对着已经在跑的服务测，设 E2E_USE_RUNNING=1）", returncode=2)
    config._atp_e2e_stack = stack
    os.environ["E2E_API"], os.environ["E2E_WEB"], os.environ["E2E_ISOLATED"] = stack.api_url, stack.web_url, "1"


def pytest_unconfigure(config):
    stack = getattr(config, "_atp_e2e_stack", None)
    if stack is not None:
        stack.stop()
        for k in ("E2E_API", "E2E_WEB", "E2E_ISOLATED"):
            os.environ.pop(k, None)


def pytest_report_header(config):
    stack = getattr(config, "_atp_e2e_stack", None)
    if stack is not None:
        return (f"e2e：独立前后端 {stack.web_url} / {stack.api_url}，临时数据库在 {stack.tmp}"
                "（跑完删除，不碰你平时用的服务和数据）")
    if _e2e_selected(config):
        return "e2e：对着已经在跑的服务（E2E_API / E2E_WEB / E2E_USE_RUNNING）"
    return None
