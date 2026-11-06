"""scripts/set_xhs_cookie.py：把粘贴的 Cookie 整理好写进 .env，且不泄露值。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import set_xhs_cookie as S  # noqa: E402

FAKE = "a1=FAKE-A1-VALUE; webId=FAKE-WEB; web_session=FAKE-SECRET-123"


def test_pasted_text_is_tidied_into_one_line():
    assert S.parse_pasted(f"  Cookie: {FAKE}\r\n") == FAKE
    assert S.parse_pasted(f'"{FAKE}"') == FAKE
    assert S.parse_pasted("a1=1;\n  b=2") == "a1=1; b=2"


@pytest.mark.parametrize("raw", ["", "   ", "不是cookie", "x" * 9000, "a=1\x00b"])
def test_bad_input_is_refused_without_echoing_it(raw):
    with pytest.raises(ValueError) as err:
        S.parse_pasted(raw)
    assert raw.strip() == "" or raw[:20] not in str(err.value)


def test_names_never_include_values():
    assert S.names_of(FAKE) == ["a1", "webId", "web_session"]
    text = S.describe(FAKE)
    assert "FAKE" not in text and "web_session" in text and "共 3 项" in text
    assert "没有 web_session" in S.describe("a1=1; b=2")
    assert "还没有配置" in S.describe(None)


def test_writes_a_new_env_file(tmp_path):
    env = tmp_path / ".env"
    S.write_env(FAKE, env)
    assert env.read_text(encoding="utf-8") == f"XHS_COOKIE={FAKE}\n"


def test_replaces_the_existing_line_and_keeps_everything_else(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_BACKEND=ollama\r\nXHS_COOKIE=old\r\nOLLAMA_MODEL=qwen\r\n", encoding="utf-8", newline="")
    S.write_env(FAKE, env)
    assert env.read_bytes().decode() == f"LLM_BACKEND=ollama\r\nXHS_COOKIE={FAKE}\r\nOLLAMA_MODEL=qwen\r\n"


def test_fills_in_the_commented_example_line_and_appends_when_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n# XHS_COOKIE=\nB=2\n", encoding="utf-8")
    S.write_env("x=1", env)
    assert env.read_text(encoding="utf-8") == "A=1\nXHS_COOKIE=x=1\nB=2\n"
    env.write_text("A=1\n", encoding="utf-8")
    S.write_env("x=1", env)
    assert env.read_text(encoding="utf-8") == "A=1\nXHS_COOKIE=x=1\n"


def test_main_never_prints_the_value(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    monkeypatch.setattr(S, "ENV_PATH", env)
    monkeypatch.setattr(S.getpass, "getpass", lambda prompt="": FAKE)
    monkeypatch.setattr(sys, "argv", ["set_xhs_cookie.py"])
    assert S.main() == 0
    out = capsys.readouterr().out
    assert "FAKE" not in out and "web_session" in out
    assert env.read_text(encoding="utf-8") == f"XHS_COOKIE={FAKE}" + chr(10)


def test_main_refuses_bad_input_and_writes_nothing(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    monkeypatch.setattr(S, "ENV_PATH", env)
    monkeypatch.setattr(S.getpass, "getpass", lambda prompt="": "乱七八糟")
    monkeypatch.setattr(sys, "argv", ["set_xhs_cookie.py"])
    assert S.main() == 1 and not env.exists() and "没写入" in capsys.readouterr().out
