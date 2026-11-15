"""共享夹具：把包内 ``config/`` 复制到 tmp，并把 ``gate/policy.md`` 放成兄弟目录。

``load_config(root)`` 的 root 是包内的 config 目录，policy 固定从 ``root.parent/gate/policy.md``
取；所以复制配置树时必须一起复制策略文件，否则测试验的不是同一套路径规则。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from jagent.config import CONFIG_ROOT

PACKAGE = CONFIG_ROOT.parent


@pytest.fixture()
def config_copy(tmp_path: Path) -> Path:
    root = tmp_path / "config"
    shutil.copytree(CONFIG_ROOT, root)
    gate = tmp_path / "gate"
    gate.mkdir()
    shutil.copy(PACKAGE / "gate" / "policy.md", gate / "policy.md")
    return root
