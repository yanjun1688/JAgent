"""v8 MVP · 模型适配层测试。

覆盖四类替身（scripted / replay / counting / 真实适配器）与 JSON 解析：
- `ScriptedModel` 顺序、记录、耗尽；
- `ReplayModel` 的零模型；
- `CountingModel` 的计数与 fail_after；
- `parse_json_object` 的围栏、散文、字符串花括号、非法与顶层数组；
- `OpenAICompatibleModel` 用 `httpx.MockTransport`（不联网、不需要 key）测 200 / 500；
- `from_env` 的 `JAGENT_LLM_*` 与仓库 `.env` 的 `LLM_*` 两套命名。
"""

from __future__ import annotations

import json

import httpx
import pytest

from jagent.agent.model import (
    MAX_BODY_CHARS,
    CountingModel,
    ModelCallFailed,
    OpenAICompatibleModel,
    ReplayModel,
    ScriptedModel,
    call_digest,
    from_env,
    parse_json_object,
)
from jagent.ports import ModelReply, ModelUnavailable

# --------------------------------------------------------------------------- #
# ScriptedModel
# --------------------------------------------------------------------------- #

def test_scripted_model_returns_replies_in_order() -> None:
    scripted = ModelReply(text="第二", tokens_in=7, tokens_out=3)
    model = ScriptedModel(["第一", scripted, {"ok": True}])

    first = model.complete(system="sys", prompt="p1")
    second = model.complete(system="sys", prompt="p2")
    third = model.complete(system="sys", prompt="p3")

    assert first.text == "第一"
    assert second is scripted
    assert (second.tokens_in, second.tokens_out) == (7, 3)
    assert json.loads(third.text) == {"ok": True}
    assert third.raw == {"ok": True}


def test_scripted_model_records_system_and_prompt() -> None:
    model = ScriptedModel(["a", "b"])

    model.complete(system="s1", prompt="p1", temperature=0.2)
    model.complete(system="s2", prompt="p2")

    assert model.calls == [("s1", "p1"), ("s2", "p2")]
    assert model.digests == [call_digest("s1", "p1"), call_digest("s2", "p2")]


def test_scripted_model_exhausted_names_the_call() -> None:
    model = ScriptedModel(["only"])

    model.complete(system="s", prompt="a")
    with pytest.raises(ModelCallFailed, match=r"call #2"):
        model.complete(system="s", prompt="b")

    assert len(model.calls) == 2


def test_scripted_model_empty_script_fails_on_first_call() -> None:
    with pytest.raises(ModelCallFailed, match=r"call #1"):
        ScriptedModel([]).complete(system="s", prompt="p")


def test_call_digest_is_stable_and_order_sensitive() -> None:
    assert call_digest("s", "p") == call_digest("s", "p")
    assert call_digest("s", "p") != call_digest("p", "s")


# --------------------------------------------------------------------------- #
# ReplayModel / CountingModel
# --------------------------------------------------------------------------- #

def test_replay_model_is_zero_model() -> None:
    with pytest.raises(ModelUnavailable):
        ReplayModel().complete(system="s", prompt="p")


def test_counting_model_counts_and_passes_through() -> None:
    inner = ScriptedModel(["a", "b"])
    model = CountingModel(inner)

    assert model.complete(system="s", prompt="p1").text == "a"
    assert model.complete(system="s", prompt="p2").text == "b"
    assert model.calls == 2
    assert len(inner.calls) == 2


def test_counting_model_fail_after_allows_exactly_n_calls() -> None:
    inner = ScriptedModel(["a", "b"])
    model = CountingModel(inner, fail_after=1)

    assert model.complete(system="s", prompt="p1").text == "a"
    with pytest.raises(ModelCallFailed, match="call 2"):
        model.complete(system="s", prompt="p2")
    with pytest.raises(ModelCallFailed, match="call 3"):
        model.complete(system="s", prompt="p3")

    assert model.calls == 3
    assert len(inner.calls) == 1


def test_counting_model_fail_after_zero_fails_immediately() -> None:
    model = CountingModel(ScriptedModel(["a"]), fail_after=0)

    with pytest.raises(ModelCallFailed, match="call 1"):
        model.complete(system="s", prompt="p")

    assert model.calls == 1


# --------------------------------------------------------------------------- #
# parse_json_object
# --------------------------------------------------------------------------- #

def test_parse_json_object_plain() -> None:
    assert parse_json_object('{"a": 1, "b": {"c": 2}}') == {"a": 1, "b": {"c": 2}}


def test_parse_json_object_strips_json_fences() -> None:
    text = '```json\n{"plan": ["x", "y"], "ok": true}\n```'

    assert parse_json_object(text) == {"plan": ["x", "y"], "ok": True}


def test_parse_json_object_strips_bare_fences() -> None:
    assert parse_json_object('```\n{"ok": true}\n```') == {"ok": True}


def test_parse_json_object_ignores_surrounding_prose() -> None:
    text = '好的，结果如下：\n{"plan": "check", "steps": []}\n希望有帮助。'

    assert parse_json_object(text) == {"plan": "check", "steps": []}


def test_parse_json_object_handles_braces_and_escapes_inside_strings() -> None:
    text = '先说明一句：{"note": "这里有 } 和 { 还有 \\" 引号", "n": 1} 结束'

    parsed = parse_json_object(text)

    assert parsed["note"] == '这里有 } 和 { 还有 " 引号'
    assert parsed["n"] == 1


def test_parse_json_object_rejects_invalid_json() -> None:
    with pytest.raises(ModelCallFailed, match="not valid JSON"):
        parse_json_object("抱歉，我不能给 JSON：{not valid}")


def test_parse_json_object_rejects_text_without_object() -> None:
    with pytest.raises(ModelCallFailed, match="no balanced JSON object"):
        parse_json_object("没有花括号，只有散文。")


@pytest.mark.parametrize("text", ["[1, 2, 3]", '[{"a": 1}]'])
def test_parse_json_object_rejects_top_level_array(text: str) -> None:
    with pytest.raises(ModelCallFailed, match="array"):
        parse_json_object(text)


def test_parse_json_object_error_echoes_original_prefix() -> None:
    marker = "坏输出标记"
    bad = marker + "{oops}" + "尾" * 400

    with pytest.raises(ModelCallFailed) as excinfo:
        parse_json_object(bad)

    assert marker in str(excinfo.value)


# --------------------------------------------------------------------------- #
# OpenAICompatibleModel（httpx.MockTransport：不联网）
# --------------------------------------------------------------------------- #

OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5},
}


def _ok_transport(seen: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    return httpx.MockTransport(handler)


def test_openai_model_posts_chat_completions_and_reads_text_and_tokens() -> None:
    seen: dict[str, object] = {}
    model = OpenAICompatibleModel(
        base_url="http://llm.test/v1/",
        api_key="secret",
        model="qwen-max",
        transport=_ok_transport(seen),
    )

    reply = model.complete(system="be terse", prompt="hello", temperature=0.2)

    assert reply.text == '{"ok": true}'
    assert (reply.tokens_in, reply.tokens_out) == (12, 5)
    assert reply.raw["usage"]["prompt_tokens"] == 12
    assert seen["url"] == "http://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"] == {
        "model": "qwen-max",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
        ],
        "temperature": 0.2,
        "stream": False,
        "max_tokens": model.max_tokens,
    }


def test_openai_model_missing_usage_defaults_to_zero() -> None:
    body = {"choices": [{"message": {"content": "hi"}}]}
    model = OpenAICompatibleModel(
        base_url="http://llm.test/v1",
        api_key="secret",
        model="m",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )

    reply = model.complete(system="s", prompt="p")

    assert (reply.tokens_in, reply.tokens_out) == (0, 0)


def test_openai_model_server_error_carries_status_and_truncated_body() -> None:
    model = OpenAICompatibleModel(
        base_url="http://llm.test/v1",
        api_key="secret",
        model="m",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, text="x" * (MAX_BODY_CHARS * 2))
        ),
    )

    with pytest.raises(ModelCallFailed) as excinfo:
        model.complete(system="s", prompt="p")

    error = excinfo.value
    assert error.status_code == 500
    assert len(error.body) == MAX_BODY_CHARS
    assert "500" in str(error)


def test_openai_model_malformed_body_fails_without_status() -> None:
    model = OpenAICompatibleModel(
        base_url="http://llm.test/v1",
        api_key="secret",
        model="m",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": []})),
    )

    with pytest.raises(ModelCallFailed) as excinfo:
        model.complete(system="s", prompt="p")

    assert excinfo.value.status_code == 200


def test_openai_model_transport_error_becomes_model_call_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    model = OpenAICompatibleModel(
        base_url="http://llm.test/v1",
        api_key="secret",
        model="m",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelCallFailed, match="model request failed") as excinfo:
        model.complete(system="s", prompt="p")

    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


# --------------------------------------------------------------------------- #
# from_env：两套命名
# --------------------------------------------------------------------------- #

def test_from_env_reads_jagent_names() -> None:
    model = from_env(
        {
            "JAGENT_LLM_BASE_URL": "http://llm.test/v1",
            "JAGENT_LLM_API_KEY": "secret",
            "JAGENT_LLM_MODEL": "qwen-max",
        }
    )

    assert model is not None
    assert model.base_url == "http://llm.test/v1"
    assert model.api_key == "secret"
    assert model.model == "qwen-max"


def test_from_env_falls_back_to_dotenv_names() -> None:
    model = from_env(
        {
            "LLM_BASE_URL": "https://api.example.com/v1/",
            "LLM_API_KEY": "sk-test",
            "LLM_MODEL_NAME": "qwen3.8-max",
        }
    )

    assert model is not None
    assert model.base_url == "https://api.example.com/v1"
    assert model.api_key == "sk-test"
    assert model.model == "qwen3.8-max"


def test_from_env_prefers_jagent_names() -> None:
    model = from_env(
        {
            "JAGENT_LLM_BASE_URL": "http://jagent.test/v1",
            "JAGENT_LLM_API_KEY": "jagent-key",
            "JAGENT_LLM_MODEL": "jagent-model",
            "LLM_BASE_URL": "http://dotenv.test/v1",
            "LLM_API_KEY": "dotenv-key",
            "LLM_MODEL_NAME": "dotenv-model",
        }
    )

    assert model is not None
    assert model.base_url == "http://jagent.test/v1"
    assert model.model == "jagent-model"


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"LLM_BASE_URL": "http://llm.test/v1"},
        {"LLM_BASE_URL": "http://llm.test/v1", "LLM_API_KEY": "k"},
        {"JAGENT_LLM_API_KEY": "k", "JAGENT_LLM_MODEL": "m"},
        {"JAGENT_LLM_BASE_URL": "  ", "JAGENT_LLM_API_KEY": "k", "JAGENT_LLM_MODEL": "m"},
    ],
)
def test_from_env_returns_none_when_incomplete(env: dict[str, str]) -> None:
    assert from_env(env) is None


def test_from_env_reads_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "JAGENT_LLM_BASE_URL",
        "JAGENT_LLM_API_KEY",
        "JAGENT_LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://env.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    monkeypatch.setenv("LLM_MODEL_NAME", "env-model")

    model = from_env()

    assert model is not None
    assert model.model == "env-model"
    assert model.base_url == "http://env.test/v1"


def test_from_env_can_disable_reasoning() -> None:
    """推理型模型：LLM_DISABLE_THINKING=1 时请求带 enable_thinking=false。"""
    model = from_env(
        {
            "LLM_BASE_URL": "http://llm.test/v1",
            "LLM_API_KEY": "secret",
            "LLM_MODEL_NAME": "reasoner",
            "LLM_DISABLE_THINKING": "1",
        }
    )
    assert model is not None
    assert model.extra == {"enable_thinking": False}


def test_from_env_without_disable_thinking_sends_no_extra() -> None:
    model = from_env(
        {
            "LLM_BASE_URL": "http://llm.test/v1",
            "LLM_API_KEY": "secret",
            "LLM_MODEL_NAME": "plain",
        }
    )
    assert model is not None
    assert model.extra == {}
