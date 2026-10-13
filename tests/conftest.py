"""pytest 全局配置。

live 测试卫生：标记 @pytest.mark.live 的测试会真打 staging 网络，
默认**永远跳过**——只有显式加 `--run-live` 才执行。这样即便 .env 里有
token（会自动加载），本地/CI 的默认 `pytest` 也保持离线、稳定、绿。

  .venv/bin/python -m pytest --run-live -v      # 真跑 live（需有效 token/账户）
  .venv/bin/python -m pytest                     # 默认：live 全 skip
"""

from __future__ import annotations

import os

import pytest

# 测试隔离（必须在任何 app/get_settings 导入前执行）：强制 fake provider。
# os.environ 优先级高于 .env 文件，屏蔽开发者本地 .env 里的 provider_mode=real +
# AGENT_REDIS_URL / AGENT_DATABASE_URL，保证离线单测不去连真 redis/PG。
# live 测试自建 AgentOsStagingClient、不经容器，不受此影响。
os.environ["AGENT_PROVIDER_MODE"] = "fake"
os.environ["AGENT_ENV"] = "local"  # 与 fake provider 一致，屏蔽开发者 .env 的 AGENT_ENV=staging，避免 validate_runtime_mode 冲突


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="真打 staging 的 live 测试（需有效凭证）；不加则全部跳过。",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live 测试默认跳过，加 --run-live 才执行")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
