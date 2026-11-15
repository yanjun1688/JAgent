"""启动 HTTP 服务：`python -m jagent.api`

这是产品的唯一入口（没有 CLI）：
    python -m jagent.api            # 默认 http://127.0.0.1:8021
    JAGENT_PORT=9000 python -m jagent.api
    JAGENT_HOST=0.0.0.0 python -m jagent.api   # Linux 服务器部署对外暴露

环境变量：
    JAGENT_DATA_DIR   数据目录（默认 data/v8_http，内含 events.db / artifacts/ / ws/）
    JAGENT_HOST       绑定地址（默认 127.0.0.1；对外暴露改 0.0.0.0，本服务无鉴权，勿裸奔公网）
    JAGENT_PORT       监听端口（默认 8021）
    JAGENT_LLM_*      接模型时用；不配则用模板/规则（离线可跑）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path


def _load_dotenv() -> tuple[dict[str, str], str]:
    """读 .env（CWD → 仓库根，找到第一个为止），返回 (合并后的环境, 来源)。

    不装 python-dotenv：产品只需要 KEY=VALUE，注释与空行跳过即可。
    环境变量优先于文件，避免部署时被仓库里的文件覆盖。
    来源写进启动日志——.env 找不到时是"没配"而不是"读了个空文件"，
    两种静默降级必须可区分（运维视角）。
    """
    merged = dict(os.environ)
    candidates = (
        ("cwd", Path.cwd() / ".env"),
        ("repo", Path(__file__).resolve().parents[2] / ".env"),
    )
    for source, candidate in candidates:
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
        return merged, str(candidate)
    return merged, "<unset：没有找到 .env，全部依赖真实环境变量>"


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
    from .app import STATIC_FILE, UI_DIST, create_app
    from .logsink import attach

    root = os.environ.get("JAGENT_DATA_DIR", "data/v8_http")
    host = os.environ.get("JAGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("JAGENT_PORT", "8021"))
    env, env_source = _load_dotenv()

    model = None
    try:
        from ..agent.model import from_env

        candidate = from_env(env)
        if candidate is not None and _probe(candidate):
            model = candidate
        elif candidate is not None:
            logging.getLogger(__name__).warning(
                "模型不可用（配额/网络/鉴权），本次以模板模式启动。"
            )
    except Exception:  # 模型层没装好也要能起服务，降级为模板
        model = None

    # MCP 是工具层的外部工具源；连不上就跳过，不影响服务启动。
    mcp_servers = mcp_servers_from_env(env)
    application = create_app(root=root, model=model, mcp_servers=mcp_servers)
    app_obj = application.state.jagent

    # 日志出口（/api/logs）：root INFO 起，uvicorn 以 log_config=None 启动，
    # 访问线/错误线沿 propagate 流进 sink，控制台仍走 stderr。
    detach_logs = attach(application.state.log_sink)
    log = logging.getLogger(__name__)
    if mcp_servers:
        mcp_note = f"{len(app_obj.mcp_tools)} tools / {len(mcp_servers)} servers"
    else:
        mcp_note = "off"
    ui_mode = "ui/dist" if (UI_DIST / "index.html").is_file() else f"fallback（{STATIC_FILE.name}）"
    # 启动面（部署三件套的可见性）：绑的地址、.env 从哪读的、数据根绝对路径、
    # 面板模式——任何一项不符合预期，启动日志第一行就能看见。
    log.info(
        "JAgent 已启动 http://%s:%d (model=%s, mcp=%s, data=%s, env=%s, ui=%s)",
        host,
        port,
        "on" if model else "template",
        mcp_note,
        Path(root).resolve(),
        env_source,
        ui_mode,
    )
    try:
        uvicorn.run(application, host=host, port=port, log_config=None, log_level="info")
    finally:
        detach_logs()


if __name__ == "__main__":
    main()
