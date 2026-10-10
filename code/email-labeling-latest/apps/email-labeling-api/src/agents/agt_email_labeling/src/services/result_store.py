"""结果持久化共享助手（从原 orchestrator.py 抽离，供 V2 流水线与路由复用）。

只依赖 models / schemas，**不依赖任何 legacy stage**——这是删除 legacy 流水线的前置。
"""
import hashlib
import json
import logging
from typing import Optional

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.agt_email_labeling.src.models import (
    EmailLabelingEmail,
    EmailLabelingResult,
)
from src.agents.agt_email_labeling.src.schemas import (
    FilterResult,
    LabelEmailRequest,
    LabelResultResponse,
)

logger = logging.getLogger(__name__)


def n122_revision_hash(*, input_hash: str, prompt_hash: str, schema_hash: str, model: str) -> str:
    """Stable revision identity used to isolate reruns without deleting history."""
    payload = {"input_hash": input_hash, "prompt_hash": prompt_hash, "schema_hash": schema_hash, "model": model}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def n122_result_matches_revision(row: EmailLabelingResult, revision_hash: str) -> bool:
    """Read-side guard: an old model/prompt revision is never reused as current."""
    facts = row.facts or {}
    return (facts.get("provenance") or {}).get("n122_revision_hash") == revision_hash


async def _get_existing_result(email_id: str, db: AsyncSession) -> Optional[EmailLabelingResult]:
    row = await db.execute(
        select(EmailLabelingResult)
        .where(EmailLabelingResult.email_id == email_id)
        .order_by(EmailLabelingResult.version.desc())
        .limit(1)
    )
    return row.scalar_one_or_none()


async def delete_existing_results(email_id: str, db: AsyncSession) -> int:
    """删除某封邮件已有的全部结果行（force 重跑用：覆盖语义，重写后 version 归 1，避免读侧双计）。"""
    res = await db.execute(
        sa_delete(EmailLabelingResult).where(EmailLabelingResult.email_id == email_id)
    )
    return res.rowcount or 0


async def _next_version(email_id: str, db: AsyncSession) -> int:
    row = await db.execute(
        select(EmailLabelingResult.version)
        .where(EmailLabelingResult.email_id == email_id)
        .order_by(EmailLabelingResult.version.desc())
        .limit(1)
    )
    latest = row.scalar_one_or_none()
    return (latest or 0) + 1


async def _upsert_email(req: LabelEmailRequest, cleaning, db: AsyncSession) -> None:
    stmt = pg_insert(EmailLabelingEmail).values(
        email_id=req.email_id,
        thread_id=req.thread_id,
        customer_id=req.customer_id,
        sender_type=req.sender_type,
        sender_email=req.sender_email,
        subject=req.subject,
        raw_body=req.raw_body,
        cleaned_body=cleaning.cleaned_body,
        cleaning_meta=cleaning.meta.model_dump(),
        sent_at=req.sent_at.replace(tzinfo=None),
    ).on_conflict_do_nothing(index_elements=["email_id"])
    await db.execute(stmt)


async def _persist_filtered(
    req: LabelEmailRequest,
    cleaning,
    filter_result: FilterResult,
    db: AsyncSession,
) -> LabelResultResponse:
    """Stage 0 命中时短路持久化：直接写 email_topic=system_noise。不走 LLM。"""
    from src.agents.agt_email_labeling.src.pipeline.topic_definitions import TOPIC_DISPLAY_MAP
    try:
        await _upsert_email(req, cleaning, db)
        version = await _next_version(req.email_id, db)
        display = TOPIC_DISPLAY_MAP.get(filter_result.topic, filter_result.topic)
        result_row = EmailLabelingResult(
            email_id=req.email_id,
            thread_id=req.thread_id,
            version=version,
            sender_type=req.sender_type,
            email_summary=f"系统过滤：{filter_result.sub_action}",
            email_topic=filter_result.topic,
            email_topic_display=display,
            sub_action=filter_result.sub_action,
            label_reason=f"SystemFilter: {filter_result.reason}",
            email_label=display,
            facts={},
            risk_signals=[],
            opportunity_signals=[],
            confidence_score=1.0,
            confidence_level="high",
            confidence_detail={},
            evidence=[],
            ambiguity_flags=[],
            needs_review=False,
            review_reasons=[],
        )
        db.add(result_row)
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(result_row)
    return _to_response(result_row)


def _to_response(row: EmailLabelingResult) -> LabelResultResponse:
    facts = row.facts or {}
    return LabelResultResponse(
        email_id=row.email_id,
        thread_id=row.thread_id,
        email_summary=row.email_summary,
        email_topic=row.email_topic,
        email_topic_display=row.email_topic_display,
        sub_action=row.sub_action,
        label_reason=row.label_reason,
        email_label=row.email_label,
        primary_event=row.primary_event,
        secondary_events=row.secondary_events,
        risk_signals=row.risk_signals,
        opportunity_signals=row.opportunity_signals,
        confidence_score=row.confidence_score,
        confidence_level=row.confidence_level,
        needs_review=row.needs_review,
        review_reasons=row.review_reasons,
        created_at=row.created_at,
        multi_label=facts.get("multi_label") if facts.get("multi_label_version") == "v2" else None,
    )


def _derive_customer_email(req: LabelEmailRequest) -> Optional[str]:
    # 客户级上下文 key。客户编码（customer_id）是精确实体：一个客户多个邮箱/联系人，
    # 跨线程上下文要按编码聚合（否则 bart.stam@ 与 marc.hofman@ 会被当成两个客户）。
    # 优先用编码；无编码时回退旧行为（customer_email → 客户方 sender_email），兼容历史摄入。
    if req.customer_id:
        return req.customer_id
    if req.customer_email:
        return req.customer_email
    if req.sender_type == "customer" and req.sender_email:
        return req.sender_email
    return None
