from datetime import UTC, datetime
from decimal import Decimal

from app.usage.voice_duration import RoomWindow, derive_duration_seconds, reconcile_duration

T = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def test_duration_from_join_and_leave() -> None:
    window = RoomWindow(join_at=T, leave_at=T.replace(second=30))
    assert derive_duration_seconds(window) == Decimal("30.000")


def test_missing_leave_event_yields_none() -> None:
    """只进房没退房（回调丢了）时返回 None，由调用方决定退回客户端口径。"""
    assert derive_duration_seconds(RoomWindow(join_at=T, leave_at=None)) is None


def test_missing_join_event_yields_none() -> None:
    assert derive_duration_seconds(RoomWindow(join_at=None, leave_at=T)) is None


def test_negative_window_yields_none_not_negative_duration() -> None:
    """乱序回调不能算出负时长——那会变成负成本。"""
    window = RoomWindow(join_at=T.replace(second=30), leave_at=T)
    assert derive_duration_seconds(window) is None


def test_server_duration_wins_when_within_tolerance() -> None:
    r = reconcile_duration(Decimal(60), Decimal(62), tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds == Decimal(62)
    assert r.suspicious is False
    assert r.source == "server"


def test_client_underreport_beyond_tolerance_is_flagged() -> None:
    """客户端少报很多 = 疑似漏报（崩溃、被杀进程）。取服务端值并标记。"""
    r = reconcile_duration(Decimal(5), Decimal(120), tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds == Decimal(120)
    assert r.suspicious is True
    assert r.source == "server"


def test_client_overreport_beyond_tolerance_is_also_flagged() -> None:
    """客户端多报同样可疑，只是方向相反；一律以服务端为准。"""
    r = reconcile_duration(Decimal(300), Decimal(60), tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds == Decimal(60)
    assert r.suspicious is True


def test_falls_back_to_client_when_server_unavailable() -> None:
    """兜底关闭或回调缺失时退回客户端口径，行为与接入前一致。"""
    r = reconcile_duration(Decimal(60), None, tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds == Decimal(60)
    assert r.source == "client"
    assert r.suspicious is False


def test_server_duration_without_client_report_is_flagged() -> None:
    """有服务端时长但客户端压根没上报 = 确定的客户端漏报，必须标记。"""
    r = reconcile_duration(None, Decimal(60), tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds == Decimal(60)
    assert r.source == "server"
    assert r.suspicious is True


def test_no_duration_at_all_yields_none() -> None:
    r = reconcile_duration(None, None, tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds is None
    assert r.source == "none"


def test_zero_server_duration_does_not_divide_by_zero() -> None:
    """服务端算出 0 秒（秒开秒挂）时不能拿它做分母。"""
    r = reconcile_duration(Decimal(5), Decimal(0), tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds == Decimal(0)
    assert r.source == "server"
