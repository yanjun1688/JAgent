from __future__ import annotations

from collections import deque
from typing import Any

from pydantic import BaseModel, Field


class DagStep(BaseModel):
    id: str = ""
    tool: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    description: str = ""
    upstream_selectors: dict[str, str] | None = None
    max_parallel: int = 10
    branches: dict | None = None


class DagPlan(BaseModel):
    intent: str = ""
    steps: list[DagStep] = Field(default_factory=list)
    dynamic: bool = False

    def _step_map(self) -> dict[str, DagStep]:
        return {s.id: s for s in self.steps}

    def topological_sort(
        self, completed_step_ids: set[str] | None = None,
    ) -> list[list[str]]:
        steps = self._step_map()
        in_degree: dict[str, int] = {}
        adjacency: dict[str, list[str]] = {}

        all_valid = set(steps.keys()) | (completed_step_ids or set())

        for s in self.steps:
            in_degree[s.id] = 0
            adjacency[s.id] = []

        for s in self.steps:
            for dep in s.depends_on:
                if dep not in all_valid:
                    raise ValueError(f"Step '{s.id}': depends on unknown step '{dep}'")
                if dep not in steps:
                    continue
                adjacency.setdefault(dep, []).append(s.id)
                in_degree[s.id] = in_degree.get(s.id, 0) + 1

        layers: list[list[str]] = []
        queue = deque([sid for sid, deg in in_degree.items() if deg == 0])

        visited = 0
        while queue:
            layer = []
            for _ in range(len(queue)):
                sid = queue.popleft()
                layer.append(sid)
                visited += 1
                for neighbor in adjacency.get(sid, []):
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)
            layers.append(layer)

        if visited != len(self.steps):
            raise ValueError("Cycle detected in DAG plan")

        return layers

    def upstream_outputs(
        self, step_id: str, results: dict[str, Any],
    ) -> dict[str, Any]:
        step = self._step_map().get(step_id)
        if not step:
            return {}
        merged = {}
        for dep_id in step.depends_on:
            dep_result = results.get(dep_id)
            if dep_result is None:
                merged[dep_id] = None
                continue
            output = dep_result.get("output") if isinstance(dep_result, dict) else (dep_result.output if hasattr(dep_result, 'output') else dep_result)
            selectors = step.upstream_selectors or {}
            if dep_id in selectors:
                merged[dep_id] = self._resolve_path(output, selectors[dep_id])
            else:
                merged[dep_id] = output
        return merged

    @staticmethod
    def _resolve_path(obj: Any, path: str) -> Any:
        parts = path.split(".")
        current = obj
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, (list, tuple)) and part.isdigit():
                idx = int(part)
                current = current[idx] if 0 <= idx < len(current) else None
            else:
                return None
        return current
