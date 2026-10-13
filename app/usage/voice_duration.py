"""语音通话时长的服务端兜底推导（spec §7.1 / D1）。

数据面直连，Runtime 看不到音频流，时长本来只能靠客户端挂断时上报。客户端崩溃、
被系统杀进程、断网未重试，这通电话就变成零成本——做单用户抄表账单时这类漏报
直接落在具体用户头上，不是宏观平均能摊掉的误差。

本模块只做纯推导。**事件类型字面量不在这里硬编码**：火山回调的 EventType 取值
在本仓库代码里没有枚举（app/voice/volc_callbacks.py 是 schema-agnostic 的宽容
解析），必须先在 staging 用真实回调确认。取值由配置注入，配置为空时兜底不生效，
时长仍只取客户端上报——写一个猜测的魔法值进来会静默算出错的时长，而错的账单比
没有账单更糟。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

DurationSource = Literal["server", "client", "none"]

#: 时长保留 3 位小数，与 usage_ledger.duration_seconds 的 Numeric(scale=3) 对齐。
_DURATION_EXPONENT = Decimal("0.001")


@dataclass(frozen=True)
class RoomWindow:
    """一次通话的进房/退房时间。任一端缺失都算推不出时长。"""

    join_at: datetime | None
    leave_at: datetime | None


@dataclass(frozen=True)
class DurationReconciliation:
    duration_seconds: Decimal | None
    source: DurationSource
    #: 客户端与服务端差得过多，或客户端压根没上报。取服务端值，同时告警。
    suspicious: bool


def derive_duration_seconds(window: RoomWindow) -> Decimal | None:
    """由进房/退房时间算时长。任一端缺失或时间乱序都返回 None。"""
    if window.join_at is None or window.leave_at is None:
        return None
    delta = (window.leave_at - window.join_at).total_seconds()
    if delta < 0:
        # 乱序回调算出负时长会变成负成本，宁可当作拿不到。
        return None
    return Decimal(str(delta)).quantize(_DURATION_EXPONENT)


def reconcile_duration(
    client_seconds: Decimal | None,
    server_seconds: Decimal | None,
    *,
    tolerance_ratio: Decimal,
) -> DurationReconciliation:
    """服务端时长优先，客户端上报作交叉校验（spec §7.1）。

    服务端拿不到时退回客户端口径，行为与接入兜底之前一致。
    """
    if server_seconds is None:
        if client_seconds is None:
            return DurationReconciliation(None, "none", False)
        return DurationReconciliation(client_seconds, "client", False)

    if client_seconds is None:
        # 有服务端时长却没有客户端上报：确定的客户端漏报。
        return DurationReconciliation(server_seconds, "server", True)

    if server_seconds <= 0:
        # 秒开秒挂，服务端时长为 0，不能拿它做分母。
        return DurationReconciliation(server_seconds, "server", False)

    drift = abs(server_seconds - client_seconds) / server_seconds
    return DurationReconciliation(server_seconds, "server", drift > tolerance_ratio)
