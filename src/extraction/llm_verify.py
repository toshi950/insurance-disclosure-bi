"""LLM verification/completion layer — the second half of the hybrid
extraction design (see handoff notes, "ループ搭載AIツールの設計原則").

Invoked only when deterministic extraction (`table_search.py`) is missing
or implausible for a field. Never treats the model's answer as ground
truth without a bounded retry and an explicit low-confidence path back to
the caller (never silently guesses forever).

Model tiering: cheap/fast model first, escalate once to a stronger model
only if the cheap pass itself reports low confidence or fails to parse.
No unbounded loop — at most one escalation, ever, per field.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

CHEAP_MODEL = "claude-haiku-4-5-20251001"
STRONG_MODEL = "claude-sonnet-5"

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set (expected in .env, loaded via python-dotenv)"
            )
        # This machine's installed `Brotli` package (1.0.9) is incompatible
        # with the anthropic SDK's vendored httpx2 BrotliDecoder (calls
        # `.process(data, output_buffer_limit=...)`, a kwarg that package
        # version doesn't accept -> every response read raises
        # `TypeError: process() takes no keyword arguments`, surfaced by
        # the SDK as a generic APIConnectionError). Rather than touching
        # the shared/global environment's Brotli install, just ask the
        # server not to use brotli at all.
        _client = anthropic.Anthropic(
            api_key=api_key,
            default_headers={"Accept-Encoding": "gzip, deflate"},
        )
    return _client


@dataclass
class LLMResult:
    value: Optional[float]
    confidence: str  # "high" | "low"
    evidence_snippet: Optional[str]
    unit_note: Optional[str]
    model_used: str
    raw_response: str


PROMPT_TEMPLATE = """あなたは日本の保険会社の開示資料から特定の科目の数値を抽出する担当者です。
以下は開示資料からの抜粋（プレーンテキスト）です。

--- 抜粋ここから ---
{excerpt}
--- 抜粋ここまで ---

科目名「{label}」について、**開示されている年度のうち最も新しい年度**の数値を抽出してください。
複数年度が並んでいる表の場合、通常は右側の列が最新年度です。

以下のJSON形式のみで回答してください（説明文は不要）：
{{
  "value": <数値。カンマなしの数値。マイナスの場合は負の数。見つからない場合はnull>,
  "confidence": "<high または low。抜粋内に該当科目が明確に1箇所だけ存在し値も明瞭ならhigh、複数候補がある・文脈が曖昧・単位が不明瞭等はlow>",
  "evidence_snippet": "<該当行の原文をそのまま引用（30〜80文字程度）>",
  "unit_note": "<単位の記載があれば（百万円/千円等）、なければnull>"
}}
"""


def _call_model(model: str, excerpt: str, label: str) -> LLMResult:
    client = _get_client()
    prompt = PROMPT_TEMPLATE.format(excerpt=excerpt, label=label)
    resp = client.messages.create(
        model=model,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    # The strong model may return a ThinkingBlock first; only text blocks carry the answer.
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return LLMResult(
            value=None,
            confidence="low",
            evidence_snippet=None,
            unit_note=None,
            model_used=model,
            raw_response=raw,
        )
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return LLMResult(
            value=None,
            confidence="low",
            evidence_snippet=None,
            unit_note=None,
            model_used=model,
            raw_response=raw,
        )

    value = parsed.get("value")
    return LLMResult(
        value=float(value) if value is not None else None,
        confidence=parsed.get("confidence", "low"),
        evidence_snippet=parsed.get("evidence_snippet"),
        unit_note=parsed.get("unit_note"),
        model_used=model,
        raw_response=raw,
    )


def extract_with_verification(excerpt: str, label: str) -> LLMResult:
    """Cheap model first; escalate once to the strong model only if the
    cheap pass says low confidence or fails to parse a value at all.
    """
    result = _call_model(CHEAP_MODEL, excerpt, label)
    if result.confidence == "high" and result.value is not None:
        return result

    # One bounded escalation — never more than this.
    escalated = _call_model(STRONG_MODEL, excerpt, label)
    return escalated


def page_text_excerpt(pdf_path: Path, center_page: int, window: int = 1) -> str:
    """Plain-text (not table) extraction around a hint page, for feeding to
    the LLM. Deliberately uses `extract_text()` here, not table extraction:
    the LLM is meant to compensate exactly where table detection failed to
    keep a label and its value together.
    """
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        lo = max(0, center_page - window)
        hi = min(len(pdf.pages), center_page + window + 1)
        parts = []
        for i in range(lo, hi):
            parts.append(f"[page {i}]\n" + (pdf.pages[i].extract_text() or ""))
        return "\n\n".join(parts)


def find_label_pages(pdf_path: Path, label: str, page_range: Optional[range] = None) -> list[int]:
    """Plain-text scan (cheap) to locate which pages mention `label` at
    all, used when the deterministic table pass found nothing to anchor
    on. Returns page indices in order found.
    """
    import pdfplumber

    hits = []
    with pdfplumber.open(pdf_path) as pdf:
        indices = page_range if page_range is not None else range(len(pdf.pages))
        for i in indices:
            if i >= len(pdf.pages):
                break
            text = pdf.pages[i].extract_text() or ""
            if label in text:
                hits.append(i)
    return hits
