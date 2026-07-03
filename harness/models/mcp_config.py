from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class MCPConnectionConfig(BaseModel):
    name: str
    command: list[str] | None = None
    url: str | None = None
    enabled: bool = True
    environment: dict[str, str] = Field(default_factory=dict)
    auto_register_tools: bool = False
    timeout_ms: int = 120000


class MCPConfig(BaseModel):
    servers: list[MCPConnectionConfig] = Field(default_factory=list)

    @classmethod
    def from_file(cls, path: str | Path) -> MCPConfig:
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPConfig:
        return cls(servers=[MCPConnectionConfig(**s) for s in data.get("servers", [])])

    @classmethod
    def from_env(cls) -> MCPConfig:
        config_path = os.environ.get("HARNESS_MCP_CONFIG", "mcp_servers.json")
        return cls.from_file(config_path)
