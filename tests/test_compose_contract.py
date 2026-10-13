"""docker-compose.yml 的契约测试。

这个文件不是摆设：部署工作流把它 scp 到 `/home/sxy/releases/<sha>/` 再从那里
`docker compose -p app up -d`，所以它写错的代价是 staging 部署失败。

已经踩过一次：jaeger 镜像 tag 写成两段式 `1.76`，registry 里根本不存在
（`1.60` 之后 Jaeger 不再发布两段式 tag），`up -d` 拉镜像直接失败。当时是靠
CI 报错才发现，而这类错本地一条断言就能挡掉。
"""

from __future__ import annotations

import pathlib
import re

import yaml

_COMPOSE = pathlib.Path(__file__).resolve().parents[1] / "docker-compose.yml"

# 三段式语义化版本。两段式（1.76）与浮动 tag（latest / 1）都不接受。
_PINNED_TAG = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

# 只在 compose 里声明 image 的服务受此约束；agent-runtime 是 build 出来的。
_FLOATING = {"latest", "stable", "main", "master", "edge"}


def _services() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())["services"]


def test_compose_parses_and_declares_the_expected_services() -> None:
    services = _services()
    for name in ("agent-runtime", "redis", "jaeger"):
        assert name in services, name


def test_third_party_images_are_pinned_to_a_full_version() -> None:
    """浮动 tag 会让某次拉取悄悄换掉大版本 —— Jaeger v2 与 v1 的配置方式不兼容。"""

    for name, spec in _services().items():
        image = spec.get("image")
        if image is None:  # 本地 build 的服务不受约束
            continue
        assert ":" in image, f"{name} 的 image 没有 tag：{image}"
        tag = image.rsplit(":", 1)[1]
        assert tag not in _FLOATING, f"{name} 用了浮动 tag {tag!r}，请钉死具体版本"


def test_jaeger_image_uses_a_three_part_tag_that_exists_upstream() -> None:
    """两段式 tag（1.76）在 Jaeger 的 registry 里不存在，会在 up -d 时才炸。"""

    tag = _services()["jaeger"]["image"].rsplit(":", 1)[1]
    assert _PINNED_TAG.match(tag), (
        f"jaeger tag {tag!r} 不是三段式版本号。Jaeger 自 1.60 起只发布 X.Y.Z，"
        "写成 1.76 这种两段式会在 docker compose up -d 拉镜像时失败"
    )


# 只有对外提供 API 的服务允许发布到所有网卡；其余一律绑回环。
_PUBLIC_BY_DESIGN = {"agent-runtime"}


def test_only_the_api_service_publishes_on_all_interfaces() -> None:
    """后端组件不该有公网监听。

    两个都是实打实踩过的：
    * Jaeger UI 无鉴权，链路里带 unified_account_id / request_id / 下游 URL；
    * Redis 的 requirepass 为空、protected-mode=no，绑 0.0.0.0 时从公网直连
      6380 可以成功握手 —— 任何人都能读写 session 或 FLUSHALL。
    """

    for name, spec in _services().items():
        if name in _PUBLIC_BY_DESIGN:
            continue
        for port in spec.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (
                f"服务 {name} 的端口 {port!r} 发布在所有网卡上。"
                "后端组件只能绑 127.0.0.1，需要访问就走 SSH 隧道"
            )


def test_jaeger_memory_is_capped() -> None:
    """all-in-one 是纯内存存储，staging 一共 4G，不封顶会被吃满。"""

    env = _services()["jaeger"].get("environment", [])
    joined = " ".join(env) if isinstance(env, list) else " ".join(f"{k}={v}" for k, v in env.items())
    assert "MEMORY_MAX_TRACES=" in joined, "jaeger 必须设 MEMORY_MAX_TRACES"
