from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from .config import WECOM_CONFIG_PATH
from .domain import clean_text, load_json_file


@dataclass
class WeComConfig:
    corp_id: str
    corp_secret: str
    agent_id: int
    touser: str
    toparty: str = ""
    totag: str = ""


def load_wecom_config() -> dict[str, Any]:
    default = {
        "corp_id": os.getenv("WECOM_CORP_ID", ""),
        "corp_secret": os.getenv("WECOM_CORP_SECRET", ""),
        "agent_id": int(os.getenv("WECOM_AGENT_ID", "0") or 0),
        "touser": os.getenv("WECOM_TOUSER", ""),
        "toparty": "",
        "totag": "",
    }
    file_cfg = load_json_file(WECOM_CONFIG_PATH, default={})
    merged = dict(default)
    merged.update({k: v for k, v in file_cfg.items() if v not in (None, "")})
    if "agent_id" in merged:
        try:
            merged["agent_id"] = int(merged["agent_id"] or 0)
        except Exception:
            merged["agent_id"] = 0
    return merged


class WeComNotifier:
    def __init__(self, config: dict[str, Any] | None = None):
        raw = config or load_wecom_config()
        self.config = WeComConfig(
            corp_id=clean_text(raw.get("corp_id", "")),
            corp_secret=clean_text(raw.get("corp_secret", "")),
            agent_id=int(raw.get("agent_id") or 0),
            touser=clean_text(raw.get("touser", "")),
            toparty=clean_text(raw.get("toparty", "")),
            totag=clean_text(raw.get("totag", "")),
        )
        self._access_token: str | None = None
        self._access_token_expiry: float = 0.0

    def is_configured(self) -> bool:
        return bool(self.config.corp_id and self.config.corp_secret and self.config.agent_id)

    def _request_json(self, url: str, payload: dict[str, Any] | None = None, method: str = "GET") -> dict[str, Any]:
        data = None
        headers = {"User-Agent": "AegisAlphaMVP/1.0", "Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=data, headers=headers, method=method)
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def get_access_token(self) -> str:
        if self._access_token and time.time() < self._access_token_expiry - 60:
            return self._access_token
        if not self.is_configured():
            raise RuntimeError("WeCom config is incomplete")
        url = (
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
            f"?corpid={quote_plus(self.config.corp_id)}&corpsecret={quote_plus(self.config.corp_secret)}"
        )
        payload = self._request_json(url, method="GET")
        if payload.get("errcode", 0) != 0:
            raise RuntimeError(f"WeCom token error: {payload}")
        token = clean_text(payload.get("access_token", ""))
        expires_in = int(payload.get("expires_in") or 7200)
        self._access_token = token
        self._access_token_expiry = time.time() + max(300, expires_in)
        return token

    def send_markdown(self, content: str, touser: str | None = None) -> dict[str, Any]:
        token = self.get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={quote_plus(token)}"
        payload: dict[str, Any] = {
            "touser": clean_text(touser or self.config.touser),
            "toparty": self.config.toparty,
            "totag": self.config.totag,
            "msgtype": "markdown",
            "agentid": self.config.agent_id,
            "markdown": {"content": content},
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 600,
        }
        payload = {k: v for k, v in payload.items() if v not in ("", None)}
        result = self._request_json(url, payload=payload, method="POST")
        if result.get("errcode", 0) != 0:
            raise RuntimeError(f"WeCom send error: {result}")
        return result
