from sqlalchemy import Column, String, Text, Boolean, Float, Integer, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func
import uuid

Base = declarative_base()


class EmailLabelingEmail(Base):
    __tablename__ = "email_labeling_emails"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_id = Column(String(255), nullable=False, unique=True)
    thread_id = Column(String(255), nullable=False)
    customer_id = Column(String(255))
    sender_type = Column(String(20), nullable=False)
    sender_email = Column(String(255))
    subject = Column(Text)
    raw_body = Column(Text, nullable=False)
    cleaned_body = Column(Text)
    cleaning_meta = Column(JSONB)
    sent_at = Column(DateTime, nullable=False)
    excluded_from_timeline = Column(Boolean, nullable=False, default=False)
    duplicate_of_email_id = Column(String(255))
    data_quality_flags = Column(JSONB, nullable=False, default=list)
    data_quality_note = Column(Text)
    data_quality_checked_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class EmailLabelingThread(Base):
    __tablename__ = "email_labeling_threads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id = Column(String(255), nullable=False, unique=True)
    customer_id = Column(String(255))
    # 业务漏斗（funnel stage / 单调里程碑）整层已移除（migration 092 drop 列）。
    latest_email_id = Column(String(255))
    thread_summary = Column(Text)
    # 最近动作 + 事件摘要
    last_action_event = Column(String(50))
    last_email_topic = Column(String(50))
    recent_events = Column(JSONB, nullable=False, default=list)
    # V2: 纯 multi-label 历史事件（不从旧标签派生）
    multi_label_recent_events = Column(JSONB, nullable=False, default=list)
    state_version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class EmailLabelingResult(Base):
    __tablename__ = "email_labeling_results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_id = Column(String(255), nullable=False)
    thread_id = Column(String(255), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    sender_type = Column(String(20))
    email_summary = Column(Text)
    # V3: 单封邮件话题（三路分离）
    email_topic = Column(String(50))
    email_topic_display = Column(String(50))
    sub_action = Column(String(50))
    label_reason = Column(Text)
    email_label = Column(String(50))
    # 漏斗阶段/状态机转移列已移除（migration 092 drop：stage_before/after/transition_*/sub_stages）。
    primary_event = Column(String(50))
    secondary_events = Column(JSONB)
    facts = Column(JSONB)
    risk_signals = Column(JSONB)
    opportunity_signals = Column(JSONB)
    confidence_score = Column(Float)
    confidence_level = Column(String(10))
    confidence_detail = Column(JSONB)
    evidence = Column(JSONB)
    ambiguity_flags = Column(JSONB)
    needs_review = Column(Boolean, nullable=False, default=False)
    review_reasons = Column(JSONB)
    model_name = Column(String(100))
    prompt_version = Column(String(50))
    state_machine_version = Column(String(50))
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class EmailLabelingReviewTask(Base):
    __tablename__ = "email_labeling_review_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_id = Column(String(255), nullable=False)
    thread_id = Column(String(255), nullable=False)
    label_result_id = Column(UUID(as_uuid=True), ForeignKey("email_labeling_results.id"))
    predicted_result = Column(JSONB, nullable=False)
    review_status = Column(String(20), nullable=False, default="pending")
    # corrected_funnel_stage 已移除（漏斗废弃，migration 092 drop 列）
    corrected_primary_event = Column(String(50))
    corrected_secondary_events = Column(JSONB)
    corrected_risk_signals = Column(JSONB)
    corrected_opportunity_signals = Column(JSONB)
    reviewer = Column(String(255))
    correction_reason = Column(Text)
    is_evaluation_sample = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    reviewed_at = Column(DateTime)


class EmailLabelingVersion(Base):
    __tablename__ = "email_labeling_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_code = Column(String(50), nullable=False, unique=True)
    # funnel_stages 已移除（漏斗废弃，migration 092 drop 列）
    email_events = Column(JSONB, nullable=False)
    risk_signals = Column(JSONB, nullable=False)
    opportunity_signals = Column(JSONB, nullable=False)
    prompt_config = Column(JSONB)
    state_machine_config = Column(JSONB)
    is_active = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class EmailLabelingIssue(Base):
    """V3: 质量/风险问题追踪表，由 issue_tracker 写入。"""
    __tablename__ = "email_labeling_issues"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id = Column(String(255), nullable=False)
    customer_id = Column(String(255))
    issue_type = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, default="open")
    severity = Column(String(20))
    opened_by_email_id = Column(String(255))
    latest_related_email_id = Column(String(255))
    opened_at = Column(DateTime, nullable=False, server_default=func.now())
    resolved_at = Column(DateTime)
    resolution_note = Column(Text)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
