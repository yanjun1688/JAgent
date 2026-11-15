"""执行底座 bash 的定位（D-037 的实现细节，2026-11-15 修）。

事故：Windows 上 `where bash` 先命中 `System32\\bash.exe`（WSL 启动器，没有发行版
时一直挂到 60s 超时），而 Git for Windows 的 bash 在 git 旁边。git 装在非默认盘
（`D:\\ToolsInstall\\Git`）时，PATH 与 ProgramFiles 都找不到它——`shell.exec` 于是
全部超时，门禁大面积假红。定位顺序改为：git 旁边的 bash → 常规安装路径 → PATH。
"""

from __future__ import annotations

from pathlib import Path

from jagent.harness import registry


def _fake_git(root: Path) -> Path:
    """造一棵 Git for Windows 目录：cmd/git.exe + bin/bash.exe。"""

    (root / "cmd").mkdir(parents=True)
    (root / "bin").mkdir()
    git = root / "cmd" / "git.exe"
    git.write_text("", encoding="utf-8")
    (root / "bin" / "bash.exe").write_text("", encoding="utf-8")
    return git


def test_win32_prefers_the_bash_next_to_git(monkeypatch, tmp_path: Path) -> None:
    git = _fake_git(tmp_path / "ToolsInstall" / "Git")
    wsl = tmp_path / "System32" / "bash.exe"
    wsl.parent.mkdir()
    wsl.write_text("", encoding="utf-8")

    monkeypatch.setattr(registry.sys, "platform", "win32")
    monkeypatch.setattr(
        registry.shutil, "which", lambda name: {"git": str(git), "bash": str(wsl)}.get(name)
    )

    assert registry._locate_bash() == str(git.parent.parent / "bin" / "bash.exe")
    assert registry._locate_bash() != str(wsl), "WSL 启动器不是 D-037 的执行底座"


def test_win32_falls_back_to_path_when_git_has_no_sibling(monkeypatch, tmp_path: Path) -> None:
    git = tmp_path / "portable" / "git.exe"
    git.parent.mkdir()
    git.write_text("", encoding="utf-8")
    other = tmp_path / "elsewhere" / "bash.exe"
    other.parent.mkdir()
    other.write_text("", encoding="utf-8")

    monkeypatch.setattr(registry.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "missing"))
    monkeypatch.setattr(
        registry.shutil, "which", lambda name: {"git": str(git), "bash": str(other)}.get(name)
    )

    assert registry._locate_bash() == str(other)
