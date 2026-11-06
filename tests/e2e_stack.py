"""浏览器验收（e2e）用的独立前后端：自己起一套后端 + 前端，数据库放在临时目录，跑完整个删掉。

为什么：以前 e2e 连的是你平时在用的后端和数据库，每跑一遍「我的行程」里就多出上百份测试行程（跑完才清理），
首页还会因为测试行程的日期含「今天」而弹出「今天」卡片。现在 `pytest -m e2e` 默认起独立的一套，不碰你的服务和数据。

- 后端：`python -m uvicorn app.api:app` 在空闲端口，`ATP_DATA_DIR` 指向临时目录（行程库、汇率缓存、用量记录都在里面）；
  `OLLAMA_BASE_URL` 指向一个连不上的地址，说明文字一律走模板（和模型没开时一样快、一样稳定，
  不受本机 Ollama 开没开影响）。
- 「今天」：后端用 `ATP_TODAY` 固定成 PINNED_TODAY，测试结果不随真实日期漂移。
- 前端：`vite` 开发服务器在空闲端口，`VITE_API_BASE` 指向上面那个后端。
- 想对着已经在跑的服务（比如 Docker 容器）跑：设 `E2E_API` / `E2E_WEB`，或设 `E2E_USE_RUNNING=1`（用 8137 / 5173）。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
# 后端的「今天」固定成这一天（ATP_TODAY）：测试里的订票截止日、天气是预报还是去年同期等都和今天有关，
# 不固定的话每过一天就有几个测试悄悄变样（这次的 4 个失败就是这样）。这一天是这些测试写成、验证通过的日子
PINNED_TODAY = "2026-10-21"
DEAD_LLM = "http://127.0.0.1:9"      # 9 号端口（discard）没人监听：连接立刻被拒绝，模型调用立刻失败，走模板说明


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, what: str, seconds: float = 60) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"{what} 没有在 {seconds:.0f} 秒内起来：{url}")


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":          # node 会带出 esbuild 子进程，要连根杀
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


class IsolatedStack:
    def __init__(self) -> None:
        self.tmp: Path | None = None
        self.api_port = self.web_port = 0
        self.procs: list[subprocess.Popen] = []
        self.log = None

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def web_url(self) -> str:
        return f"http://127.0.0.1:{self.web_port}"

    def start(self) -> None:
        node = shutil.which("node")
        if not node or not (WEB_DIR / "node_modules" / "vite" / "bin" / "vite.js").exists():
            raise RuntimeError("没有 node 或 web/node_modules（先 cd web && npm install），没法起前端")
        self.tmp = Path(tempfile.mkdtemp(prefix="atp_e2e_"))
        self.api_port, self.web_port = free_port(), free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("E2E_")}
        env.update(ATP_DATA_DIR=str(self.tmp / "plans"), OLLAMA_BASE_URL=DEAD_LLM, LLM_BACKEND="ollama",
                   CITY_GENERATED_DIR=str(self.tmp / "generated"),         # 看不到你在应用里现建的城市
                   ATP_TODAY=PINNED_TODAY, PYTHONIOENCODING="utf-8",
                   AMAP_WEB_KEY="", AMAP_JS_KEY="", AMAP_JS_SECRET="")   # 验收不联网、不耗高德额度（空串也挡住 .env）
        # 第二座「已经建好的城市」（里昂）：多城市旅行的验收要用，真的建一座要几分钟
        subprocess.run([sys.executable, "-m", "tests.city_fixtures", PINNED_TODAY], cwd=ROOT, env=env, check=True,
                       capture_output=True)
        self.log = log = open(self.tmp / "servers.log", "wb")            # noqa: SIM115 - 跟进程同生共死
        try:
            self.procs.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.api:app", "--host", "127.0.0.1", "--port", str(self.api_port)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))
            _wait(f"{self.api_url}/health", "后端")
            web_env = {**env, "VITE_API_BASE": self.api_url}
            self.procs.append(subprocess.Popen(
                [node, str(WEB_DIR / "node_modules" / "vite" / "bin" / "vite.js"), "--host", "127.0.0.1",
                 "--port", str(self.web_port), "--strictPort"],
                cwd=WEB_DIR, env=web_env, stdout=log, stderr=subprocess.STDOUT))
            _wait(self.web_url, "前端")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        for p in reversed(self.procs):
            _kill_tree(p)
        self.procs.clear()
        if self.log:
            self.log.close()
            self.log = None
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None
