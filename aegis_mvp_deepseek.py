from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "aegis_alpha_mvp.db"
FEED_CONFIG_PATH = BASE_DIR / "aegis_feeds.json"
OUTBOX_DIR = BASE_DIR / "outbox"
WECOM_CONFIG_PATH = BASE_DIR / "aegis_wecom.json"
MODEL_NAME = os.getenv("AEGIS_MODEL_NAME", "deepseek-chat")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/")


@dataclass
class EventClassification:
    event_type: str
    region: str
    importance_level: str
    credibility_level: str
    urgency_level: str
    event_summary: str
    significance_score: float
    matched_keywords: list[str]
    validated_sources: list[str]


@dataclass
class AssetImpact:
    asset_code: str
    direction: str
    duration: str
    rationale: str


@dataclass
class WeComConfig:
    corp_id: str
    corp_secret: str
    agent_id: int
    touser: str
    toparty: str = ""
    totag: str = ""


SOURCE_PROFILE_SEEDS = [
    ("Reuters", "wire", 0.95, "Global wire service"),
    ("Bloomberg", "wire", 0.96, "Global market wire"),
    ("AP", "wire", 0.92, "Newswire"),
    ("Financial Times", "wire", 0.93, "Financial press"),
    ("Wall Street Journal", "wire", 0.93, "Financial press"),
    ("CNBC", "wire", 0.89, "Business media"),
    ("Fed", "official", 0.99, "US central bank"),
    ("ECB", "official", 0.99, "Eurozone central bank"),
    ("PBOC", "official", 0.99, "China central bank"),
    ("OPEC", "official", 0.98, "Oil producer group"),
    ("IMF", "official", 0.98, "International institution"),
    ("World Bank", "official", 0.98, "International institution"),
    ("UN", "official", 0.98, "International institution"),
    ("Ministry of Commerce", "official", 0.95, "Policy source"),
    ("Macro Public Account Feed", "social", 0.68, "Curated public account /公众号 feed"),
    ("Finance KOL Digest", "social", 0.60, "Curated finance influencer feed /财经大V"),
]


DEFAULT_ASSETS = [
    ("000300.SH", "沪深300", "Index", 4000.0, 0.0, "China"),
    ("HSI", "恒生指数", "Index", 18000.0, 0.0, "Hong Kong"),
    ("IXIC", "纳斯达克100", "Index", 16000.0, 0.0, "US Growth"),
    ("GC00Y", "黄金", "Commodity", 2300.0, 0.0, "Safe Haven"),
    ("CL00Y", "WTI原油", "Commodity", 80.0, 0.0, "Energy"),
    ("DXY", "美元指数", "FX", 104.5, 0.0, "USD"),
    ("US10Y", "10年期美债收益率", "Bond", 4.5, 0.0, "Rates"),
]


MARKET_SYMBOL_MAP = {
    "000300.SH": {"source": "Yahoo Finance", "symbol": "000300.SS"},
    "HSI": {"source": "Yahoo Finance", "symbol": "^HSI"},
    "IXIC": {"source": "Yahoo Finance", "symbol": "^IXIC"},
    "GC00Y": {"source": "Yahoo Finance", "symbol": "GC=F"},
    "CL00Y": {"source": "Yahoo Finance", "symbol": "CL=F"},
    "DXY": {"source": "Yahoo Finance", "symbol": "DX-Y.NYB"},
    "US10Y": {"source": "Yahoo Finance", "symbol": "^TNX"},
}


HISTORICAL_CASES = [
    (
        "FED_POLICY",
        "2022 hawkish repricing",
        "美联储持续偏鹰，市场快速上修利率路径。",
        "美债收益率上行，成长股承压，美元走强，黄金阶段性承压。",
        0.92,
    ),
    (
        "MACRO_DATA",
        "2023 inflation re-acceleration",
        "通胀数据超预期，降息预期被推迟。",
        "收益率和美元同步走高，长久期资产回撤，红利和价值相对占优。",
        0.90,
    ),
    (
        "GEO_CONFLICT",
        "2022 Russia-Ukraine shock",
        "地缘冲突升级，能源供给风险抬升。",
        "原油和黄金受益，风险资产回撤，航运和航空承压。",
        0.94,
    ),
    (
        "ENERGY_SHOCK",
        "2023 OPEC supply cuts",
        "原油供给收缩或运输受阻。",
        "油价和能源链条上行，通胀预期被动抬升。",
        0.91,
    ),
    (
        "TRADE_TARIFF",
        "2018 tariff escalation",
        "贸易摩擦升级，出口链预期受损。",
        "半导体、制造、航运与全球风险偏好受到冲击。",
        0.89,
    ),
    (
        "TECH_BREAKTHROUGH",
        "2023 AI capex wave",
        "AI和算力突破刺激产业链资本开支预期。",
        "算力、半导体、软件和相关ETF走强，风格切向成长。",
        0.84,
    ),
    (
        "ELECTION_RISK",
        "2024 election uncertainty",
        "关键选举结果增加政策不确定性。",
        "美元、国债和黄金波动上升，政策敏感资产出现轮动。",
        0.80,
    ),
    (
        "NATURAL_DISASTER",
        "2011 Japan earthquake shock",
        "自然灾害冲击供应链和工业生产。",
        "相关地区制造业、航运和保险板块承压，部分商品波动放大。",
        0.83,
    ),
]


EVENT_LIBRARY: dict[str, dict[str, Any]] = {
    "FED_POLICY": {
        "label": "美联储政策冲击",
        "region": "US",
        "base_score": 88,
        "keywords": [
            "fed",
            "fomc",
            "powell",
            "rate hike",
            "rate cut",
            "interest rate",
            "dot plot",
            "美联储",
            "加息",
            "降息",
            "议息",
            "利率决议",
            "点阵图",
        ],
        "asset_impacts": [
            {"asset_code": "US10Y", "direction": "up", "duration": "intraday", "rationale": "利率路径重定价通常先推高长端收益率"},
            {"asset_code": "DXY", "direction": "up", "duration": "intraday", "rationale": "利率优势带动美元走强"},
            {"asset_code": "IXIC", "direction": "down", "duration": "1-3d", "rationale": "长久期成长股对贴现率最敏感"},
            {"asset_code": "GC00Y", "direction": "down", "duration": "1-3d", "rationale": "实际利率上行会压制黄金"},
            {"asset_code": "000300.SH", "direction": "down", "duration": "1-3d", "rationale": "外部流动性和风险偏好同步承压"},
        ],
        "causal_chain": [
            "政策措辞转向鹰派 -> 市场上修未来利率路径",
            "美债收益率和美元率先反应 -> 全球流动性边际收紧",
            "成长股估值受压 -> 风险资产波动放大",
            "黄金和高贝塔资产短线承压 -> 价值和防御风格相对占优",
        ],
        "risk_warning": "如果市场已经提前定价，初始波动可能在数小时内快速回吐。",
        "watchpoints": ["10年期美债收益率", "DXY", "纳指期货", "黄金"],
    },
    "MACRO_DATA": {
        "label": "宏观数据冲击",
        "region": "Global",
        "base_score": 80,
        "keywords": [
            "cpi",
            "inflation",
            "pce",
            "gdp",
            "jobs",
            "nonfarm",
            "unemployment",
            "通胀",
            "就业",
            "GDP",
            "CPI",
            "PCE",
            "非农",
            "失业率",
            "零售销售",
        ],
        "asset_impacts": [
            {"asset_code": "US10Y", "direction": "up", "duration": "intraday", "rationale": "强数据通常推升收益率并重新定价政策预期"},
            {"asset_code": "DXY", "direction": "up", "duration": "intraday", "rationale": "宏观偏强支撑美元"},
            {"asset_code": "IXIC", "direction": "down", "duration": "1-3d", "rationale": "高估值资产对利率预期高度敏感"},
            {"asset_code": "GC00Y", "direction": "mixed", "duration": "1-3d", "rationale": "通胀与实际利率渠道方向可能冲突"},
        ],
        "causal_chain": [
            "宏观数据偏热 -> 政策宽松预期降温",
            "收益率和美元上行 -> 贴现率抬升",
            "高估值资产承压 -> 风格轮动到防御和价值",
        ],
        "risk_warning": "需要区分一次性数据噪声和趋势性恶化，避免过度交易。",
        "watchpoints": ["美元指数", "美债收益率", "科技股期货", "黄金"],
    },
    "GEO_CONFLICT": {
        "label": "地缘冲突 / 战争升级",
        "region": "Middle East / Europe",
        "base_score": 92,
        "keywords": [
            "war",
            "conflict",
            "strike",
            "missile",
            "sanction",
            "military",
            "battle",
            "地缘",
            "战争",
            "冲突",
            "武装",
            "袭击",
            "制裁",
            "空袭",
            "炮击",
        ],
        "asset_impacts": [
            {"asset_code": "CL00Y", "direction": "up", "duration": "intraday", "rationale": "供应中断风险抬升原油风险溢价"},
            {"asset_code": "GC00Y", "direction": "up", "duration": "intraday", "rationale": "避险需求上升推动黄金"},
            {"asset_code": "IXIC", "direction": "down", "duration": "1-3d", "rationale": "风险偏好回落压制成长资产"},
            {"asset_code": "HSI", "direction": "down", "duration": "1-3d", "rationale": "全球风险资产同步承压"},
        ],
        "causal_chain": [
            "冲突升级 -> 供应链和能源运输风险上升",
            "原油风险溢价抬升 -> 通胀预期抬头",
            "避险资金流向黄金和美元 -> 风险资产承压",
        ],
        "risk_warning": "若冲突未扩散或快速降温，避险溢价会快速回吐。",
        "watchpoints": ["布伦特/WTI油价", "黄金", "美元", "航运与航空板块"],
    },
    "ENERGY_SHOCK": {
        "label": "能源供给冲击",
        "region": "Global",
        "base_score": 86,
        "keywords": [
            "oil",
            "gas",
            "opec",
            "pipeline",
            "refinery",
            "energy",
            "原油",
            "天然气",
            "欧佩克",
            "OPEC",
            "管道",
            "炼厂",
            "能源",
            "供应中断",
        ],
        "asset_impacts": [
            {"asset_code": "CL00Y", "direction": "up", "duration": "intraday", "rationale": "供给收缩直接利多油价"},
            {"asset_code": "GC00Y", "direction": "up", "duration": "1-3d", "rationale": "能源冲击推升通胀和避险需求"},
            {"asset_code": "US10Y", "direction": "up", "duration": "1-3d", "rationale": "通胀预期与期限溢价可能抬升"},
        ],
        "causal_chain": [
            "供给收缩或运输受阻 -> 原油和天然气价格上行",
            "能源成本上升 -> 通胀预期被动抬升",
            "权益市场承压 -> 能源链条和资源股相对受益",
        ],
        "risk_warning": "需要区分一次性扰动和持续性供给缺口。",
        "watchpoints": ["原油期货", "天然气", "能源板块", "航运成本"],
    },
    "TRADE_TARIFF": {
        "label": "贸易 / 关税 / 管制升级",
        "region": "Global",
        "base_score": 84,
        "keywords": [
            "tariff",
            "trade war",
            "export control",
            "restriction",
            "customs",
            "关税",
            "贸易战",
            "出口管制",
            "禁运",
            "制裁清单",
            "反制",
            "配额",
        ],
        "asset_impacts": [
            {"asset_code": "000300.SH", "direction": "down", "duration": "1-4w", "rationale": "出口链和风险偏好承压"},
            {"asset_code": "HSI", "direction": "down", "duration": "1-4w", "rationale": "外需和中国资产风险偏好同步受压"},
            {"asset_code": "IXIC", "direction": "down", "duration": "1-4w", "rationale": "半导体和跨境科技链受扰动"},
            {"asset_code": "DXY", "direction": "up", "duration": "1-3d", "rationale": "避险和政策不确定性往往支撑美元"},
        ],
        "causal_chain": [
            "关税或管制升级 -> 供应链和利润预期受损",
            "企业资本开支与库存决策推迟 -> 风险偏好下降",
            "出口链和科技链条受压 -> 美元和防御资产相对占优",
        ],
        "risk_warning": "若政策只停留在口头威慑而没有落地，市场冲击会明显减弱。",
        "watchpoints": ["出口链", "半导体", "航运", "美元"],
    },
    "TECH_BREAKTHROUGH": {
        "label": "重大科技突破",
        "region": "Global",
        "base_score": 74,
        "keywords": [
            "ai",
            "chip",
            "semiconductor",
            "breakthrough",
            "model",
            "quantum",
            "drug",
            "approval",
            "AI",
            "芯片",
            "半导体",
            "突破",
            "量子",
            "药物",
            "获批",
            "算力",
        ],
        "asset_impacts": [
            {"asset_code": "IXIC", "direction": "up", "duration": "1-4w", "rationale": "科技预期改善有利成长风格"},
            {"asset_code": "000300.SH", "direction": "mixed", "duration": "1-4w", "rationale": "产业链映射不一，需看落地环节"},
            {"asset_code": "HSI", "direction": "mixed", "duration": "1-4w", "rationale": "科技权重提升时可能受益，但节奏分化"},
        ],
        "causal_chain": [
            "技术突破 -> 商业化和资本开支预期上升",
            "行业利润弹性被重估 -> 成长风格和相关产业链受益",
            "若估值已经较高，需要防止利好兑现过快",
        ],
        "risk_warning": "技术新闻不等于业绩落地，需观察订单、融资和收入确认。",
        "watchpoints": ["算力链", "半导体设备", "软件平台", "相关ETF"],
    },
    "ELECTION_RISK": {
        "label": "选举 / 政策不确定性",
        "region": "Global",
        "base_score": 72,
        "keywords": [
            "election",
            "vote",
            "parliament",
            "referendum",
            "poll",
            "选举",
            "投票",
            "议会",
            "公投",
            "民调",
            "执政党",
            "反对党",
        ],
        "asset_impacts": [
            {"asset_code": "DXY", "direction": "up", "duration": "1-3d", "rationale": "不确定性通常提升美元和现金偏好"},
            {"asset_code": "US10Y", "direction": "mixed", "duration": "1-3d", "rationale": "财政与政策预期可能拉扯债券定价"},
            {"asset_code": "GC00Y", "direction": "up", "duration": "1-3d", "rationale": "政策不确定性推高避险需求"},
        ],
        "causal_chain": [
            "选举结果不确定 -> 政策路径难以定价",
            "波动率抬升 -> 防御和避险资产受益",
            "风险资产倾向于等待结果确认后再定价",
        ],
        "risk_warning": "民调和盘口预期会提前消化结果，注意事件前后不同阶段的市场反应。",
        "watchpoints": ["民调变化", "波动率", "美元", "国债"],
    },
    "NATURAL_DISASTER": {
        "label": "地震 / 洪水 / 飓风等自然灾害",
        "region": "Local / Regional",
        "base_score": 66,
        "keywords": [
            "earthquake",
            "flood",
            "hurricane",
            "disaster",
            "typhoon",
            "地震",
            "洪水",
            "飓风",
            "台风",
            "灾害",
            "滑坡",
            "停电",
            "损毁",
        ],
        "asset_impacts": [
            {"asset_code": "CL00Y", "direction": "mixed", "duration": "1-3d", "rationale": "灾害对能源供需的影响取决于地区和基础设施损毁情况"},
            {"asset_code": "GC00Y", "direction": "up", "duration": "intraday", "rationale": "风险事件常触发短线避险需求"},
            {"asset_code": "HSI", "direction": "down", "duration": "1-3d", "rationale": "区域风险偏好回落影响港股和相关供应链"},
        ],
        "causal_chain": [
            "自然灾害 -> 供应链和基础设施受损",
            "局部生产和物流中断 -> 相关行业短线承压",
            "若影响范围扩散，避险需求同步抬升",
        ],
        "risk_warning": "需要判断是否只是局部事件，还是会演化成更大范围的供应链冲击。",
        "watchpoints": ["受灾地区供应链", "保险板块", "黄金", "相关商品"],
    },
    "GENERAL": {
        "label": "一般市场事件",
        "region": "Global",
        "base_score": 52,
        "keywords": [],
        "asset_impacts": [
            {"asset_code": "000300.SH", "direction": "mixed", "duration": "1-3d", "rationale": "缺少明确方向，需要更多验证"},
        ],
        "causal_chain": [
            "信息还不足以形成高置信结论",
            "先等待二次来源和价格确认，再决定是否升级为交易信号",
        ],
        "risk_warning": "请先验证信息真实性，不要过度解读单条新闻。",
        "watchpoints": ["补充来源", "价格反应", "后续二次报道"],
    },
}


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def short_text(value: str, limit: int = 220) -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value
    return value[: max(limit - 3, 0)].rstrip() + "..."


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def load_json_file(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return dict(default or {})


def stable_hash(*parts: str) -> str:
    digest = hashlib.md5()
    digest.update("||".join(clean_text(part) for part in parts).encode("utf-8"))
    return digest.hexdigest()


def direction_label(direction: str) -> str:
    mapping = {
        "up": "偏多 / 上行",
        "down": "偏空 / 下行",
        "mixed": "分歧",
        "neutral": "中性",
    }
    return mapping.get(direction, direction)


def credibility_label(score: float) -> str:
    if score >= 0.85:
        return "HIGH"
    if score >= 0.65:
        return "MEDIUM"
    return "LOW"


def importance_label(score: float) -> str:
    if score >= 85:
        return "S"
    if score >= 70:
        return "A"
    if score >= 55:
        return "B"
    return "C"


def source_type_from_name(source: str) -> str:
    text = clean_text(source).lower()
    if any(token in text for token in ["reuters", "bloomberg", "ap", "ft", "wsj", "cnbc", "nikkei"]):
        return "wire"
    if any(token in text for token in ["fed", "ecb", "pboc", "opec", "imf", "world bank", "un", "ministry"]):
        return "official"
    if any(token in text for token in ["wechat", "公众号", "微博", "x ", "twitter", "kol", "blog", "digest"]):
        return "social"
    if any(token in text for token in ["research", "report", "sell-side", "sellside"]):
        return "research"
    return "market"


def source_credibility_score(source_type: str) -> float:
    return {
        "official": 0.98,
        "wire": 0.93,
        "research": 0.82,
        "market": 0.72,
        "social": 0.58,
    }.get(source_type, 0.65)


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


def keyword_hits(text: str, keywords: Iterable[str]) -> list[str]:
    lowered = text.lower()
    hits: list[str] = []
    for keyword in keywords:
        if keyword.lower() in lowered:
            hits.append(keyword)
    return hits


class AegisPipeline:
    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path)
        self.client = self._build_client()
        self.wecom = WeComNotifier()
        self.init_database()

    def _build_client(self) -> Any:
        if OpenAI is None or not DEEPSEEK_API_KEY:
            return None
        try:
            return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        except Exception as exc:  # pragma: no cover - runtime guard
            logging.warning("DeepSeek client init failed, fallback to rules: %s", exc)
            return None

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row[1] for row in rows}

    def ensure_column(self, conn: sqlite3.Connection, table_name: str, column_sql: str, column_name: str) -> None:
        columns = self.table_columns(conn, table_name)
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")

    def init_database(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS news_raw (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT,
                    source TEXT,
                    source_type TEXT,
                    publish_time TEXT,
                    metadata_json TEXT,
                    hash_md5 TEXT UNIQUE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_profiles (
                    source TEXT PRIMARY KEY,
                    source_type TEXT,
                    credibility_score REAL,
                    notes TEXT,
                    last_seen_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_assets (
                    asset_code TEXT PRIMARY KEY,
                    asset_name TEXT NOT NULL,
                    asset_type TEXT,
                    price_t0 REAL,
                    price_t1 REAL,
                    benchmark_group TEXT,
                    market_source TEXT,
                    market_symbol TEXT,
                    last_market_price REAL,
                    last_market_change_pct REAL,
                    last_market_updated_at TEXT,
                    market_payload_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS historical_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    case_name TEXT NOT NULL,
                    case_summary TEXT,
                    market_pattern TEXT,
                    confidence REAL,
                    UNIQUE(event_type, case_name)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    news_id INTEGER UNIQUE,
                    event_type TEXT,
                    region TEXT,
                    importance_level TEXT,
                    credibility_level TEXT,
                    urgency_level TEXT,
                    event_summary TEXT,
                    significance_score REAL,
                    validated_sources TEXT,
                    FOREIGN KEY(news_id) REFERENCES news_raw(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reasoning_chains (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER UNIQUE,
                    chain_version INTEGER,
                    causal_chain_text TEXT,
                    historical_validation TEXT,
                    asset_impacts TEXT,
                    key_observations TEXT,
                    risk_warning TEXT,
                    model_mode TEXT,
                    created_at TEXT,
                    FOREIGN KEY(event_id) REFERENCES events(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS push_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER UNIQUE,
                    card_title TEXT,
                    card_body_markdown TEXT,
                    card_json TEXT,
                    created_at TEXT,
                    delivery_status TEXT,
                    FOREIGN KEY(event_id) REFERENCES events(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    card_id INTEGER,
                    review_stage TEXT,
                    is_correct INTEGER,
                    error_type TEXT,
                    review_analysis TEXT,
                    actual_market_report TEXT,
                    reviewed_at TEXT,
                    FOREIGN KEY(card_id) REFERENCES push_cards(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    news_id INTEGER,
                    event_id INTEGER,
                    card_id INTEGER,
                    stage_name TEXT,
                    input_json TEXT,
                    output_json TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_card_stage
                ON reviews(card_id, review_stage)
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reasoning_event
                ON reasoning_chains(event_id)
                """
            )

            # Migration helpers for older local databases.
            self.ensure_column(conn, "news_raw", "source_type TEXT", "source_type")
            self.ensure_column(conn, "news_raw", "metadata_json TEXT", "metadata_json")
            self.ensure_column(conn, "market_assets", "benchmark_group TEXT", "benchmark_group")
            self.ensure_column(conn, "market_assets", "market_source TEXT", "market_source")
            self.ensure_column(conn, "market_assets", "market_symbol TEXT", "market_symbol")
            self.ensure_column(conn, "market_assets", "last_market_price REAL", "last_market_price")
            self.ensure_column(conn, "market_assets", "last_market_change_pct REAL", "last_market_change_pct")
            self.ensure_column(conn, "market_assets", "last_market_updated_at TEXT", "last_market_updated_at")
            self.ensure_column(conn, "market_assets", "market_payload_json TEXT", "market_payload_json")
            self.ensure_column(conn, "events", "significance_score REAL DEFAULT 0", "significance_score")
            self.ensure_column(conn, "events", "validated_sources TEXT DEFAULT ''", "validated_sources")
            self.ensure_column(conn, "reasoning_chains", "chain_version INTEGER DEFAULT 1", "chain_version")
            self.ensure_column(conn, "reasoning_chains", "historical_validation TEXT DEFAULT ''", "historical_validation")
            self.ensure_column(conn, "reasoning_chains", "key_observations TEXT DEFAULT ''", "key_observations")
            self.ensure_column(conn, "reasoning_chains", "risk_warning TEXT DEFAULT ''", "risk_warning")
            self.ensure_column(conn, "reasoning_chains", "model_mode TEXT DEFAULT 'rules'", "model_mode")
            self.ensure_column(conn, "reasoning_chains", "created_at TEXT DEFAULT ''", "created_at")
            self.ensure_column(conn, "push_cards", "card_json TEXT DEFAULT ''", "card_json")
            self.ensure_column(conn, "push_cards", "delivery_status TEXT DEFAULT 'queued'", "delivery_status")
            self.ensure_column(conn, "reviews", "actual_market_report TEXT DEFAULT ''", "actual_market_report")
            self.ensure_column(conn, "reviews", "reviewed_at TEXT DEFAULT ''", "reviewed_at")

            self.seed_reference_data(conn)

    def seed_reference_data(self, conn: sqlite3.Connection) -> None:
        conn.executemany(
            """
            INSERT INTO source_profiles (source, source_type, credibility_score, notes, last_seen_at)
            VALUES (?, ?, ?, ?, COALESCE((SELECT last_seen_at FROM source_profiles WHERE source = ?), ''))
            ON CONFLICT(source) DO UPDATE SET
                source_type = excluded.source_type,
                credibility_score = excluded.credibility_score,
                notes = excluded.notes
            """,
            [(src, typ, score, notes, src) for src, typ, score, notes in SOURCE_PROFILE_SEEDS],
        )

        conn.executemany(
            """
            INSERT INTO market_assets (
                asset_code, asset_name, asset_type, price_t0, price_t1, benchmark_group,
                market_source, market_symbol, last_market_price, last_market_change_pct,
                last_market_updated_at, market_payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_code) DO UPDATE SET
                asset_name = excluded.asset_name,
                asset_type = excluded.asset_type,
                price_t0 = COALESCE(market_assets.price_t0, excluded.price_t0),
                price_t1 = COALESCE(market_assets.price_t1, excluded.price_t1),
                benchmark_group = excluded.benchmark_group,
                market_source = COALESCE(market_assets.market_source, excluded.market_source),
                market_symbol = COALESCE(market_assets.market_symbol, excluded.market_symbol)
            """,
            [
                (
                    asset_code,
                    asset_name,
                    asset_type,
                    price_t0,
                    price_t1,
                    benchmark_group,
                    MARKET_SYMBOL_MAP.get(asset_code, {}).get("source", "Static Seed"),
                    MARKET_SYMBOL_MAP.get(asset_code, {}).get("symbol", ""),
                    None,
                    None,
                    None,
                    None,
                )
                for asset_code, asset_name, asset_type, price_t0, price_t1, benchmark_group in DEFAULT_ASSETS
            ],
        )

        conn.executemany(
            """
            INSERT INTO historical_cases (event_type, case_name, case_summary, market_pattern, confidence)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(event_type, case_name) DO UPDATE SET
                case_summary = excluded.case_summary,
                market_pattern = excluded.market_pattern,
                confidence = excluded.confidence
            """,
            HISTORICAL_CASES,
        )

    def record_workflow_log(
        self,
        stage_name: str,
        input_payload: dict[str, Any] | None = None,
        output_payload: dict[str, Any] | None = None,
        news_id: int | None = None,
        event_id: int | None = None,
        card_id: int | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_logs (news_id, event_id, card_id, stage_name, input_json, output_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    news_id,
                    event_id,
                    card_id,
                    stage_name,
                    to_json(input_payload or {}),
                    to_json(output_payload or {}),
                    now_str(),
                ),
            )

    def ensure_source_profile(self, conn: sqlite3.Connection, source: str) -> tuple[str, float]:
        source = clean_text(source) or "Unknown"
        source_type = source_type_from_name(source)
        score = source_credibility_score(source_type)
        known_sources = {row[0] for row in SOURCE_PROFILE_SEEDS}
        if source not in known_sources and source_type in {"market", "social"}:
            source_type = "social"
            score = 0.58
        conn.execute(
            """
            INSERT INTO source_profiles (source, source_type, credibility_score, notes, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                source_type = excluded.source_type,
                credibility_score = excluded.credibility_score,
                last_seen_at = excluded.last_seen_at
            """,
            (source, source_type, score, "Auto-discovered source profile", now_str()),
        )
        return source_type, score

    def collector_agent(
        self,
        title: str,
        content: str,
        source: str,
        publish_time: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        title = clean_text(title)
        content = clean_text(content)
        source = clean_text(source)
        publish_time = clean_text(publish_time) or now_str()
        metadata = metadata or {}
        hash_md5 = stable_hash(title, content, source, publish_time)

        with self.connect() as conn:
            source_type, _ = self.ensure_source_profile(conn, source)

            existing = conn.execute("SELECT id FROM news_raw WHERE hash_md5 = ?", (hash_md5,)).fetchone()
            if existing:
                logging.info("[Collector] Duplicate news skipped: id=%s", existing["id"])
                return int(existing["id"])

            cursor = conn.execute(
                """
                INSERT INTO news_raw (title, content, source, source_type, publish_time, metadata_json, hash_md5)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title, content, source, source_type, publish_time, to_json(metadata), hash_md5),
            )
            news_id = int(cursor.lastrowid)

        self.record_workflow_log(
            "collector",
            {"title": title, "source": source, "publish_time": publish_time},
            {"news_id": news_id},
            news_id=news_id,
        )
        logging.info("[Collector] News ingested: id=%s", news_id)
        return news_id

    def import_news_file(self, file_path: str | Path) -> list[int]:
        path = Path(file_path)
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        records = payload if isinstance(payload, list) else payload.get("items", [])
        news_ids: list[int] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            news_id = self.collector_agent(
                item.get("title", ""),
                item.get("content", ""),
                item.get("source", "Imported Feed"),
                item.get("publish_time"),
                item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
            )
            if news_id is not None:
                news_ids.append(news_id)
        return news_ids

    def classify_news_locally(self, title: str, content: str, source: str) -> EventClassification:
        text = f"{title}\n{content}\n{source}"
        source_type = source_type_from_name(source)
        source_score = source_credibility_score(source_type)

        best_event_type = "GENERAL"
        best_rule = EVENT_LIBRARY["GENERAL"]
        best_score = -1.0
        best_hits: list[str] = []

        for event_type, rule in EVENT_LIBRARY.items():
            if event_type == "GENERAL":
                continue
            hits = keyword_hits(text, rule["keywords"])
            if not hits:
                continue
            score = rule["base_score"] + len(hits) * 6 + source_score * 10
            if any(token in text.lower() for token in ["breaking", "urgent", "快讯", "突发", "紧急", "alert"]):
                score += 6
            if score > best_score:
                best_score = score
                best_event_type = event_type
                best_rule = rule
                best_hits = hits

        if best_score < 0:
            best_score = best_rule["base_score"] + source_score * 10

        urgency = "URGENT" if best_event_type in {"GEO_CONFLICT", "ENERGY_SHOCK", "FED_POLICY"} and best_score >= 80 else "NORMAL"
        if any(token in text.lower() for token in ["breaking", "urgent", "快讯", "突发", "紧急", "alert"]):
            urgency = "URGENT"

        credibility = credibility_label((source_score + min(len(best_hits), 3) * 0.08))
        importance = importance_label(best_score)
        summary = short_text(f"{title}. {content}", 260)
        validated_sources = [source] if source else []
        region = best_rule["region"]

        return EventClassification(
            event_type=best_event_type,
            region=region,
            importance_level=importance,
            credibility_level=credibility,
            urgency_level=urgency,
            event_summary=summary,
            significance_score=float(round(min(best_score, 100.0), 2)),
            matched_keywords=best_hits,
            validated_sources=validated_sources,
        )

    def maybe_llm_json(self, system_prompt: str, user_prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
        if self.client is None:
            return fallback
        try:
            response = self.client.chat.completions.create(
                model=MODEL_NAME,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            payload = json.loads(response.choices[0].message.content or "{}")
            if isinstance(payload, dict):
                merged = dict(fallback)
                merged.update(payload)
                return merged
        except Exception as exc:  # pragma: no cover - runtime guard
            logging.warning("LLM call failed, fallback to rules: %s", exc)
        return fallback

    def verifier_agent(self, news_id: int) -> int:
        with self.connect() as conn:
            news = conn.execute(
                "SELECT id, title, content, source, source_type, publish_time, metadata_json FROM news_raw WHERE id = ?",
                (news_id,),
            ).fetchone()
            if news is None:
                raise ValueError(f"news_id not found: {news_id}")

            existing = conn.execute("SELECT id FROM events WHERE news_id = ?", (news_id,)).fetchone()
            if existing:
                logging.info("[Verifier] Event already exists: id=%s", existing["id"])
                return int(existing["id"])

            classification = self.classify_news_locally(news["title"], news["content"] or "", news["source"] or "")
            fallback = asdict(classification)
            payload = self.maybe_llm_json(
                system_prompt=(
                    "You are a financial event classifier. Return JSON with keys "
                    "event_type, region, importance_level, credibility_level, urgency_level, "
                    "event_summary, significance_score, matched_keywords, validated_sources."
                ),
                user_prompt=(
                    f"Title: {news['title']}\n"
                    f"Content: {news['content']}\n"
                    f"Source: {news['source']}\n"
                    f"Source type: {news['source_type']}\n"
                    f"Local classification: {to_json(fallback)}"
                ),
                fallback=fallback,
            )

            event_type = str(payload.get("event_type") or fallback["event_type"])
            if event_type not in EVENT_LIBRARY:
                event_type = "GENERAL"

            region = str(payload.get("region") or fallback["region"])
            importance_level = str(payload.get("importance_level") or fallback["importance_level"])
            credibility_level = str(payload.get("credibility_level") or fallback["credibility_level"])
            urgency_level = str(payload.get("urgency_level") or fallback["urgency_level"])
            event_summary = clean_text(payload.get("event_summary") or fallback["event_summary"])
            significance_score = float(payload.get("significance_score") or fallback["significance_score"])
            matched_keywords = payload.get("matched_keywords") or fallback["matched_keywords"]
            if not isinstance(matched_keywords, list):
                matched_keywords = list(fallback["matched_keywords"])
            validated_sources = payload.get("validated_sources") or fallback["validated_sources"]
            if not isinstance(validated_sources, list):
                validated_sources = list(fallback["validated_sources"])

            cursor = conn.execute(
                """
                INSERT INTO events (
                    news_id, event_type, region, importance_level, credibility_level,
                    urgency_level, event_summary, significance_score, validated_sources
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    news_id,
                    event_type,
                    region,
                    importance_level,
                    credibility_level,
                    urgency_level,
                    event_summary,
                    significance_score,
                    to_json(validated_sources),
                ),
            )
            event_id = int(cursor.lastrowid)

        self.record_workflow_log(
            "verifier",
            {"news_id": news_id, "title": news["title"]},
            {"event_id": event_id, "classification": payload},
            news_id=news_id,
            event_id=event_id,
        )
        logging.info("[Verifier] Event classified: id=%s type=%s", event_id, event_type)
        return event_id

    def history_agent(self, event_type: str) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT case_name, case_summary, market_pattern, confidence
                FROM historical_cases
                WHERE event_type = ?
                ORDER BY confidence DESC, id ASC
                LIMIT 3
                """,
                (event_type,),
            ).fetchall()

        if not rows:
            return {
                "historical_validation": "No direct historical case is seeded yet.",
                "reference_count": 0,
                "confidence": 0.4,
            }

        lines = []
        avg_conf = 0.0
        for row in rows:
            avg_conf += float(row["confidence"] or 0)
            lines.append(
                f"- {row['case_name']}: {row['case_summary']} | Market pattern: {row['market_pattern']}"
            )
        avg_conf /= max(len(rows), 1)
        return {
            "historical_validation": "\n".join(lines),
            "reference_count": len(rows),
            "confidence": round(avg_conf, 3),
        }

    def asset_name_map(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            "SELECT asset_code, asset_name, asset_type, price_t0, price_t1, benchmark_group FROM market_assets"
        ).fetchall()
        return {row["asset_code"]: dict(row) for row in rows}

    def build_analysis_bundle(self, event_row: sqlite3.Row, news_row: sqlite3.Row) -> dict[str, Any]:
        event_type = event_row["event_type"] or "GENERAL"
        library = EVENT_LIBRARY.get(event_type, EVENT_LIBRARY["GENERAL"])
        history = self.history_agent(event_type)

        asset_impacts: list[AssetImpact] = [
            AssetImpact(
                asset_code=item["asset_code"],
                direction=item["direction"],
                duration=item["duration"],
                rationale=item["rationale"],
            )
            for item in library["asset_impacts"]
        ]
        if event_row["importance_level"] == "C" and event_type != "GENERAL":
            asset_impacts = asset_impacts[:3]

        card_title = f"{library['label']} | {short_text(news_row['title'], 36)}"
        causal_lines = library["causal_chain"]
        if history["reference_count"]:
            causal_lines = causal_lines + ["Historical validation: " + short_text(history["historical_validation"], 160)]

        key_observations = library["watchpoints"][:]
        for impact in asset_impacts:
            key_observations.append(f"{impact.asset_code}: {direction_label(impact.direction)}")

        bundle = {
            "card_title": card_title,
            "causal_chain_text": "\n".join(f"{idx + 1}. {line}" for idx, line in enumerate(causal_lines)),
            "historical_validation": history["historical_validation"],
            "asset_impacts": [asdict(item) for item in asset_impacts],
            "key_observations": key_observations[:8],
            "risk_warning": library["risk_warning"],
            "watchpoints": library["watchpoints"],
            "market_summary": short_text(news_row["content"] or news_row["title"], 280),
            "review_schedule": ["24H", "7D"],
            "event_label": library["label"],
        }
        return bundle

    def maybe_llm_analysis(self, event_row: sqlite3.Row, news_row: sqlite3.Row, fallback: dict[str, Any]) -> dict[str, Any]:
        if self.client is None:
            return fallback
        system_prompt = (
            "You are a financial analyst agent. Return JSON only with keys "
            "card_title, causal_chain_text, historical_validation, asset_impacts, "
            "key_observations, risk_warning, watchpoints, market_summary, review_schedule, event_label."
        )
        user_prompt = (
            f"Event: {to_json(dict(event_row))}\n"
            f"News: title={news_row['title']}\ncontent={news_row['content']}\nsource={news_row['source']}\n"
            f"Fallback bundle: {to_json(fallback)}"
        )
        return self.maybe_llm_json(system_prompt, user_prompt, fallback)

    def render_card_markdown(
        self,
        event_row: sqlite3.Row,
        news_row: sqlite3.Row,
        bundle: dict[str, Any],
    ) -> str:
        impacts = bundle["asset_impacts"]
        table_lines = [
            "| Asset | Direction | Horizon | Rationale |",
            "| --- | --- | --- | --- |",
        ]
        for item in impacts:
            asset_name = self.get_asset_name(item["asset_code"])
            table_lines.append(
                f"| {asset_name} ({item['asset_code']}) | {direction_label(item['direction'])} | {item['duration']} | {item['rationale']} |"
            )

        watchpoints = "\n".join(f"- {item}" for item in bundle["watchpoints"])
        key_observations = "\n".join(f"- {item}" for item in bundle["key_observations"])

        card = f"""# {bundle['card_title']}

- Event level: {event_row['importance_level']}
- Credibility: {event_row['credibility_level']}
- Urgency: {event_row['urgency_level']}
- Event type: {bundle['event_label']}

## Event Summary
{news_row['title']}

{bundle['market_summary']}

## Causal Chain
{bundle['causal_chain_text']}

## Asset Map
{chr(10).join(table_lines)}

## Historical Validation
{bundle['historical_validation'] or 'No direct case available yet.'}

## Key Observations
{key_observations}

## Watchpoints
{watchpoints}

## Risk Warning
{bundle['risk_warning']}

## Review Loop
- 24H review
- 7D review
"""
        return card

    def get_asset_name(self, asset_code: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT asset_name FROM market_assets WHERE asset_code = ?",
                (asset_code,),
            ).fetchone()
            return row["asset_name"] if row else asset_code

    def analyst_agent(self, event_id: int) -> int:
        with self.connect() as conn:
            event_row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if event_row is None:
                raise ValueError(f"event_id not found: {event_id}")

            existing = conn.execute("SELECT id FROM push_cards WHERE event_id = ?", (event_id,)).fetchone()
            if existing:
                logging.info("[Analyst] Card already exists: id=%s", existing["id"])
                return int(existing["id"])

            news_row = conn.execute(
                "SELECT * FROM news_raw WHERE id = ?",
                (event_row["news_id"],),
            ).fetchone()
            if news_row is None:
                raise ValueError(f"news row missing for event_id: {event_id}")

            bundle = self.build_analysis_bundle(event_row, news_row)
            bundle = self.maybe_llm_analysis(event_row, news_row, bundle)
            card_markdown = self.render_card_markdown(event_row, news_row, bundle)

            cursor = conn.execute(
                """
                INSERT INTO reasoning_chains (
                    event_id, chain_version, causal_chain_text, historical_validation,
                    asset_impacts, key_observations, risk_warning, model_mode, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    chain_version = excluded.chain_version,
                    causal_chain_text = excluded.causal_chain_text,
                    historical_validation = excluded.historical_validation,
                    asset_impacts = excluded.asset_impacts,
                    key_observations = excluded.key_observations,
                    risk_warning = excluded.risk_warning,
                    model_mode = excluded.model_mode,
                    created_at = excluded.created_at
                """,
                (
                    event_id,
                    1,
                    bundle["causal_chain_text"],
                    bundle["historical_validation"],
                    to_json(bundle["asset_impacts"]),
                    to_json(bundle["key_observations"]),
                    bundle["risk_warning"],
                    "llm" if self.client else "rules",
                    now_str(),
                ),
            )

            card_payload = {
                "event": dict(event_row),
                "news": dict(news_row),
                "bundle": bundle,
            }
            card_cursor = conn.execute(
                """
                INSERT INTO push_cards (event_id, card_title, card_body_markdown, card_json, created_at, delivery_status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    bundle["card_title"],
                    card_markdown,
                    to_json(card_payload),
                    now_str(),
                    "queued",
                ),
            )
            card_id = int(card_cursor.lastrowid)

        self.record_workflow_log(
            "analyst",
            {"event_id": event_id},
            {"card_id": card_id, "bundle": bundle},
            event_id=event_id,
            card_id=card_id,
        )
        logging.info("[Analyst] Card created: id=%s", card_id)
        return card_id

    def simulate_market_prices_for_card(self, card_id: int) -> dict[str, float]:
        with self.connect() as conn:
            card = conn.execute(
                """
                SELECT p.card_json, e.id AS event_id
                FROM push_cards p
                JOIN events e ON e.id = p.event_id
                WHERE p.id = ?
                """,
                (card_id,),
            ).fetchone()
            if card is None:
                raise ValueError(f"card_id not found: {card_id}")
            card_payload = json.loads(card["card_json"] or "{}")
            impacts = card_payload.get("bundle", {}).get("asset_impacts", [])
            assets = self.asset_name_map(conn)

        simulated: dict[str, float] = {}
        for impact in impacts:
            code = impact["asset_code"]
            asset = assets.get(code)
            if not asset:
                continue
            base_price = float(asset["price_t0"] or 0)
            if base_price <= 0:
                continue
            scale = 0.02 if asset["asset_type"] != "Commodity" else 0.03
            if impact["direction"] == "up":
                simulated[code] = round(base_price * (1 + scale), 4)
            elif impact["direction"] == "down":
                simulated[code] = round(base_price * (1 - scale), 4)
            elif impact["direction"] == "mixed":
                simulated[code] = round(base_price * (1 + scale / 3), 4)
            else:
                simulated[code] = round(base_price, 4)
        return simulated

    def review_agent(
        self,
        card_id: int,
        market_prices: dict[str, float] | None = None,
        review_stage: str = "24H",
        auto_simulate: bool = False,
    ) -> tuple[int, str, dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.id AS card_id, p.card_title, p.card_body_markdown, p.card_json,
                       e.event_summary, e.event_type, e.importance_level, e.credibility_level,
                       e.id AS event_id
                FROM push_cards p
                JOIN events e ON p.event_id = e.id
                WHERE p.id = ?
                """,
                (card_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"card_id not found: {card_id}")

            payload = json.loads(row["card_json"] or "{}")
            impacts = payload.get("bundle", {}).get("asset_impacts", [])
            assets = self.asset_name_map(conn)

            if market_prices is None and auto_simulate:
                market_prices = self.simulate_market_prices_for_card(card_id)
            market_prices = market_prices or {}

            review_lines: list[str] = []
            all_correct = True
            missing_assets: list[str] = []
            mismatched_assets: list[str] = []

            for item in impacts:
                code = item["asset_code"]
                asset = assets.get(code)
                if not asset:
                    continue
                price_t0 = float(asset["price_t0"] or 0)
                price_t1 = market_prices.get(code, float(asset["price_t1"] or 0))

                if price_t1 <= 0:
                    missing_assets.append(code)
                    continue

                conn.execute("UPDATE market_assets SET price_t1 = ? WHERE asset_code = ?", (price_t1, code))

                predicted = item["direction"]
                actual = "up" if price_t1 > price_t0 else "down" if price_t1 < price_t0 else "neutral"
                if predicted == "mixed":
                    is_correct = actual != "neutral"
                elif predicted == "up":
                    is_correct = actual == "up"
                elif predicted == "down":
                    is_correct = actual == "down"
                else:
                    is_correct = actual == "neutral"

                if not is_correct:
                    all_correct = False
                    mismatched_assets.append(code)

                review_lines.append(
                    f"{asset['asset_name']} ({code}): predicted={direction_label(predicted)}, actual={direction_label(actual)}, "
                    f"{price_t0:.4f} -> {price_t1:.4f}"
                )

            if missing_assets:
                review_lines.append(f"Missing market data: {', '.join(missing_assets)}")

            if all_correct and not missing_assets:
                error_type = "NO_ERROR"
                review_analysis = (
                    "The causal chain matched the observed move for the tracked assets. "
                    "This strengthens the current rule set for the same event family."
                )
            elif mismatched_assets:
                if row["event_type"] in {"FED_POLICY", "MACRO_DATA"}:
                    error_type = "PRICED_IN_OR_TOO_EARLY"
                    review_analysis = (
                        "The direction partly failed, which often means the market had already priced in the event "
                        "or another macro driver dominated the session."
                    )
                else:
                    error_type = "WRONG_DIRECTION"
                    review_analysis = (
                        "The direction did not match the follow-through. Check whether the event was over-weighted, "
                        "or whether a stronger cross-asset variable dominated the tape."
                    )
            else:
                error_type = "MISSING_DATA"
                review_analysis = "There was insufficient market data to make a reliable verdict."

            review_report = "\n".join(review_lines)
            conn.execute(
                """
                INSERT INTO reviews (
                    card_id, review_stage, is_correct, error_type, review_analysis,
                    actual_market_report, reviewed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_id, review_stage) DO UPDATE SET
                    is_correct = excluded.is_correct,
                    error_type = excluded.error_type,
                    review_analysis = excluded.review_analysis,
                    actual_market_report = excluded.actual_market_report,
                    reviewed_at = excluded.reviewed_at
                """,
                (
                    card_id,
                    review_stage,
                    1 if all_correct and not missing_assets else 0,
                    error_type,
                    review_analysis,
                    review_report,
                    now_str(),
                ),
            )

        self.record_workflow_log(
            "review",
            {"card_id": card_id, "market_prices": market_prices or {}, "review_stage": review_stage},
            {"review_report": review_report, "error_type": error_type},
            card_id=card_id,
        )
        logging.info("[Review] Completed: card_id=%s stage=%s", card_id, review_stage)
        return card_id, review_report, {"error_type": error_type, "review_analysis": review_analysis}

    def batch_process_pending_news(self, limit: int = 20) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT n.id
                FROM news_raw n
                LEFT JOIN events e ON e.news_id = n.id
                WHERE e.id IS NULL
                ORDER BY n.id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        event_ids: list[int] = []
        for row in rows:
            event_ids.append(self.verifier_agent(int(row["id"])))
        return event_ids

    def recent_cards(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.card_title, p.card_body_markdown, p.created_at, p.delivery_status,
                       e.event_type, e.importance_level, e.credibility_level, e.urgency_level,
                       n.title AS news_title, n.source AS news_source
                FROM push_cards p
                JOIN events e ON p.event_id = e.id
                JOIN news_raw n ON e.news_id = n.id
                ORDER BY p.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_reviews(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.id, r.card_id, r.review_stage, r.is_correct, r.error_type, r.review_analysis,
                       r.actual_market_report, r.reviewed_at,
                       p.card_title
                FROM reviews r
                JOIN push_cards p ON p.id = r.card_id
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            news_count = conn.execute("SELECT COUNT(*) AS c FROM news_raw").fetchone()["c"]
            event_count = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            card_count = conn.execute("SELECT COUNT(*) AS c FROM push_cards").fetchone()["c"]
            review_count = conn.execute("SELECT COUNT(*) AS c FROM reviews").fetchone()["c"]
            event_types = conn.execute(
                """
                SELECT event_type, COUNT(*) AS cnt
                FROM events
                GROUP BY event_type
                ORDER BY cnt DESC, event_type ASC
                """
            ).fetchall()
        return {
            "news_count": news_count,
            "event_count": event_count,
            "card_count": card_count,
            "review_count": review_count,
            "event_type_counts": [dict(row) for row in event_types],
        }

    def daily_digest_markdown(self, limit: int = 8) -> str:
        cards = self.recent_cards(limit)
        stats = self.stats()
        market_rows = self.market_overview(limit=7)
        lines = [
            f"# 24H Event Digest",
            "",
            f"- News: {stats['news_count']}",
            f"- Events: {stats['event_count']}",
            f"- Cards: {stats['card_count']}",
            f"- Reviews: {stats['review_count']}",
            "",
            "## Market Snapshot",
        ]
        for row in market_rows:
            change_text = row.get("last_market_change_pct")
            if change_text is None:
                change_label = "n/a"
            else:
                change_label = f"{change_text:.2f}%"
            lines.append(
                f"- {row['asset_code']} | {row['asset_name']} | {row.get('last_market_price') or row.get('price_t1') or 'n/a'} | {change_label} | {row.get('market_symbol') or 'static'}"
            )
        lines.extend(
            [
                "",
                "## Recent Cards",
            ]
        )
        for card in cards:
            lines.extend(
                [
                    f"## {card['card_title']}",
                    f"- Event type: {card['event_type']}",
                    f"- Importance: {card['importance_level']}",
                    f"- Credibility: {card['credibility_level']}",
                    f"- Source: {card['news_source']}",
                    "",
                    short_text(card["card_body_markdown"], 500),
                    "",
                ]
            )
        if not cards:
            lines.append("No cards yet.")
        return "\n".join(lines)

    def demo_news_batch(self) -> list[dict[str, str]]:
        return [
            {
                "title": "中东关键海峡突发武装冲突，原油运输短线受阻",
                "content": "据多家主流媒体与航运跟踪消息，中东关键海峡周边发生武装冲突，部分油轮暂停通行，市场担忧原油供应中断风险抬升。",
                "source": "Reuters",
            },
            {
                "title": "美联储官员释放偏鹰信号，暗示通胀粘性仍然存在",
                "content": "最新表态显示，美联储对通胀回落节奏保持谨慎，市场开始重新定价未来降息路径。",
                "source": "Fed",
            },
            {
                "title": "AI算力芯片取得重大突破，产业链或迎来新一轮资本开支",
                "content": "多家研究机构和公号指出，新一代AI芯片在能效与吞吐量上取得显著突破，相关服务器和软件生态或同步受益。",
                "source": "Macro Public Account Feed",
            },
        ]

    def run_demo(self) -> dict[str, Any]:
        created_news: list[int] = []
        created_events: list[int] = []
        created_cards: list[int] = []
        reviews: list[dict[str, Any]] = []

        for item in self.demo_news_batch():
            news_id = self.collector_agent(item["title"], item["content"], item["source"])
            if news_id is None:
                continue
            created_news.append(news_id)
            event_id = self.verifier_agent(news_id)
            created_events.append(event_id)
            card_id = self.analyst_agent(event_id)
            created_cards.append(card_id)
            simulated_prices = self.simulate_market_prices_for_card(card_id)
            _, report, review_payload = self.review_agent(card_id, simulated_prices, review_stage="24H")
            reviews.append({"card_id": card_id, "report": report, **review_payload})

        return {
            "news_ids": created_news,
            "event_ids": created_events,
            "card_ids": created_cards,
            "reviews": reviews,
            "digest": self.daily_digest_markdown(),
        }

    def export_cards_markdown(self, limit: int = 20) -> str:
        cards = self.recent_cards(limit)
        return "\n\n".join(card["card_body_markdown"] for card in cards)

    def process_single_news(
        self,
        title: str,
        content: str,
        source: str,
        review_with_simulation: bool = False,
    ) -> dict[str, Any]:
        news_id = self.collector_agent(title=title, content=content, source=source)
        if news_id is None:
            return {"status": "duplicate"}
        event_id = self.verifier_agent(news_id)
        card_id = self.analyst_agent(event_id)
        review_payload = None
        review_report = None
        if review_with_simulation:
            simulated_prices = self.simulate_market_prices_for_card(card_id)
            _, review_report, review_payload = self.review_agent(card_id, simulated_prices, review_stage="24H")
        return {
            "news_id": news_id,
            "event_id": event_id,
            "card_id": card_id,
            "review_report": review_report,
            "review": review_payload,
        }

    def default_feed_config(self) -> dict[str, Any]:
        return {
            "feeds": [
                {
                    "name": "GDELT Global Conflict",
                    "type": "gdelt",
                    "query": "(war OR conflict OR attack OR missile OR bombing OR sanctions)",
                    "source": "GDELT",
                    "topic_hint": "GEO_CONFLICT",
                },
                {
                    "name": "GDELT Macro Policy",
                    "type": "gdelt",
                    "query": "(Federal Reserve OR FOMC OR Powell OR ECB OR CPI OR inflation OR PCE OR jobs OR nonfarm)",
                    "source": "GDELT",
                    "topic_hint": "MACRO_DATA",
                },
                {
                    "name": "GDELT Trade Tariff",
                    "type": "gdelt",
                    "query": "(tariff OR export control OR trade war OR sanctions OR customs)",
                    "source": "GDELT",
                    "topic_hint": "TRADE_TARIFF",
                },
                {
                    "name": "GDELT Energy Shock",
                    "type": "gdelt",
                    "query": "(oil OR gas OR OPEC OR pipeline OR refinery OR energy supply)",
                    "source": "GDELT",
                    "topic_hint": "ENERGY_SHOCK",
                },
                {
                    "name": "GDELT Tech Breakthrough",
                    "type": "gdelt",
                    "query": "(AI OR semiconductor OR chip OR breakthrough OR quantum OR drug approval)",
                    "source": "GDELT",
                    "topic_hint": "TECH_BREAKTHROUGH",
                },
                {
                    "name": "GDELT Election Risk",
                    "type": "gdelt",
                    "query": "(election OR poll OR vote OR referendum OR parliament)",
                    "source": "GDELT",
                    "topic_hint": "ELECTION_RISK",
                },
                {
                    "name": "GDELT Natural Disaster",
                    "type": "gdelt",
                    "query": "(earthquake OR flood OR hurricane OR typhoon OR disaster)",
                    "source": "GDELT",
                    "topic_hint": "NATURAL_DISASTER",
                },
                {
                    "name": "Reuters World",
                    "type": "rss",
                    "url": "https://www.reutersagency.com/feed/?best-topics=world-news&post_type=best",
                    "source": "Reuters",
                    "topic_hint": "GEO_CONFLICT",
                },
                {
                    "name": "Reuters Markets",
                    "type": "rss",
                    "url": "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
                    "source": "Reuters",
                    "topic_hint": "MACRO_DATA",
                },
                {
                    "name": "Federal Reserve",
                    "type": "rss",
                    "url": "https://www.federalreserve.gov/feeds/press_all.xml",
                    "source": "Fed",
                    "topic_hint": "FED_POLICY",
                },
                {
                    "name": "ECB",
                    "type": "rss",
                    "url": "https://www.ecb.europa.eu/rss/press.html",
                    "source": "ECB",
                    "topic_hint": "FED_POLICY",
                },
                {
                    "name": "OPEC",
                    "type": "rss",
                    "url": "https://www.opec.org/opec_web/en/press_room/28.htm?rss=1",
                    "source": "OPEC",
                    "topic_hint": "ENERGY_SHOCK",
                },
                {
                    "name": "World Bank",
                    "type": "rss",
                    "url": "https://www.worldbank.org/en/news/all?format=rss",
                    "source": "World Bank",
                    "topic_hint": "MACRO_DATA",
                },
            ]
        }

    def write_feed_template(self, output_path: str | Path = FEED_CONFIG_PATH) -> Path:
        path = Path(output_path)
        path.write_text(json.dumps(self.default_feed_config(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_feed_config(self, config_path: str | Path | None = None) -> dict[str, Any]:
        path = Path(config_path) if config_path else FEED_CONFIG_PATH
        if not path.exists():
            return self.default_feed_config()
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict) and isinstance(payload.get("feeds"), list):
            return payload
        return self.default_feed_config()

    def _read_url(self, url: str, timeout: int = 20) -> bytes:
        req = Request(url, headers={"User-Agent": "AegisAlphaMVP/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _local_tag(self, tag: str) -> str:
        return tag.split("}", 1)[-1].lower()

    def _node_text(self, node: Any, names: Iterable[str]) -> str:
        target_names = {name.lower() for name in names}
        for child in list(node):
            if self._local_tag(child.tag) in target_names and (child.text or "").strip():
                return clean_text(child.text)
        return ""

    def fetch_rss_feed(self, feed: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
        url = clean_text(feed.get("url", ""))
        if not url:
            return []

        try:
            raw = self._read_url(url)
        except (URLError, HTTPError, TimeoutError, ValueError) as exc:
            logging.warning("[Feed] Failed to fetch %s: %s", url, exc)
            return []

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            logging.warning("[Feed] Failed to parse %s: %s", url, exc)
            return []

        channel = root.find("channel")
        if channel is not None:
            item_nodes = channel.findall("item")
        else:
            item_nodes = root.findall("{http://www.w3.org/2005/Atom}entry") or root.findall("entry")

        records: list[dict[str, Any]] = []
        for node in item_nodes[:limit]:
            title = self._node_text(node, ["title"]) or "Untitled"
            content = (
                self._node_text(node, ["description", "summary", "content", "encoded"])
                or self._node_text(node, ["subtitle"])
                or title
            )
            published = self._node_text(node, ["pubdate", "published", "updated", "dc:date"]) or now_str()
            link = ""
            for child in list(node):
                if self._local_tag(child.tag) == "link":
                    if child.attrib.get("href"):
                        link = child.attrib["href"]
                    elif child.text:
                        link = clean_text(child.text)
                    break
            records.append(
                {
                    "title": title,
                    "content": content,
                    "source": feed.get("source") or feed.get("name") or url,
                    "publish_time": published,
                    "metadata": {
                        "feed_name": feed.get("name", ""),
                        "feed_url": url,
                        "link": link,
                        "topic_hint": feed.get("topic_hint", ""),
                        "raw_source_type": feed.get("type", "rss"),
                    },
                }
            )
        return records

    def fetch_json_feed(self, feed: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
        payload: Any = {}
        path = clean_text(feed.get("url", ""))
        if path:
            try:
                if path.startswith("http://") or path.startswith("https://"):
                    raw = self._read_url(path)
                    payload = json.loads(raw.decode("utf-8", errors="replace"))
                else:
                    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            except Exception as exc:
                logging.warning("[Feed] Failed to load json feed %s: %s", path, exc)
                payload = {}

        items = payload if isinstance(payload, list) else payload.get("items", [])
        records: list[dict[str, Any]] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            records.append(
                {
                    "title": clean_text(item.get("title", "")) or "Untitled",
                    "content": clean_text(item.get("content", "")) or clean_text(item.get("summary", "")),
                    "source": feed.get("source") or feed.get("name") or "JSON Feed",
                    "publish_time": clean_text(item.get("publish_time", "")) or now_str(),
                    "metadata": {
                        "feed_name": feed.get("name", ""),
                        "feed_url": path,
                        "topic_hint": feed.get("topic_hint", ""),
                        "raw_source_type": "json",
                    },
                }
            )
        return records

    def fetch_gdelt_feed(self, feed: dict[str, Any], limit: int = 20, timespan: str = "24h") -> list[dict[str, Any]]:
        query = clean_text(feed.get("query", ""))
        if not query:
            hint = clean_text(feed.get("topic_hint", "GENERAL"))
            query = {
                "GEO_CONFLICT": "(war OR conflict OR attack OR missile OR sanctions)",
                "MACRO_DATA": "(Federal Reserve OR ECB OR CPI OR inflation OR PCE OR jobs OR nonfarm)",
                "FED_POLICY": "(Federal Reserve OR FOMC OR Powell OR rate hike OR rate cut)",
                "ENERGY_SHOCK": "(oil OR gas OR OPEC OR pipeline OR refinery)",
                "TRADE_TARIFF": "(tariff OR export control OR sanctions OR trade war)",
                "TECH_BREAKTHROUGH": "(AI OR semiconductor OR chip OR quantum OR drug approval)",
                "ELECTION_RISK": "(election OR poll OR vote OR referendum)",
                "NATURAL_DISASTER": "(earthquake OR flood OR hurricane OR typhoon)",
            }.get(hint, hint)

        api_url = (
            "https://api.gdeltproject.org/api/v2/doc/doc?"
            f"query={quote_plus(query)}"
            f"&mode=ArtList&format=json&sort=HybridRel&maxrecords={int(limit)}"
            f"&timespan={quote_plus(timespan)}"
        )

        try:
            raw = self._read_url(api_url, timeout=25)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            logging.warning("[GDELT] Failed to fetch %s: %s", query, exc)
            return []

        articles = []
        if isinstance(payload, dict):
            for key in ("articles", "results", "artList", "data"):
                if isinstance(payload.get(key), list):
                    articles = payload[key]
                    break

        records: list[dict[str, Any]] = []
        for item in articles[:limit]:
            if not isinstance(item, dict):
                continue
            title = clean_text(item.get("title") or item.get("seendate") or "Untitled")
            content = clean_text(
                item.get("summary")
                or item.get("snippet")
                or item.get("excerpt")
                or item.get("description")
                or title
            )
            source_name = clean_text(feed.get("source") or item.get("domain") or item.get("source") or "GDELT")
            publish_time = clean_text(item.get("seendate") or item.get("datetime") or item.get("published") or now_str())
            metadata = {
                "feed_name": feed.get("name", ""),
                "feed_url": api_url,
                "topic_hint": feed.get("topic_hint", ""),
                "query": query,
                "gdelt_url": item.get("url", ""),
                "domain": item.get("domain", ""),
                "source_country": item.get("sourceCountry", ""),
                "raw_source_type": "gdelt",
            }
            records.append(
                {
                    "title": title,
                    "content": content,
                    "source": source_name,
                    "publish_time": publish_time,
                    "metadata": metadata,
                }
            )
        return records

    def fetch_yahoo_quote(self, symbol: str) -> dict[str, Any]:
        symbol = clean_text(symbol)
        if not symbol:
            return {}

        api_url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote_plus(symbol)}"
            "?interval=1d&range=5d&includePrePost=false&events=div%2Csplits"
        )
        try:
            raw = self._read_url(api_url, timeout=20)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            logging.warning("[Market] Failed to fetch quote %s: %s", symbol, exc)
            return {}

        result = (((payload or {}).get("chart") or {}).get("result") or [])
        if not result:
            return {}
        result = result[0]
        meta = result.get("meta", {}) if isinstance(result, dict) else {}
        indicators = result.get("indicators", {}) if isinstance(result, dict) else {}
        quote = (indicators.get("quote") or [{}])[0]
        closes = quote.get("close") or []

        last_price = next((value for value in reversed(closes) if value is not None), None)
        prev_price = next((value for value in reversed(closes[:-1]) if value is not None), None)
        if prev_price is None:
            prev_price = meta.get("previousClose")

        change_pct = None
        if last_price not in (None, 0) and prev_price not in (None, 0):
            try:
                change_pct = round((float(last_price) - float(prev_price)) / float(prev_price) * 100.0, 4)
            except Exception:
                change_pct = None

        return {
            "symbol": symbol,
            "source": "Yahoo Finance",
            "last_price": float(last_price) if last_price is not None else None,
            "previous_close": float(prev_price) if prev_price is not None else None,
            "change_pct": change_pct,
            "currency": meta.get("currency", ""),
            "exchange_name": meta.get("exchangeName", ""),
            "regular_market_time": meta.get("regularMarketTime", 0),
            "short_name": meta.get("shortName", ""),
            "long_name": meta.get("longName", ""),
            "timezone": meta.get("timezone", ""),
            "instrument_type": meta.get("instrumentType", ""),
            "raw": payload,
        }

    def refresh_market_data(self) -> dict[str, Any]:
        updated: list[dict[str, Any]] = []
        errors: list[str] = []

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT asset_code, asset_name, market_symbol, market_source, price_t0, price_t1
                FROM market_assets
                ORDER BY asset_code ASC
                """
            ).fetchall()

            for row in rows:
                asset_code = row["asset_code"]
                symbol = clean_text(row["market_symbol"] or MARKET_SYMBOL_MAP.get(asset_code, {}).get("symbol", ""))
                source = clean_text(row["market_source"] or MARKET_SYMBOL_MAP.get(asset_code, {}).get("source", "Static Seed"))
                if not symbol:
                    continue

                quote = self.fetch_yahoo_quote(symbol)
                if not quote:
                    errors.append(asset_code)
                    continue

                observed_price = quote.get("last_price")
                change_pct = quote.get("change_pct")
                if observed_price is None:
                    errors.append(asset_code)
                    continue

                conn.execute(
                    """
                    UPDATE market_assets
                    SET market_source = ?,
                        market_symbol = ?,
                        last_market_price = ?,
                        last_market_change_pct = ?,
                        last_market_updated_at = ?,
                        market_payload_json = ?,
                        price_t1 = ?
                    WHERE asset_code = ?
                    """,
                    (
                        source,
                        symbol,
                        observed_price,
                        change_pct,
                        now_str(),
                        to_json(quote),
                        observed_price,
                        asset_code,
                    ),
                )
                updated.append(
                    {
                        "asset_code": asset_code,
                        "symbol": symbol,
                        "source": source,
                        "last_price": observed_price,
                        "previous_close": quote.get("previous_close"),
                        "change_pct": change_pct,
                        "name": row["asset_name"],
                    }
                )

                conn.execute(
                    """
                    INSERT INTO workflow_logs (stage_name, input_json, output_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "market_refresh",
                        to_json({"asset_code": asset_code, "symbol": symbol}),
                        to_json({"quote": quote}),
                        now_str(),
                    ),
                )

        result = {"updated": updated, "errors": errors}
        self.record_workflow_log("refresh_market_data", {}, result)
        return result

    def market_overview(self, limit: int = 7) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT asset_code, asset_name, asset_type, price_t0, price_t1, benchmark_group,
                       market_source, market_symbol, last_market_price, last_market_change_pct,
                       last_market_updated_at
                FROM market_assets
                ORDER BY asset_code ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def build_wecom_digest_markdown(self, limit: int = 8) -> str:
        cards = self.recent_cards(limit)
        market_rows = self.market_overview(limit=7)
        stats_data = self.stats()

        lines = [
            f"# Aegis Alpha 24H 重点摘要",
            "",
            f"- 新闻数：{stats_data['news_count']}",
            f"- 事件数：{stats_data['event_count']}",
            f"- 卡片数：{stats_data['card_count']}",
            f"- 复盘数：{stats_data['review_count']}",
            "",
            "## 市场快照",
        ]
        for row in market_rows:
            price = row.get("last_market_price") or row.get("price_t1") or "n/a"
            change = row.get("last_market_change_pct")
            change_text = "n/a" if change is None else f"{float(change):.2f}%"
            lines.append(f"- {row['asset_code']} {row['asset_name']} {price} {change_text}")

        lines.append("")
        lines.append("## 重点卡片")
        for card in cards:
            lines.extend(
                [
                    f"### {card['card_title']}",
                    f"- 事件类型：{card['event_type']}",
                    f"- 重要度：{card['importance_level']} / 可信度：{card['credibility_level']} / 紧急度：{card['urgency_level']}",
                    f"- 来源：{card['news_source']}",
                    "",
                    short_text(card["card_body_markdown"], 480),
                    "",
                ]
            )
        if not cards:
            lines.append("No cards yet.")
        return "\n".join(lines)

    def push_wecom_markdown(self, content: str) -> dict[str, Any]:
        if not self.wecom.is_configured():
            raise RuntimeError("WeCom is not configured. Create aegis_wecom.json or set WECOM_* env vars.")
        result = self.wecom.send_markdown(content)
        self.record_workflow_log("wecom_push", {"type": "markdown"}, result)
        return result

    def push_daily_digest(self, limit: int = 8) -> dict[str, Any]:
        markdown = self.build_wecom_digest_markdown(limit=limit)
        return self.push_wecom_markdown(markdown)

    def push_card(self, card_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.card_title, p.card_body_markdown, e.event_type, e.importance_level,
                       e.credibility_level, e.urgency_level, n.source AS news_source
                FROM push_cards p
                JOIN events e ON p.event_id = e.id
                JOIN news_raw n ON e.news_id = n.id
                WHERE p.id = ?
                """,
                (card_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"card_id not found: {card_id}")
        markdown = "\n".join(
            [
                f"# {row['card_title']}",
                f"- 事件类型：{row['event_type']}",
                f"- 重要度：{row['importance_level']} / 可信度：{row['credibility_level']} / 紧急度：{row['urgency_level']}",
                f"- 来源：{row['news_source']}",
                "",
                row["card_body_markdown"] or "",
            ]
        )
        return self.push_wecom_markdown(markdown)

    def push_urgent_events(self, limit: int = 5) -> dict[str, Any]:
        events = self.latest_urgent_events(limit=limit)
        if not events:
            return {"status": "no_urgent_events"}
        lines = ["# Aegis Alpha 紧急事件提醒", ""]
        for item in events:
            lines.extend(
                [
                    f"## {item['news_title']}",
                    f"- 事件类型：{item['event_type']}",
                    f"- 重要度：{item['importance_level']} / 紧急度：{item['urgency_level']} / 可信度：{item['credibility_level']}",
                    f"- 地区：{item['region']}",
                    "",
                    short_text(item["event_summary"], 260),
                    "",
                ]
            )
        return self.push_wecom_markdown("\n".join(lines))

    def sync_news_feeds(
        self,
        config_path: str | Path | None = None,
        limit_per_feed: int = 10,
        process_immediately: bool = True,
    ) -> dict[str, Any]:
        config = self.load_feed_config(config_path)
        feeds = config.get("feeds", [])
        collected: list[int] = []
        skipped = 0
        errors: list[str] = []

        for feed in feeds:
            if not isinstance(feed, dict):
                continue
            feed_type = clean_text(feed.get("type", "rss")).lower()
            try:
                if feed_type == "json":
                    records = self.fetch_json_feed(feed, limit=limit_per_feed)
                elif feed_type == "gdelt":
                    records = self.fetch_gdelt_feed(feed, limit=limit_per_feed)
                else:
                    records = self.fetch_rss_feed(feed, limit=limit_per_feed)
            except Exception as exc:  # pragma: no cover - safety net
                errors.append(f"{feed.get('name', 'unknown')}: {exc}")
                continue

            for record in records:
                news_id = self.collector_agent(
                    title=record["title"],
                    content=record["content"],
                    source=record["source"],
                    publish_time=record.get("publish_time"),
                    metadata=record.get("metadata", {}),
                )
                if news_id is None:
                    skipped += 1
                else:
                    collected.append(news_id)

        processed_events: list[int] = []
        created_cards: list[int] = []
        if process_immediately and collected:
            for news_id in collected:
                try:
                    event_id = self.verifier_agent(news_id)
                    processed_events.append(event_id)
                    with self.connect() as conn:
                        event_row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
                    if event_row and event_row["importance_level"] in {"S", "A"}:
                        card_id = self.analyst_agent(event_id)
                        created_cards.append(card_id)
                except Exception as exc:  # pragma: no cover - operational guard
                    errors.append(f"news_id {news_id}: {exc}")

        result = {
            "fetched": len(collected),
            "skipped": skipped,
            "processed_events": len(processed_events),
            "created_cards": len(created_cards),
            "errors": errors,
        }
        self.record_workflow_log("sync_feeds", {"config_path": str(config_path or FEED_CONFIG_PATH)}, result)
        return result

    def latest_urgent_events(self, limit: int = 5) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.id, e.event_type, e.region, e.importance_level, e.credibility_level,
                       e.urgency_level, e.event_summary, e.significance_score, e.validated_sources,
                       n.title AS news_title, n.source AS news_source, n.publish_time
                FROM events e
                JOIN news_raw n ON e.news_id = n.id
                WHERE e.importance_level IN ('S', 'A')
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_digest_file(self, output_dir: str | Path = OUTBOX_DIR, limit: int = 8) -> Path:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"daily_digest_{stamp}.md"
        path.write_text(self.daily_digest_markdown(limit=limit), encoding="utf-8")
        return path

    def run_loop(
        self,
        config_path: str | Path | None = None,
        base_interval_minutes: int = 120,
        urgent_interval_minutes: int = 15,
        limit_per_feed: int = 10,
        one_shot: bool = False,
        max_cycles: int | None = None,
        write_digest: bool = True,
    ) -> list[dict[str, Any]]:
        cycles: list[dict[str, Any]] = []
        cycle_no = 0
        while True:
            cycle_no += 1
            sync_result = self.sync_news_feeds(
                config_path=config_path,
                limit_per_feed=limit_per_feed,
                process_immediately=True,
            )
            market_result = self.refresh_market_data()
            urgent_events = self.latest_urgent_events(limit=5)
            digest_path = self.export_digest_file(limit=8) if write_digest else None
            cycle_summary = {
                "cycle": cycle_no,
                "sync_result": sync_result,
                "market_result": market_result,
                "urgent_events": urgent_events,
                "digest_path": str(digest_path) if digest_path else "",
                "next_interval_minutes": urgent_interval_minutes if urgent_events else base_interval_minutes,
                "timestamp": now_str(),
            }
            cycles.append(cycle_summary)
            self.record_workflow_log("run_loop", {"cycle": cycle_no}, cycle_summary)

            if one_shot or (max_cycles is not None and cycle_no >= max_cycles):
                break

            sleep_minutes = urgent_interval_minutes if urgent_events else base_interval_minutes
            time.sleep(max(1, sleep_minutes) * 60)

        return cycles


def parse_price_updates(items: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in items:
        if "=" not in item:
            continue
        code, price = item.split("=", 1)
        try:
            result[code.strip()] = float(price.strip())
        except ValueError:
            continue
    return result


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aegis Alpha MVP event-driven investment agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init-db", help="Initialize or migrate the local database")

    demo = sub.add_parser("demo", help="Run the offline demo batch")
    demo.add_argument("--export", action="store_true", help="Print the digest markdown after processing")

    ingest = sub.add_parser("ingest", help="Ingest a single news item")
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--content", required=True)
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--review", action="store_true", help="Simulate a review after analysis")

    batch = sub.add_parser("import-json", help="Import a JSON feed file")
    batch.add_argument("--path", required=True)
    batch.add_argument("--review", action="store_true")

    digest = sub.add_parser("digest", help="Print a markdown digest for recent cards")
    digest.add_argument("--limit", type=int, default=8)

    review = sub.add_parser("review", help="Review an existing card")
    review.add_argument("--card-id", type=int, required=True)
    review.add_argument("--stage", default="24H")
    review.add_argument("--price", action="append", default=[], help="Asset price override like CL00Y=86.5")
    review.add_argument("--simulate", action="store_true", help="Generate a synthetic market path from the card")

    export = sub.add_parser("export", help="Export recent cards to markdown")
    export.add_argument("--limit", type=int, default=20)
    export.add_argument("--output", default="")

    pending = sub.add_parser("process-pending", help="Process news that has not been classified yet")
    pending.add_argument("--limit", type=int, default=20)

    feeds_template = sub.add_parser("feeds-template", help="Write a starter news feed config")
    feeds_template.add_argument("--output", default=str(FEED_CONFIG_PATH))

    wecom_template = sub.add_parser("wecom-template", help="Write a starter WeCom config")
    wecom_template.add_argument("--output", default=str(WECOM_CONFIG_PATH))

    sync = sub.add_parser("sync-feeds", help="Fetch configured feeds and process new items")
    sync.add_argument("--config", default=str(FEED_CONFIG_PATH))
    sync.add_argument("--limit-per-feed", type=int, default=10)
    sync.add_argument("--no-immediate-process", action="store_true")

    market = sub.add_parser("refresh-market", help="Refresh market quotes from public data sources")

    sync_all = sub.add_parser("sync-all", help="Refresh market quotes, fetch feeds, and write a digest")
    sync_all.add_argument("--config", default=str(FEED_CONFIG_PATH))
    sync_all.add_argument("--limit-per-feed", type=int, default=10)
    sync_all.add_argument("--no-digest", action="store_true")
    sync_all.add_argument("--push-wecom", action="store_true")

    push_digest = sub.add_parser("push-digest", help="Push the daily digest to WeCom")
    push_digest.add_argument("--limit", type=int, default=8)

    push_card = sub.add_parser("push-card", help="Push a single card to WeCom")
    push_card.add_argument("--card-id", type=int, required=True)

    push_urgent = sub.add_parser("push-urgent", help="Push urgent events to WeCom")
    push_urgent.add_argument("--limit", type=int, default=5)

    loop = sub.add_parser("run-loop", help="Run the scheduled fetch/analyze loop")
    loop.add_argument("--config", default=str(FEED_CONFIG_PATH))
    loop.add_argument("--base-interval-minutes", type=int, default=120)
    loop.add_argument("--urgent-interval-minutes", type=int, default=15)
    loop.add_argument("--limit-per-feed", type=int, default=10)
    loop.add_argument("--once", action="store_true")
    loop.add_argument("--cycles", type=int, default=None)
    loop.add_argument("--no-digest", action="store_true")
    loop.add_argument("--push-wecom", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli()
    args = parser.parse_args(argv)
    pipeline = AegisPipeline()

    command = args.command or "demo"

    if command == "init-db":
        logging.info("Database initialized at %s", DB_PATH)
        return 0

    if command == "demo":
        result = pipeline.run_demo()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.export:
            print("\n" + pipeline.daily_digest_markdown())
        return 0

    if command == "ingest":
        result = pipeline.process_single_news(args.title, args.content, args.source, review_with_simulation=args.review)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "import-json":
        news_ids = pipeline.import_news_file(args.path)
        processed: list[dict[str, Any]] = []
        for news_id in news_ids:
            event_id = pipeline.verifier_agent(news_id)
            card_id = pipeline.analyst_agent(event_id)
            payload = {"news_id": news_id, "event_id": event_id, "card_id": card_id}
            if args.review:
                simulated_prices = pipeline.simulate_market_prices_for_card(card_id)
                _, review_report, review_payload = pipeline.review_agent(card_id, simulated_prices, review_stage="24H")
                payload["review_report"] = review_report
                payload["review"] = review_payload
            processed.append(payload)
        print(json.dumps(processed, ensure_ascii=False, indent=2))
        return 0

    if command == "digest":
        print(pipeline.daily_digest_markdown(limit=args.limit))
        return 0

    if command == "review":
        prices = parse_price_updates(args.price)
        market_prices = prices if prices else None
        if args.simulate:
            market_prices = pipeline.simulate_market_prices_for_card(args.card_id)
        card_id, report, payload = pipeline.review_agent(
            args.card_id,
            market_prices=market_prices,
            review_stage=args.stage,
            auto_simulate=args.simulate and market_prices is None,
        )
        print(json.dumps({"card_id": card_id, "report": report, **payload}, ensure_ascii=False, indent=2))
        return 0

    if command == "export":
        markdown = pipeline.export_cards_markdown(limit=args.limit)
        if args.output:
            Path(args.output).write_text(markdown, encoding="utf-8")
            print(f"Exported to {args.output}")
        else:
            print(markdown)
        return 0

    if command == "process-pending":
        event_ids = pipeline.batch_process_pending_news(limit=args.limit)
        print(json.dumps({"event_ids": event_ids}, ensure_ascii=False, indent=2))
        return 0

    if command == "feeds-template":
        path = pipeline.write_feed_template(args.output)
        print(json.dumps({"output": str(path)}, ensure_ascii=False, indent=2))
        return 0

    if command == "wecom-template":
        template = {
            "corp_id": "",
            "corp_secret": "",
            "agent_id": 1000002,
            "touser": "ZhangYiTao",
            "toparty": "",
            "totag": "",
        }
        path = Path(args.output)
        path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(path)}, ensure_ascii=False, indent=2))
        return 0

    if command == "sync-feeds":
        result = pipeline.sync_news_feeds(
            config_path=args.config,
            limit_per_feed=args.limit_per_feed,
            process_immediately=not args.no_immediate_process,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "refresh-market":
        result = pipeline.refresh_market_data()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "push-digest":
        result = pipeline.push_daily_digest(limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "push-card":
        result = pipeline.push_card(args.card_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "push-urgent":
        result = pipeline.push_urgent_events(limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "sync-all":
        market_result = pipeline.refresh_market_data()
        feed_result = pipeline.sync_news_feeds(
            config_path=args.config,
            limit_per_feed=args.limit_per_feed,
            process_immediately=True,
        )
        digest_path = "" if args.no_digest else str(pipeline.export_digest_file())
        wecom_result = None
        if args.push_wecom:
            wecom_result = pipeline.push_daily_digest(limit=8)
        result = {
            "market_result": market_result,
            "feed_result": feed_result,
            "digest_path": digest_path,
            "wecom_result": wecom_result,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "run-loop":
        result = pipeline.run_loop(
            config_path=args.config,
            base_interval_minutes=args.base_interval_minutes,
            urgent_interval_minutes=args.urgent_interval_minutes,
            limit_per_feed=args.limit_per_feed,
            one_shot=args.once,
            max_cycles=args.cycles,
            write_digest=not args.no_digest,
        )
        if args.push_wecom:
            try:
                result.append({"wecom_push": pipeline.push_daily_digest(limit=8)})
            except Exception as exc:
                result.append({"wecom_push_error": str(exc)})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
