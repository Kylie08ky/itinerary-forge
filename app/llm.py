"""可替换的 LLM 客户端。

- OllamaClient：调本地 Ollama，用 structured output（把 JSON Schema 传进去）强制合法 JSON。
- StubClient：返回固定样例，供测试和「无模型快速演示」使用。

由 .env / 环境变量的 LLM_BACKEND 决定用哪个。上层（planner）只依赖 LLMClient 协议，
以后要接 Claude / OpenAI，加一个类即可，不用改 planner。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_STUB_FIXTURE = _PROJECT_ROOT / "data" / "fixtures" / "stub-itinerary.json"


# 笔记导入用的模型（规格 M12）：读乐园 / 商场类笔记时比 7B 干净（实测），导入不常用，慢几秒可以接受
NOTE_MODEL_DEFAULT = "qwen2.5:14b-instruct"


class LLMError(RuntimeError):
    """LLM 调用或返回解析失败。"""


def _load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载：只认 KEY=VALUE，不覆盖已存在的环境变量。避免引第三方依赖。"""
    path = path or (_PROJECT_ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LLMConfig:
    backend: str = "ollama"          # ollama | stub
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    temperature: float = 0.3
    timeout_seconds: float = 240.0
    # True: 把完整 JSON Schema 作为语法约束传给 Ollama（小模型下反而容易「走捷径」输出空数组）。
    # False: 只用 "json" 模式，靠提示词 + pydantic 校验 + 重试兜底。默认 False。
    structured_output: bool = False
    # 费用统计用：每百万 token 的美元价格。本地模型是 0；以后接付费模型时在 .env 里填
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0

    @classmethod
    def from_env(cls) -> LLMConfig:
        _load_dotenv()
        return cls(
            backend=os.getenv("LLM_BACKEND", cls.backend).strip().lower(),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", cls.ollama_base_url).rstrip("/"),
            ollama_model=os.getenv("OLLAMA_MODEL", cls.ollama_model),
            temperature=float(os.getenv("LLM_TEMPERATURE", cls.temperature)),
            timeout_seconds=float(os.getenv("LLM_TIMEOUT", cls.timeout_seconds)),
            structured_output=_env_bool("LLM_STRUCTURED_OUTPUT", cls.structured_output),
            price_in_per_mtok=float(os.getenv("LLM_PRICE_IN_PER_MTOK", cls.price_in_per_mtok)),
            price_out_per_mtok=float(os.getenv("LLM_PRICE_OUT_PER_MTOK", cls.price_out_per_mtok)),
        )


@runtime_checkable
class LLMClient(Protocol):
    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        """返回一段 JSON 字符串（不保证语义正确，只保证是 JSON）。"""
        ...


class OllamaClient:
    backend = "ollama"

    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    @property
    def model_name(self) -> str:
        return self._config.ollama_model

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        return self.complete_json_with_usage(system=system, user=user, schema=schema)[0]

    def complete_json_with_usage(self, *, system: str, user: str, schema: dict) -> tuple[str, tuple[int, int] | None]:
        """同 complete_json，额外返回 (输入 token, 输出 token)；供调用追踪使用。"""
        payload = {
            "model": self._config.ollama_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # structured_output=True 时用完整 schema 约束；否则用宽松的 "json" 模式。
            "format": schema if self._config.structured_output else "json",
            "stream": False,
            "options": {"temperature": self._config.temperature},
        }
        try:
            resp = httpx.post(
                f"{self._config.ollama_base_url}/api/chat",
                json=payload,
                timeout=self._config.timeout_seconds,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama 请求失败：{exc}") from exc

        body = resp.json()
        content = body.get("message", {}).get("content", "")
        if not content.strip():
            raise LLMError("Ollama 返回内容为空")
        usage = None
        if "prompt_eval_count" in body or "eval_count" in body:
            usage = (int(body.get("prompt_eval_count", 0)), int(body.get("eval_count", 0)))
        return content, usage


class StubClient:
    """忽略 prompt，永远返回同一份样例行程。"""

    backend = "stub"
    model_name = "stub"

    def __init__(self, fixture_path: Path | None = None) -> None:
        path = fixture_path or _STUB_FIXTURE
        if not path.exists():
            raise LLMError(f"Stub 样例文件不存在：{path}")
        self._fixture = path.read_text(encoding="utf-8")
        json.loads(self._fixture)  # 提前校验样例本身是合法 JSON

    def complete_json(self, *, system: str, user: str, schema: dict) -> str:
        return self._fixture


def get_client(config: LLMConfig | None = None) -> LLMClient:
    config = config or LLMConfig.from_env()
    if config.backend == "stub":
        return StubClient()
    if config.backend == "ollama":
        return OllamaClient(config)
    raise LLMError(f"未知的 LLM_BACKEND：{config.backend!r}（支持 ollama / stub）")
