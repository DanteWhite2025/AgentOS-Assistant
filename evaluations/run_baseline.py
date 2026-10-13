"""质量层评测 —— 用真实模型跑一遍测题集，输出可人工判读的对比报告。

结构层（tests/test_evaluations.py）能确定性地保证消息结构不坏，但答得好不好
它管不了。这个脚本负责后者：同一批测题在改动前后各跑一次，把 prompt 结构、
工具调用序列和最终回复落成 markdown，人工对比。

**需要真实模型 key，会产生费用，不进 CI。**

用法：

    # 改动前，在基线分支上跑一次
    uv run python -m evaluations.run_baseline --label before --out evaluations/reports

    # 改动后再跑一次
    uv run python -m evaluations.run_baseline --label after --out evaluations/reports

    # 两份报告并排看，或直接 diff
    diff evaluations/reports/before.md evaluations/reports/after.md

模型与 key 走既有配置（AGENT_LLM_* / provider_mode=real），不在这里另开一套。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
from datetime import UTC, datetime

from app.agents.runner import AgentRunner
from app.config import get_settings
from app.container import build_container
from app.runtime.session import Scope, Session
from evaluations.answer_shape import inspect, is_deep_analysis
from evaluations.cases import CASES, CaseOutcome, EvalCase


def _session(case: EvalCase) -> Session:
    now = datetime.now(UTC)
    return Session(
        session_id=f"eval_{case.name}",
        user_id="eval_user",
        modality="text",
        scope=Scope(agents=("avatar",), tools=case.scope_tools),
        state="text_active",
        created_at=now,
        last_activity_at=now,
    )


async def _run_case(runner: AgentRunner, case: EvalCase) -> CaseOutcome:
    outcome = CaseOutcome(case=case)
    try:
        result = await runner.run_turn(
            _session(case),
            case.question,
            f"trace_eval_{case.name}",
            datetime.now(UTC),
            web_search=case.web_search,
        )
    except Exception as exc:
        # 一道题炸了不该中断整轮评测——报告里如实记下，其余测题继续跑。
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome

    outcome.answer = result.text
    outcome.tool_calls = [tool.name for tool in result.executed_tools]
    outcome.blockers = list(result.blockers)
    return outcome


def _shape_cell(answer: str, tool_calls: list[str]) -> tuple[str, str, str, str]:
    """（摘要, 追问标记, 抽取, chips 自包含）四列。空回答不体检——那是别的问题。"""

    if not answer:
        return "—", "—", "—", "—"
    shape = inspect(answer)
    if is_deep_analysis(tool_calls):
        summary = f"{'✅' if shape.summary_ok else '⚠️'} {shape.summary_bullets} 条"
    else:
        # 简单查询场景没有「恰好 3 条」这个要求，只记条数不判优劣。
        summary = f"{shape.summary_bullets} 条（不适用）"
    marker = "✅" if shape.has_marker else "❌ 无标记"
    if shape.leaked_marker:
        extracted = "❌ 标记漏进正文"
    elif shape.questions:
        flag = "✅" if shape.suggestions_ok else "⚠️"
        extracted = f"{flag} {len(shape.questions)} 条"
    else:
        extracted = "—" if not shape.has_marker else "❌ 抽不出"
    # 自包含只在真抽出了 chips 时才有意义：没抽出来是上一列的事，这里再报一次红
    # 只会让两列同时变红，看报告的人分不清是格式问题还是代词问题。
    if not shape.questions:
        self_contained = "—"
    elif shape.dangling_questions:
        self_contained = f"❌ {len(shape.dangling_questions)} 条带代词"
    else:
        self_contained = "✅"
    return summary, marker, extracted, self_contained


def _render(label: str, outcomes: list[CaseOutcome]) -> str:
    lines = [
        f"# 评测报告 · {label}",
        "",
        f"生成时间：{datetime.now(UTC).isoformat()}",
        f"测题数：{len(outcomes)}",
        "",
        "## 汇总",
        "",
        (
            "| 测题 | 期望工具 | 实际工具 | 回复长度 | 摘要 | 追问标记 "
            "| chips 抽取 | chips 自包含 | 状态 |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in outcomes:
        expected = "、".join(item.case.expect_tool_calls) or (
            "（不该调用）" if item.case.expect_no_tool_calls else "—"
        )
        actual = "、".join(item.tool_calls) or "—"
        status = "错误" if item.error else "完成"
        summary, marker, extracted, self_contained = _shape_cell(item.answer, item.tool_calls)
        lines.append(
            f"| {item.case.name} | {expected} | {actual} | {len(item.answer)} "
            f"| {summary} | {marker} | {extracted} | {self_contained} | {status} |"
        )

    lines += ["", "## 逐题详情", ""]
    for item in outcomes:
        lines += [
            f"### {item.case.name}",
            "",
            f"**问题**：{item.case.question}",
            "",
            f"**说明**：{item.case.note}",
            "",
            f"**实际工具调用**：{'、'.join(item.tool_calls) or '无'}",
            "",
        ]
        if item.blockers:
            lines += [f"**blockers**：{'、'.join(item.blockers)}", ""]
        if item.error:
            lines += [f"**错误**：{item.error}", ""]
        if item.answer:
            shape = inspect(item.answer)
            if shape.questions:
                lines += ["**抽出的 chips**：", ""]
                # 带代词的那条直接在行尾点出来。汇总表只给条数，要定位是哪一条
                # 还得回来数——而这一列红了之后要做的第一件事就是看它长什么样。
                dangling = set(shape.dangling_questions)
                lines += [
                    f"{i}. {q}" + ("　← ❌ 指代上文，点开会串人" if q in dangling else "")
                    for i, q in enumerate(shape.questions, 1)
                ]
                lines += [""]
            elif shape.has_marker:
                lines += ["**抽出的 chips**：标记在但抽不出内容", ""]
        lines += ["**回复原文**：", "", "```", item.answer or "（空）", "```", ""]
    return "\n".join(lines)


async def _main() -> int:
    parser = argparse.ArgumentParser(description="用真实模型跑评测集")
    parser.add_argument("--label", required=True, help="报告标签，如 before / after")
    parser.add_argument("--out", default="evaluations/reports", help="报告输出目录")
    parser.add_argument("--only", default="", help="只跑指定测题（逗号分隔的 name）")
    args = parser.parse_args()

    settings = get_settings()
    if settings.provider_mode != "real":
        print("provider_mode 不是 real，跑出来的是 Fake 结果，没有评测意义。")
        print("请设置 AGENT_PROVIDER_MODE=real 与对应的模型 key 后重试。")
        return 2

    wanted = {name.strip() for name in args.only.split(",") if name.strip()}
    cases = [c for c in CASES if not wanted or c.name in wanted]

    container = build_container(settings)
    try:
        outcomes = []
        for case in cases:
            print(f"跑 {case.name} …", flush=True)
            outcomes.append(await _run_case(container.runner, case))
    finally:
        await container.aclose()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"{args.label}.md"
    report.write_text(_render(args.label, outcomes), encoding="utf-8")
    print(f"\n报告已写入 {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
