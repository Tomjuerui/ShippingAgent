"""仓库级 pytest 钩子：测试统一使用入库的 tests/config.test.yaml。

不设置时 settings.load() 会回落到未入库的本地 config.yaml，导致 CI 与本地结果不一致。
"""

from __future__ import annotations

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def pytest_configure(config):
    os.environ.setdefault("SBA_CONFIG", str(_HERE / "tests" / "config.test.yaml"))
