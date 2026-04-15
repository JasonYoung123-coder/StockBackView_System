"""飞书机器人通知模块 —— 通过飞书自建应用向指定私聊发送交易信号通知。

认证流程：app_id + app_secret → tenant_access_token（2 小时有效，自动缓存续期）
消息格式：飞书 POST 富文本，支持标题 + 多行内容 + emoji 图标
"""

from __future__ import annotations

import json
import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

_token_lock = threading.Lock()
_cached_token: str = ""
_token_expires_at: float = 0.0


def _get_tenant_token(app_id: str, app_secret: str) -> str:
    """获取 tenant_access_token，带缓存（提前 5 分钟续期）。"""
    global _cached_token, _token_expires_at

    with _token_lock:
        if _cached_token and time.time() < _token_expires_at - 300:
            return _cached_token

    resp = requests.post(
        _TOKEN_URL,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(f"飞书认证失败: {data.get('msg', '未知错误')}")

    token = data["tenant_access_token"]
    expire = int(data.get("expire", 7200))

    with _token_lock:
        _cached_token = token
        _token_expires_at = time.time() + expire

    return token


def _build_post_content(title: str, sections: list[list[dict]]) -> dict:
    """构建飞书 POST 富文本消息体。

    sections 是一个二维列表，每个元素是一行的内容（飞书 POST 格式）。
    每行可包含多个元素（text / a 等标签）。
    """
    return {
        "zh_cn": {
            "title": title,
            "content": sections,
        }
    }


def build_text(text: str, bold: bool = False) -> dict:
    """构建飞书文本元素。"""
    node = {"tag": "text", "text": text}
    if bold:
        node["style"] = ["bold"]
    return node


def build_line(text: str, bold: bool = False) -> list[dict]:
    """构建单行文本。"""
    return [build_text(text, bold=bold)]


def build_divider() -> list[dict]:
    """构建分割线（用横线字符模拟）。"""
    return [build_text("─" * 28)]


def send_feishu_notification(title: str, sections: list[list[dict]]) -> None:
    """向飞书指定私聊发送富文本通知。

    Parameters
    ----------
    title : 消息标题
    sections : 富文本行列表，每行是 [{"tag": "text", "text": "..."}] 格式。
               也可传入 list[str]（向后兼容），自动转换为富文本格式。
    """
    from app.core.config import get_settings

    settings = get_settings()

    if not settings.feishu_enabled:
        return

    app_id = settings.feishu_app_id
    app_secret = settings.feishu_app_secret
    chat_id = settings.feishu_chat_id

    if not app_id or not app_secret or not chat_id:
        logger.debug("飞书配置不完整（app_id/app_secret/chat_id），跳过通知")
        return

    converted: list[list[dict]] = []
    for item in sections:
        if isinstance(item, str):
            converted.append([{"tag": "text", "text": item}])
        elif isinstance(item, list):
            converted.append(item)
        else:
            converted.append([{"tag": "text", "text": str(item)}])

    try:
        token = _get_tenant_token(app_id, app_secret)

        post_content = _build_post_content(title, converted)
        body = {
            "receive_id": chat_id,
            "msg_type": "post",
            "content": json.dumps(post_content, ensure_ascii=False),
        }

        resp = requests.post(
            f"{_SEND_MSG_URL}?receive_id_type=chat_id",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("code") != 0:
            logger.warning("飞书消息发送失败: %s", result.get("msg", "未知错误"))
        else:
            logger.info("飞书通知已发送: %s", title)

    except Exception as exc:
        logger.warning("飞书通知发送异常（不影响交易）: %s", exc)
