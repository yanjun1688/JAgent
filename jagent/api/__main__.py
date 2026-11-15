"""启动 HTTP 服务：`python -m jagent.api`

这是产品的唯一入口（没有 CLI）：
    python -m jagent.api            # 默认 http://127.0.0.1:8021
    JAGENT_PORT=9000 python -m jagent.api

环境变量：
    JAGENT_DATA_DIR   数据目录（默认 data/v8_http，内含 events.db / artifacts/ / ws/）
    JAGENT_PORT       监听端口（默认 8021）
    JAGENT_LLM_*      接模型时用；不配则用模板/规则（离线可跑）
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> dict[str, str]:
    """读仓库根的 .env（没有就跳过）。

    不装 python-dotenv：产品只需要 KEY=VALUE，注释与空行跳过即可。
    环境变量优先于文件，避免部署时被仓库里的文件覆盖。
    """
    merged = dict(os.environ)
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if not candidate.is_file():
            continue
        # utf-8-sig：`.env` 常带 BOM，用 utf-8 读会把首行键名读成 `\ufeffLLM_API_KEY`，
        # 于是 `LLM_API_KEY` 取不到、`from_env` 静默返回 None（模型永远连不上）。
        for line in candidate.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            merged.setdefault(key, value)
        break
    return merged


def _probe(model) -> bool:
    """起服务前试一次模型。配额耗尽/网络不通时宁可先说清楚，也不要让用户踩坑。"""
    try:
        model.complete(system="只回答一个词。", prompt="ok", temperature=0.0)
        return True
    except Exception:
        return False


def main() -> None:
    import uvicorn

    from ..adapters.mcp import mcp_servers_from_env
    from .app import create_app

    root = os.environ.get("JAGENT_DATA_DIR", "data/v8_http")
    port = int(os.environ.get("JAGENT_PORT", "8021"))
    env = _load_dotenv()

    model = None
    try:
        from ..agent.model import from_env

        candidate = from_env(env)
        if candidate is not None and _probe(candidate):
            model = candidate
        elif candidate is not None:
            print("模型不可用（配额/网络/鉴权），本次以模板模式启动。")
    except Exception:  # 模型层没装好也要能起服务，降级为模板
        model = None

    # MCP 是工具层的外部工具源；连不上就跳过，不影响服务启动。
    mcp_servers = mcp_servers_from_env(env)
    application = create_app(root=root, model=model, mcp_servers=mcp_servers)
    app_obj = application.state.jagent
    if mcp_servers:
        mcp_note = f"{len(app_obj.mcp_tools)} tools / {len(mcp_servers)} servers"
    else:
        mcp_note = "off"
    print(
        f"JAgent 已启动：http://127.0.0.1:{port}  "
        f"(model={'on' if model else 'template'}, mcp={mcp_note})"
    )
    uvicorn.run(application, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
