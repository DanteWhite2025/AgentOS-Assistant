"""部署工作流的契约测试。

`appleboy/ssh-action` **不会**自动把 step 的 ``env:`` 传进远端 shell —— 必须在
``with.envs`` 里逐个列出名字。而远端脚本跑在 ``set -u`` 下，少列一个的后果是
``VAR: unbound variable``，整个部署 job 直接失败。

这个坑踩过一次（PR #46 加 OTel 变量时只改了 ``env:``）。好在失败点排在
``docker compose up -d`` 之前，staging 仍在跑旧版本 —— 但 CI 白跑一轮，而且
错误信息离根因很远（报的是远端 bash 行号）。这里把两边一致固化成断言。
"""

from __future__ import annotations

import pathlib
import re

import yaml

_WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/deploy-staging.yml"


def _ssh_steps() -> list[dict]:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "envs" in (step.get("with") or {})
    ]


def test_the_deploy_workflow_still_has_an_ssh_step_with_envs() -> None:
    """守卫失效检查：步骤被重命名或删掉时，下面两条断言会静默变成空跑。"""

    steps = _ssh_steps()
    assert steps, "没找到带 with.envs 的 ssh 步骤；本文件的其余断言会失去意义"


def test_every_env_var_is_forwarded_to_the_remote_shell() -> None:
    """``env:`` 里定义了却没列进 ``envs:`` = 远端 set -u 下 unbound variable。"""

    for step in _ssh_steps():
        declared = set((step.get("env") or {}).keys())
        forwarded = {name.strip() for name in step["with"]["envs"].split(",") if name.strip()}
        missing = sorted(declared - forwarded)
        assert not missing, (
            f"步骤 {step.get('name')!r} 的 env 里有 {missing}，但 with.envs 没列 —— "
            "远端脚本在 set -u 下会报 unbound variable，部署 job 直接失败"
        )


def test_no_forwarded_name_is_left_undefined() -> None:
    """反向：``envs:`` 列了但 ``env:`` 没定义，同样是 unbound variable。"""

    for step in _ssh_steps():
        declared = set((step.get("env") or {}).keys())
        forwarded = {name.strip() for name in step["with"]["envs"].split(",") if name.strip()}
        undefined = sorted(forwarded - declared)
        assert not undefined, (
            f"步骤 {step.get('name')!r} 的 with.envs 列了 {undefined}，但 env 里没定义"
        )


def test_every_forwarded_env_var_is_actually_used_by_the_remote_script() -> None:
    """传过去了却没人用 = 声明了一半的配置项：改 var 不报错，也不生效。

    上一条守的是 ``env:`` → ``envs:``，漏了会**响亮地**失败（set -u 报
    unbound variable）。这一条守的是再下一段 —— 变量到了远端却没被
    ``set_env`` 写进 .env。那个漏法是**静默**的：部署照常绿，服务照常起，
    只是配置永远停在代码默认值上，没有任何信号。

    踩过一次（2026-09-13 加 AGENT_VOICE_BRAND_SPOKEN_AS 时只加了 Settings
    字段，三处全漏）：线上按默认值念了一整轮，没人知道 var 是摆设。
    """

    for step in _ssh_steps():
        declared = set((step.get("env") or {}).keys())
        script = (step.get("with") or {}).get("script") or ""
        unused = sorted(name for name in declared if not re.search(rf"\${{?{name}\b}}?", script))
        assert not unused, (
            f"步骤 {step.get('name')!r} 的 env 声明了 {unused}，但远端脚本从没引用 —— "
            "配置项传到了机器上却没写进 .env，改 var 会静默失效"
        )
