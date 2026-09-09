from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


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

def keyword_hits(text: str, keywords: Iterable[str]) -> list[str]:
    lowered = text.lower()
    hits: list[str] = []
    for keyword in keywords:
        if keyword.lower() in lowered:
            hits.append(keyword)
    return hits
