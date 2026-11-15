"""JAgent v8 · 配置加载（宪法 I6 / brief §5.5）。

prompt / budget 是数据，不是代码：启动时从**包内** ``jagent/config/`` 读入，
并对 ``config/**`` + ``gate/policy.md`` 算一个 sha256。缺文件、缺字段一律拒绝启动，
不回落代码里的内联默认值——"配置漏写却照样跑"会让 A/B 结果无法归因。

没有全局单例：``wiring.build_app`` 调一次 :func:`load_config`，把 ``Config`` 注入各组件。
路径按 ``__file__`` 解析，与进程 CWD 无关。
"""

from __future__ import annotations

import hashlib
import subprocess
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .contracts import Budget

#: 包内配置目录（``jagent/config``）。按 ``__file__`` 解析，**不按 CWD**。
CONFIG_ROOT = Path(__file__).resolve().parent / "config"

#: 运行期必须存在的 prompt 文件（文件名去 ``.md``）。缺一个就拒绝启动。
REQUIRED_PROMPTS: tuple[str, ...] = (
    "router",
    "planner",
    "planner_select",
    "propose",
    "judge",
    "repair",
)


class ConfigError(RuntimeError):
    """配置缺失或损坏：启动必须失败；不存在"用内联默认值继续跑"的分支。"""


@dataclass(frozen=True)
class Config:
    """一次启动读到的全部外部化旋钮。

    ``hash`` 覆盖 ``config/**`` 与 ``gate/policy.md``：改任何一个字节都会变。
    """

    root: Path
    prompts: dict[str, str]
    budget: Budget
    policy_path: Path
    hash: str


def load_config(root: Path | None = None) -> Config:
    """读配置并算指纹；``root`` 只用于测试或替换整棵配置树。

    缺 ``budget.toml``、``gate/policy.md`` 或预算字段时抛 :class:`ConfigError`。
    """
    root = CONFIG_ROOT if root is None else Path(root).resolve()
    budget_path = root / "budget.toml"
    if not budget_path.is_file():
        raise ConfigError(f"缺少预算配置：{budget_path}")
    budget = _load_budget(budget_path)
    prompts = _load_prompts(root)
    missing = [name for name in REQUIRED_PROMPTS if name not in prompts]
    if missing:
        names = ", ".join(f"{name}.md" for name in missing)
        raise ConfigError(f"缺少 prompt 文件：{names}（目录 {root / 'prompts'}）")
    policy_path = _policy_path(root)
    return Config(
        root=root,
        prompts=prompts,
        budget=budget,
        policy_path=policy_path,
        hash=_hash(root, policy_path),
    )


def _load_budget(path: Path) -> Budget:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取预算配置 {path}：{exc}") from exc
    try:
        return Budget.from_mapping(data)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"预算配置 {path} 非法：{exc}") from exc


def _load_prompts(root: Path) -> dict[str, str]:
    """``prompts/*.md`` → ``{文件名去后缀: 内容}``；内容原样读入，不做模板解释。"""
    directory = root / "prompts"
    if not directory.is_dir():
        return {}
    try:
        return {
            path.stem: path.read_text(encoding="utf-8")
            for path in sorted(directory.glob("*.md"))
        }
    except OSError as exc:
        raise ConfigError(f"无法读取 prompt 目录 {directory}：{exc}") from exc


def _policy_path(root: Path) -> Path:
    """``config/`` 与 ``gate/`` 是同一个包内的兄弟目录。"""
    path = root.parent / "gate" / "policy.md"
    if not path.is_file():
        raise ConfigError(f"缺少策略文件：{path}")
    return path


def _hash(root: Path, policy_path: Path) -> str:
    """sha256 over 排序后的 ``(相对路径, 字节)``：``config/**`` + ``gate/policy.md``。

    相对路径一律相对包目录（``config/`` 的父目录），所以同一份配置换机器/换工作区
    仍得到同一个 hash；绝对路径不参与。
    """
    package = root.parent
    entries = [
        (path.relative_to(package).as_posix(), path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    entries.append((policy_path.relative_to(package).as_posix(), policy_path))
    digest = hashlib.sha256()
    for relative, path in sorted(entries):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def config_manifest(root: Path | None = None) -> dict[str, str]:
    """``config/**`` + ``gate/policy.md`` 的逐文件 sha256 清单（D-035）。

    与 :func:`_hash` 同一套文件枚举（相对包目录的 posix 路径为键），所以
    manifest 的键集合与 ``config_hash`` 的覆盖面一致——评估对照（compare）
    用两份 manifest 的差异把 config_hash 变化归因到具体文件。
    """
    root = CONFIG_ROOT if root is None else Path(root).resolve()
    package = root.parent
    policy_path = root.parent / "gate" / "policy.md"
    entries = [
        (path.relative_to(package).as_posix(), path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    entries.append((policy_path.relative_to(package).as_posix(), policy_path))
    return {
        relative: hashlib.sha256(path.read_bytes()).hexdigest()
        for relative, path in sorted(entries)
    }


@lru_cache(maxsize=1)
def git_commit() -> str:
    """本次启动的代码指纹：``git rev-parse --short HEAD``（脏工作区加 ``+dirty``）。

    探一次就缓存：一次进程里所有 run 记的是同一个值，不会因为跑到一半提交而变化。
    拿不到 git（非仓库 / 没装 git / 命令失败）一律返回 ``"unknown"``，绝不抛错。
    """
    return _probe_git_commit()


def _probe_git_commit() -> str:
    repo = Path(__file__).resolve().parents[1]
    head = _git(repo, "rev-parse", "--short", "HEAD")
    if head is None or not head.strip():
        return "unknown"
    commit = head.strip()
    # 状态探不到时只报 commit，不假装干净也不假装脏。
    status = _git(repo, "status", "--porcelain")
    if status is not None and status.strip():
        return f"{commit}+dirty"
    return commit


def _git(repo: Path, *args: str) -> str | None:
    """在包目录跑一条只读 git 命令；失败返回 ``None``（调用方决定怎么降级）。"""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def runtime_env(config: Config) -> dict[str, str]:
    """一次 run 的环境指纹：代码版本 + 配置 hash。只读已加载的 ``Config``，不重读文件。"""
    return {"git_commit": git_commit(), "config_hash": config.hash}
