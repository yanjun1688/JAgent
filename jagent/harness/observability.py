"""JAgent v8 · 可观测性视图（D-031 S1）。

``ObservationModel`` 是 ModelPort 的观测装饰器：每次调用落 ``model.called``
（purpose / token / 耗时 / 长度），prompt 与回复全文落产物库。它**写事件**，
所以按 §3.2 住在 harness/（agent/ 只产出判定与文本，落盘归 harness）——
wiring 给每个组件发一个带 purpose 标签的视图，``ModelPort`` 签名零改动。
"""

from __future__ import annotations

import time
from typing import Any

from ..contracts import EventType


class ObservationModel:
    """可观测性 S1（D-031）：带观测的 ``ModelPort`` 视图。

    wiring 给每个组件（router / planner / propose / judge / repair）发一个
    purpose 标签视图；调用签名与 ModelPort 完全一致，组件无感知。
    已知边界（D-031）：``create_app`` 直连装配的 judge 不经本视图。
    """

    def __init__(
        self,
        inner: Any,
        *,
        purpose: str,
        writer: Any,
        artifacts: Any = None,
    ) -> None:
        self._inner = inner
        self._purpose = purpose
        self._writer = writer
        self._artifacts = artifacts
        #: gate 的 ``_judge_model`` 按名字读模型标识——透传内层的名字。
        self.name = getattr(inner, "model", None) or type(inner).__name__

    def complete(self, *, system: str, prompt: str, temperature: float = 0.0) -> Any:
        started = time.perf_counter()
        prompt_text = f"{system}\n\n{prompt}" if system else prompt
        try:
            reply = self._inner.complete(system=system, prompt=prompt, temperature=temperature)
        except Exception as exc:  # noqa: BLE001 - 失败的调用也要观测：outage/超时是最需要看见的时刻
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._writer.emit(
                EventType.MODEL_CALLED,
                {
                    "purpose": self._purpose,
                    "model": self.name,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "duration_ms": duration_ms,
                    "prompt_chars": len(prompt_text),
                    "reply_chars": 0,
                    "prompt_artifact_id": self._store(prompt_text),
                    "reply_artifact_id": None,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        self._writer.emit(
            EventType.MODEL_CALLED,
            {
                "purpose": self._purpose,
                "model": self.name,
                "tokens_in": int(getattr(reply, "tokens_in", 0) or 0),
                "tokens_out": int(getattr(reply, "tokens_out", 0) or 0),
                "duration_ms": duration_ms,
                "prompt_chars": len(prompt_text),
                "reply_chars": len(getattr(reply, "text", "") or ""),
                "prompt_artifact_id": self._store(prompt_text),
                "reply_artifact_id": self._store(getattr(reply, "text", "") or ""),
            },
        )
        return reply

    def _store(self, text: str) -> str | None:
        if self._artifacts is None or not text:
            return None
        return self._artifacts.put_text(text).artifact_id


