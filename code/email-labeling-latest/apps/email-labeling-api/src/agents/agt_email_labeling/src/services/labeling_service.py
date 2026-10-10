import asyncio
import logging
from typing import List

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.agt_email_labeling.src.models import (
    EmailLabelingEmail,
    EmailLabelingResult,
    EmailLabelingReviewTask,
)
from src.agents.agt_email_labeling.src.schemas import (
    BatchLabelRequest,
    BatchLabelResultResponse,
    EvaluationStatsResponse,
    LabelEmailRequest,
    LabelResultResponse,
    ReviewTaskResponse,
    SubmitReviewRequest,
)
from src.agents.agt_email_labeling.src.services.orchestrator_v2 import run_pipeline

logger = logging.getLogger(__name__)


class LabelingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def label_single(self, req: LabelEmailRequest) -> LabelResultResponse:
        return await run_pipeline(req, self.db)

    async def label_batch(self, req: BatchLabelRequest) -> BatchLabelResultResponse:
        """批量打标：同线程按时间顺序，跨线程并发。"""
        # 按 thread_id 分组
        groups: dict[str, List[LabelEmailRequest]] = {}
        for email in req.emails:
            groups.setdefault(email.thread_id, []).append(email)

        # 同线程内按 sent_at 排序
        for emails in groups.values():
            emails.sort(key=lambda e: e.sent_at)

        results = []

        async def process_thread(emails: List[LabelEmailRequest]) -> None:
            for email in emails:
                result = await run_pipeline(email, self.db)
                results.append(result.model_dump())

        await asyncio.gather(*[process_thread(emails) for emails in groups.values()])

        return BatchLabelResultResponse(
            results=results,
            total=len(req.emails),
            succeeded=len(req.emails),
            failed=0,
        )


class ReviewService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def submit_review(
        self, task_id: str, req: SubmitReviewRequest, reviewer: str
    ) -> ReviewTaskResponse:
        row = await self.db.execute(
            select(EmailLabelingReviewTask).where(
                EmailLabelingReviewTask.id == task_id
            )
        )
        task = row.scalar_one_or_none()
        if task is None:
            raise ValueError(f"Review task {task_id} not found")

        task.review_status = "completed"
        # 邮件级标签只纠正主题/动作/风险；旧漏斗阶段已从复核语义中移除。
        task.corrected_primary_event = req.corrected_primary_event
        task.corrected_secondary_events = req.corrected_secondary_events
        task.corrected_risk_signals = req.corrected_risk_signals
        task.corrected_opportunity_signals = req.corrected_opportunity_signals
        task.reviewer = reviewer
        task.correction_reason = req.correction_reason
        task.is_evaluation_sample = True
        # 注意：不要在这里恢复 funnel/customer_funnel 写回。
        # 复核纠错只沉淀为评估/校准样本，真正的业务事实走 authoritative_events。

        await self.db.commit()
        await self.db.refresh(task)
        return ReviewTaskResponse(
            id=str(task.id),
            email_id=task.email_id,
            thread_id=task.thread_id,
            predicted_result=task.predicted_result,
            review_status=task.review_status,
            created_at=task.created_at,
        )

    async def list_pending(self, limit: int = 50, offset: int = 0):
        rows = await self.db.execute(
            select(EmailLabelingReviewTask)
            .join(EmailLabelingEmail, EmailLabelingEmail.email_id == EmailLabelingReviewTask.email_id)
            .where(EmailLabelingReviewTask.review_status == "pending")
            .where(EmailLabelingEmail.excluded_from_timeline == False)
            .order_by(EmailLabelingReviewTask.created_at)
            .limit(limit)
            .offset(offset)
        )
        tasks = rows.scalars().all()
        return [
            ReviewTaskResponse(
                id=str(t.id),
                email_id=t.email_id,
                thread_id=t.thread_id,
                predicted_result=t.predicted_result,
                review_status=t.review_status,
                created_at=t.created_at,
            )
            for t in tasks
        ]


class EvaluationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_stats(self) -> EvaluationStatsResponse:
        # 查询已完成复核的样本
        rows = await self.db.execute(
            select(EmailLabelingReviewTask).where(
                EmailLabelingReviewTask.is_evaluation_sample == True,
                EmailLabelingReviewTask.review_status == "completed",
            )
        )
        samples = rows.scalars().all()

        total = len(samples)
        if total < 10:
            return EvaluationStatsResponse(total_samples=total, insufficient_samples=True)

        # 主事件准确率
        event_correct = 0
        for sample in samples:
            predicted = sample.predicted_result if isinstance(sample.predicted_result, dict) else {}
            predicted_primary = (
                predicted.get("primary_event")
                or predicted.get("email_topic")
                or predicted.get("primary_topic")
            )
            if predicted_primary == sample.corrected_primary_event:
                event_correct += 1
        # 复核率
        total_results_row = await self.db.execute(
            select(func.count()).select_from(EmailLabelingResult)
        )
        total_results = total_results_row.scalar() or 1
        review_rate_row = await self.db.execute(
            select(func.count()).select_from(EmailLabelingResult).where(
                EmailLabelingResult.needs_review == True
            )
        )
        review_count = review_rate_row.scalar() or 0

        # 高置信覆盖率和准确率
        high_conf_rows = await self.db.execute(
            select(EmailLabelingResult).where(
                EmailLabelingResult.confidence_level == "high"
            ).limit(1000)
        )
        high_conf_results = high_conf_rows.scalars().all()
        high_conf_coverage = len(high_conf_results) / total_results if total_results else 0

        return EvaluationStatsResponse(
            primary_event_accuracy=round(event_correct / total, 4),
            high_confidence_coverage=round(high_conf_coverage, 4),
            review_rate=round(review_count / total_results, 4),
            total_samples=total,
            insufficient_samples=False,
        )
