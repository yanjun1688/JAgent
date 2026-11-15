"""JAgent v8 · 模型适配层。

`agent/` 只认 `ModelPort`（见 `jagent/ports.py`）；这个文件是整条链路里**唯一**
知道 HTTP、密钥和模型名的地方。四种实现住在一起，方便对照：

- `OpenAICompatibleModel`：真实网络调用，OpenAI `chat/completions` 协议，只用 httpx。
- `ScriptedModel` / `CountingModel`：测试替身，可脚本化、可计数、可断言。
- `ReplayModel`：重放模式的"零模型"证明——任何调用都抛 `ModelUnavailable`。

失败语义只有两类，不要混：

- `ModelUnavailable`：根本没接模型，或当前处于重放（不该调用）。
- `ModelCallFailed`：接了模型、这次调用坏了（网络 / HTTP 非 2xx / 响应结构不对）。

配置（`from_env`）优先读 `JAGENT_LLM_*`；没有就回退仓库 `.env` 实际使用的
`LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_NAME`。三者缺一返回 `None`，让装配根
继续用模板/规则驱动，而不是假装模型可用（旧适配器只读 `JAGENT_*`，在仓库
自己的 `.env` 上会静默返回 `None`）。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from ..contracts import digest_of
from ..ports import ModelPort, ModelReply, ModelUnavailable

#: 一次 HTTP 调用的默认超时。planner 的提示词带着全部工具描述，长请求在慢模型上
#: 60s 会直接 ReadTimeout；默认放宽到 120s，并允许用 `LLM_TIMEOUT_S` 覆盖。
DEFAULT_TIMEOUT_S = 120.0
#: 单次生成的最大 token 数。不设上限时模型可能长篇输出，把一次调用拖到超时。
DEFAULT_MAX_TOKENS = 2048
#: 异常/日志里回显响应体的上限。
MAX_BODY_CHARS = 500
#: 解析失败时回显原始输出的上限。
MAX_ECHO_CHARS = 300

#: 配置来源，按优先级排列。`LLM_*` 是仓库 `.env` 的命名。
BASE_URL_ENVS: tuple[str, ...] = ("JAGENT_LLM_BASE_URL", "LLM_BASE_URL")
API_KEY_ENVS: tuple[str, ...] = ("JAGENT_LLM_API_KEY", "LLM_API_KEY")
MODEL_ENVS: tuple[str, ...] = ("JAGENT_LLM_MODEL", "LLM_MODEL_NAME")
TIMEOUT_ENVS: tuple[str, ...] = ("JAGENT_LLM_TIMEOUT_S", "LLM_TIMEOUT_S")
MAX_TOKENS_ENVS: tuple[str, ...] = ("JAGENT_LLM_MAX_TOKENS", "LLM_MAX_TOKENS")
#: 关掉推理型模型的「思考过程」（DashScope 的 enable_thinking）。推理 token 会
#: 吃掉 max_tokens 并把单次调用拖到分钟级；planner 只需要结构化输出，不需要思考。
DISABLE_THINKING_ENVS: tuple[str, ...] = (
    "JAGENT_LLM_DISABLE_THINKING",
    "LLM_DISABLE_THINKING",
)


class ModelCallFailed(RuntimeError):  # noqa: N818
    """这次模型调用失败了：网络 / HTTP 非 2xx / 响应结构不对。

    名字带 `Failed` 而不是 `Error` 是 v8 契约的一部分（调用方按它分类），
    因此这里显式豁免 N818。

    `status_code` 为 `None` 表示没拿到 HTTP 响应（连接失败、超时等）；
    `body` 是截断后的响应体，供失败分类与审计读取。
    """

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        self.status_code = status_code
        self.body = body[:MAX_BODY_CHARS]
        detail = message
        if status_code is not None:
            detail = f"{detail} (HTTP {status_code})"
        if self.body:
            detail = f"{detail}: {self.body}"
        super().__init__(detail)


def call_digest(system: str, prompt: str) -> str:
    """一次调用的稳定指纹（重放对账 / 去重用）。"""
    return digest_of({"system": system, "prompt": prompt})


# --------------------------------------------------------------------------- #
# 真实适配器：OpenAI 兼容接口
# --------------------------------------------------------------------------- #

def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _as_count(value: Any) -> int:
    """把 usage 里的 token 数整理成非负 int；缺失/奇怪的值一律 0。"""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    try:
        return max(int(str(value).strip()), 0)
    except (TypeError, ValueError):
        return 0


def _content_of(body: Mapping[str, Any]) -> str | None:
    """`choices[0].message.content`；结构不对返回 None。"""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


class OpenAICompatibleModel:
    """最小 chat-completions 客户端：`POST {base_url}/chat/completions`。

    `transport` 直接交给 httpx：测试用 `httpx.MockTransport`，生产留 `None`。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        extra: Mapping[str, Any] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        #: 额外的请求参数（部署配置驱动，例如 {"enable_thinking": False}）。
        self.extra: dict[str, Any] = dict(extra or {})
        self.transport = transport

    def _post(self, client: httpx.Client, payload: dict[str, Any]) -> httpx.Response:
        return client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    def complete(self, *, system: str, prompt: str, temperature: float = 0.0) -> ModelReply:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "stream": False,
        }
        if self.max_tokens > 0:
            payload["max_tokens"] = self.max_tokens
        payload.update(self.extra)
        try:
            with httpx.Client(timeout=self.timeout_s, transport=self.transport) as client:
                response = self._post(client, payload)
                # 有些模型不允许关闭思考（enable_thinking 只能是 True）：
                # 400 且点名这个参数时，去掉它重试一次，并记住不再带。
                if (
                    response.status_code == 400
                    and "enable_thinking" in response.text
                    and "enable_thinking" in self.extra
                ):
                    self.extra.pop("enable_thinking", None)
                    payload.pop("enable_thinking", None)
                    response = self._post(client, payload)
        except httpx.HTTPError as exc:
            raise ModelCallFailed(f"model request failed: {type(exc).__name__}: {exc}") from exc

        if not 200 <= response.status_code < 300:
            raise ModelCallFailed(
                "model endpoint returned an error",
                status_code=response.status_code,
                body=response.text,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ModelCallFailed(
                "model response is not valid JSON",
                status_code=response.status_code,
                body=response.text,
            ) from exc
        if not isinstance(body, dict):
            raise ModelCallFailed(
                "model response is not a JSON object",
                status_code=response.status_code,
                body=response.text,
            )

        content = _content_of(body)
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        completion = _as_count(usage.get("completion_tokens"))
        # 空正文 + 有 token 消耗 = 预算被推理过程吃光（推理型模型常见）。
        # 这种响应不能当「模型给了空计划」处理，要当调用失败，让上层说人话。
        if content is None or (not content.strip() and completion > 0):
            raise ModelCallFailed(
                "model returned empty content"
                + (" (max_tokens exhausted by reasoning)" if completion > 0 else ""),
                status_code=response.status_code,
                body=response.text,
            )

        return ModelReply(
            text=content,
            tokens_in=_as_count(usage.get("prompt_tokens")),
            tokens_out=completion,
            raw=body,
        )


def _first_present(values: Mapping[str, str], names: Sequence[str]) -> str:
    for name in names:
        raw = values.get(name)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def from_env(env: dict[str, str] | None = None) -> OpenAICompatibleModel | None:
    """从环境构造适配器；缺任何一项都返回 `None`（不抛）。

    `env` 显式传入时只读这个 dict（测试用）；为 `None` 时读 `os.environ`。
    """
    values: Mapping[str, str] = os.environ if env is None else env
    base_url = _first_present(values, BASE_URL_ENVS)
    api_key = _first_present(values, API_KEY_ENVS)
    model = _first_present(values, MODEL_ENVS)
    if not (base_url and api_key and model):
        return None
    raw_timeout = _first_present(values, TIMEOUT_ENVS)
    try:
        timeout_s = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_S
    except ValueError:
        timeout_s = DEFAULT_TIMEOUT_S
    raw_max = _first_present(values, MAX_TOKENS_ENVS)
    try:
        max_tokens = int(raw_max) if raw_max else DEFAULT_MAX_TOKENS
    except ValueError:
        max_tokens = DEFAULT_MAX_TOKENS
    extra: dict[str, Any] = {}
    if _first_present(values, DISABLE_THINKING_ENVS).lower() in {"1", "true", "yes", "on"}:
        extra["enable_thinking"] = False
    return OpenAICompatibleModel(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_s=timeout_s,
        max_tokens=max_tokens,
        extra=extra,
    )


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #

class ScriptedModel:
    """按脚本顺序回复，并记录每次调用收到的 `(system, prompt)`。"""

    def __init__(self, replies: Sequence[str | ModelReply | dict[str, Any]]) -> None:
        self._replies = list(replies)
        #: 每次 complete 收到的 (system, prompt)，按调用顺序。
        self.calls: list[tuple[str, str]] = []

    @property
    def digests(self) -> list[str]:
        """每次调用的稳定指纹，便于重放对账。"""
        return [call_digest(system, prompt) for system, prompt in self.calls]

    def complete(self, *, system: str, prompt: str, temperature: float = 0.0) -> ModelReply:
        index = len(self.calls)
        self.calls.append((system, prompt))
        if index >= len(self._replies):
            raise ModelCallFailed(
                f"scripted model exhausted: call #{index + 1} has no reply "
                f"(scripted {len(self._replies)})"
            )
        reply = self._replies[index]
        if isinstance(reply, ModelReply):
            return reply
        if isinstance(reply, str):
            return ModelReply(text=reply)
        if isinstance(reply, dict):
            return ModelReply(text=json.dumps(reply, ensure_ascii=False), raw=reply)
        raise ModelCallFailed(
            f"scripted reply #{index + 1} has unsupported type {type(reply).__name__}"
        )


class ReplayModel:
    """重放模式：证明零模型调用。任何 complete 都抛 `ModelUnavailable`。"""

    def complete(
        self, *, system: str = "", prompt: str = "", temperature: float = 0.0
    ) -> ModelReply:
        raise ModelUnavailable("replay mode: model calls are disabled")


class CountingModel:
    """包装另一个 `ModelPort`，数调用次数；超过 `fail_after` 就失败。"""

    def __init__(self, inner: ModelPort, *, fail_after: int | None = None) -> None:
        self.inner = inner
        self.fail_after = fail_after
        #: 已经发生的 complete 次数（含失败的那次）。
        self.calls = 0

    def complete(self, *, system: str, prompt: str, temperature: float = 0.0) -> ModelReply:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ModelCallFailed(
                f"counting model: call {self.calls} exceeds fail_after={self.fail_after}"
            )
        return self.inner.complete(system=system, prompt=prompt, temperature=temperature)


# --------------------------------------------------------------------------- #
# 输出解析
# --------------------------------------------------------------------------- #

_FENCE_LINE = re.compile(r"^\s*```[^\n]*$", re.MULTILINE)
_MISSING = object()


def _strip_fences(text: str) -> str:
    """去掉 ```json / ``` 围栏行（整行，任意位置）。"""
    if "```" not in text:
        return text
    return _FENCE_LINE.sub("", text)


def _balanced_object_span(text: str) -> tuple[int, int] | None:
    """第一个平衡的 `{...}` 的 [start, end)；正确处理字符串里的花括号与转义。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1
    return None


def parse_json_object(text: str) -> dict[str, Any]:
    """把模型输出解析成一个 JSON object。

    容忍 ```json 围栏与前后散文；取第一个平衡的 `{...}`。任何失败都抛
    `ModelCallFailed`，message 里带原始输出前 300 字，便于定位坏输出。
    """
    original = text if isinstance(text, str) else str(text)
    echo = original[:MAX_ECHO_CHARS]
    body = _strip_fences(original).strip()
    if not body:
        raise ModelCallFailed(f"model output is empty, expected a JSON object: {echo!r}")

    parsed: object = _MISSING
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = _MISSING
    else:
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            raise ModelCallFailed(
                f"model output is a JSON array, expected a JSON object: {echo!r}"
            )

    span = _balanced_object_span(body)
    if span is None:
        raise ModelCallFailed(f"model output has no balanced JSON object: {echo!r}")

    candidate = body[span[0]:span[1]]
    try:
        outer = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ModelCallFailed(
            f"model output is not valid JSON ({exc.msg}): {echo!r}"
        ) from exc
    if not isinstance(outer, dict):
        raise ModelCallFailed(f"model output is not a JSON object: {echo!r}")
    return outer
