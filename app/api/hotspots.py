"""每日热点早报 API。

前端首屏用它替换掉呆板的「你想看哪只股票/币」问答模板：主动给出
今日核心行情摘要 + 几张可点击的追问议题卡片（卡片的 prompt 直接喂给
对话接口，即可触发 Agent 调用 ask_investment_expert 工具做深度分析，
见 app.tools.investment_expert_tools）。

缓存全在服务端内存里（30 分钟）。不发 public max-age：陈旧响应一旦被
App / CDN 再缓存 30 分钟，客户端就再也回不来刷新。限流只挡冷启动打 LLM；
日本 nginx 后面 client.host 经常是 127.0.0.1，有旧早报还去占配额会全国 429。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import get_container
from app.api.errors import error_response
from app.container import Container
from app.services.hotspot_service import HotspotDigest, HotspotUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(tags=["hotspots"])

Ctn = Annotated[Container, Depends(get_container)]


def _caller_id(request: Request) -> str:
    """限流身份：只按对端 IP 计（理由同 app.api.news._caller_id）。"""
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


@router.get(
    "/hotspots/daily",
    response_model=HotspotDigest,
    summary="每日热点早报（今日要闻摘要 + 推荐追问议题）",
)
async def get_daily_hotspots(container: Ctn, request: Request, response: Response):
    service = container.hotspot_service
    if service is None:
        return error_response("INTERNAL", "热点早报服务未配置或不可用", 503)

    # 手里有过成功结果（含过期）就别限流，让单飞去刷新；只拦真正的冷启动。
    if service.peek_cached() is None:
        limiter = container.rate_limiter
        if limiter is not None:
            window = container.settings.hotspots_rate_window_seconds
            quota = container.settings.hotspots_rate_limit
            if quota > 0 and not await limiter.allow(
                f"hotspots:daily:{_caller_id(request)}", quota, window
            ):
                return error_response(
                    "RATE_LIMITED",
                    f"热点早报已达配额（{quota} 次 / {window}s），请稍后重试",
                    429,
                    details={"limit": quota, "window_seconds": window},
                )

    try:
        digest = await service.build_daily_digest()
    except HotspotUnavailable:
        logger.warning("热点早报暂不可用")
        return error_response("INTERNAL", "热点早报生成失败，请稍后重试", 503)
    except Exception:
        logger.exception("热点早报生成失败")
        return error_response("INTERNAL", "热点早报生成失败，请稍后重试", 503)

    response.headers["Cache-Control"] = "no-store"
    return digest
