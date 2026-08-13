from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ExecutionBackend(ABC):
    @property
    @abstractmethod
    def root(self) -> str:
        """Return the logical root for diagnostics."""

    @abstractmethod
    async def resolve(self, path: str) -> str: ...

    @abstractmethod
    async def read(self, path: str) -> dict[str, Any]: ...

    @abstractmethod
    async def write(self, path: str, content: str) -> dict[str, Any]: ...

    @abstractmethod
    async def append(self, path: str, content: str) -> dict[str, Any]: ...

    @abstractmethod
    async def delete(self, path: str) -> dict[str, Any]: ...

    @abstractmethod
    async def list(self, path: str) -> dict[str, Any]: ...

    async def run_command(self, cmd: str, cwd: str | None = None) -> Any:
        raise NotImplementedError("run_command is not enabled for this backend")

    async def close(self) -> None:
        return None
