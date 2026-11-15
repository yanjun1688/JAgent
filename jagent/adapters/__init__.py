"""JAgent v8 · 外部适配层（Adapter Layer）。

`harness/` 是确定性内核：不认模型、不直连网络（`tests/v8_arch/test_conformance.py`
会拦）。凡是「要出网 / 要调外部服务」的工具都放在这里，再通过 `ToolRegistry`
注册进工具层，由 Executor 统一调用：

* `mcp` —— Fuyao MCP（JSON-RPC over HTTP）的只读行情 / 资料工具。

依赖方向：`adapters/` 可以 import `harness/` 与 `contracts`；反过来不行
（`harness/` 不知道 adapters 的存在）。
"""
