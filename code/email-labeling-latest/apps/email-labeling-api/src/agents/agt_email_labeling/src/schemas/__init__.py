from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

AUTHORITATIVE_ONLY_CURRENT_FACT_FIELDS = frozenset({
    # 订单 / 样品 / 发货是否真实发生，只认 authoritative_events（ERP / 业务系统）。
    # LLM 仍可打“在谈订单/样品/物流”的 topic/action，但这些布尔事实不能进入最终事实层。
    "order_confirmed_in_email",
    "payment_confirmed_in_email",
    "shipment_mentioned_in_email",
    "delivery_received_in_email",
    "sample_mentioned_in_email",
    "sample_pi_in_email",
    "tracking_number_in_email",
    "order_detail_confirmed_in_email",
})


# ── 请求模型 ──────────────────────────────────────────────

class LabelEmailRequest(BaseModel):
    email_id: str
    thread_id: str
    customer_id: Optional[str] = None
    customer_email: Optional[str] = None
    sender_type: str = Field(..., pattern="^(customer|salesperson)$")
    sender_email: Optional[str] = None
    recipient_email: Optional[str] = None
    subject: Optional[str] = None
    raw_body: str
    sent_at: datetime
    # 权威业务事件（下单/发货，来自业务 API / ERP；测试期由订单 fixture 时间切片注入）。
    # 会按"客户摘要 + 活跃窗口 + 显式命中"压缩后进入线程上下文，帮助时序消歧；
    # 不产出阶段、不单调锁定、不替代当前邮件原文意图。
    authoritative_events: Optional[List[Dict]] = None
    # N122 batch runner sets this only after the frozen customer corpus has been
    # fully loaded.  The safe default is incomplete; online callers cannot infer
    # lifetime coverage merely because a database query returned some rows.
    n122_email_history_complete: bool = False


class BatchLabelRequest(BaseModel):
    emails: List[LabelEmailRequest]


class SubmitReviewRequest(BaseModel):
    # 业务漏斗已移除：人工复核只纠正主题(意图)/动作/风险，不再纠正"漏斗阶段"。
    corrected_primary_event: str
    corrected_secondary_events: Optional[List[str]] = None
    corrected_risk_signals: Optional[List[str]] = None
    corrected_opportunity_signals: Optional[List[str]] = None
    correction_reason: str


# ── 内部数据结构 ──────────────────────────────────────────

class CleaningMeta(BaseModel):
    removed_sections: List[str] = Field(default_factory=list)
    quality: str = "ok"  # ok | too_short | empty | possible_over_clean
    inline_reply_mode: bool = False  # 邮件正文是内联回复模式（"answers below"），原引用内容含业务信息


class CleaningResult(BaseModel):
    cleaned_body: str
    meta: CleaningMeta


class FilterResult(BaseModel):
    """Stage 0 SystemFilter 输出。命中规则时直接短路到 email_topic=other。"""
    topic: str  # other
    sub_action: str  # auto_reply | bounce | empty
    reason: str



class ConfidenceDetail(BaseModel):
    llm_event_confidence: float
    candidate_gap: float
    evidence_strength: float
    transition_reasonableness: float
    context_completeness: float
    weighted_score: float


class ConfidenceResult(BaseModel):
    score: float
    level: str  # high | medium | low
    detail: ConfidenceDetail
    needs_review: bool
    review_reasons: List[str]


# ── 响应模型 ──────────────────────────────────────────────

class LabelResultResponse(BaseModel):
    email_id: str
    thread_id: str
    email_summary: Optional[str] = None
    # V3 字段
    email_topic: Optional[str] = None
    email_topic_display: Optional[str] = None
    sub_action: Optional[str] = None
    label_reason: Optional[str] = None
    email_label: Optional[str] = None
    primary_event: Optional[str] = None
    secondary_events: Optional[List[str]] = None
    risk_signals: Optional[List[str]] = None        # V2: 派生风险信号（字符串列表）
    opportunity_signals: Optional[List[Dict[str, Any]]] = None
    confidence_score: Optional[float] = None
    confidence_level: Optional[str] = None
    needs_review: bool = False
    review_reasons: Optional[List[str]] = None
    created_at: Optional[datetime] = None
    # V2 multi-label 结构化字段（仅 V2 结果有值）
    multi_label: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


class N122LabelItem(BaseModel):
    """One independently judged N122 v8 business label.

    ``name`` is part of the final stored contract.  The extractor fills it
    from the frozen taxonomy after validating the model's code; it is not an
    LLM-generated synonym or a locally invented value.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    name: str
    evidence: str
    confidence: float = Field(ge=0.0, le=1.0)
    context_refs: List[str] = Field(default_factory=list)


class N122LabelOutput(BaseModel):
    """Versioned N122 v8 output shared by Lite and Flash."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["n122-label-output-v8"]
    taxonomy_hash: str
    direction: Literal["业务员→客户", "客户→业务员"]
    labels: List[N122LabelItem] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)
    confidence_level: Literal["high", "medium", "low"]
    validation_issues: List[str] = Field(default_factory=list)
    review_reasons: List[str] = Field(default_factory=list)


class BatchLabelResultResponse(BaseModel):
    results: List[Dict[str, Any]]
    total: int
    succeeded: int
    failed: int


class ReviewTaskResponse(BaseModel):
    id: str
    email_id: str
    thread_id: str
    predicted_result: Dict[str, Any]
    review_status: str
    created_at: datetime

    class Config:
        from_attributes = True


class EvaluationStatsResponse(BaseModel):
    primary_event_accuracy: Optional[float] = None
    risk_precision: Optional[float] = None
    risk_recall: Optional[float] = None
    high_confidence_coverage: Optional[float] = None
    high_confidence_accuracy: Optional[float] = None
    review_rate: Optional[float] = None
    total_samples: int = 0
    insufficient_samples: bool = True


# ── 新多标签体系 ──────────────────────────────────────────
# Phase 1: 并行输出，不改旧 pipeline。新结构写入 EmailLabelingResult.facts["multi_label"]。
# schema_version 用于后续兼容升级。

class TopicItem(BaseModel):
    """邮件涉及的一个业务主题。允许一封邮件有多个 topic。"""
    topic: str                                  # topic 枚举（见 TOPIC_DEFINITIONS）
    display: str = ""                           # 中文展示名
    role: str = "secondary"                     # primary | secondary
    confidence: float = 0.0
    evidence: str = ""


class ActionItem(BaseModel):
    """邮件中识别的一个具体业务动作。"""
    action: str                                 # action 枚举（见 ACTION_DEFINITIONS）
    display: str = ""                           # 中文展示名
    topic_ref: Optional[str] = None             # 关联的 topic 枚举值
    target_refs: List[str] = Field(default_factory=list)  # 关联的 entity id 列表
    confidence: float = 0.0
    evidence: str = ""


class EntityProduct(BaseModel):
    """产品实体。"""
    id: str = ""
    sku: Optional[str] = None
    product_name: Optional[str] = None
    category: Optional[str] = None
    quantity: Optional[str] = None
    color: Optional[str] = None
    related_parts: List[str] = Field(default_factory=list)
    evidence: str = ""


class EntityPrice(BaseModel):
    """价格实体。"""
    product_ref: Optional[str] = None
    price_value: Optional[str] = None
    currency: Optional[str] = None
    unit: Optional[str] = None                  # per unit | per piece | per kg
    price_type: Optional[str] = None            # warehouse_price | fob | cif | exw
    terms: Optional[str] = None
    evidence: str = ""


class EntityQuantity(BaseModel):
    """数量实体（独立于产品的数量描述，如 container/carton 级别）。"""
    product_ref: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None                  # pcs | sets | containers | cartons
    raw_text: Optional[str] = None
    evidence: str = ""


class EntityPayment(BaseModel):
    """付款实体。"""
    payment_status: Optional[str] = None        # paid | pending | partial
    payment_type: Optional[str] = None          # full_payment | deposit | sample_pi
    order_ref: Optional[str] = None
    evidence: str = ""


class EntityLogistics(BaseModel):
    """物流实体。"""
    shipment_stage: Optional[str] = None        # goods_ready | shipped | in_transit | customs | delivered
    eta_etd: Optional[str] = None
    tracking_number: Optional[str] = None
    delay_reason: Optional[str] = None
    evidence: str = ""


class EntityDocument(BaseModel):
    """文件/资料实体。"""
    document_type: Optional[str] = None         # manual | cdr | artwork | certificate | drawing | hs_code
    document_name: Optional[str] = None
    document_action: Optional[str] = None       # provided | requested | confirmed | amended
    product_ref: Optional[str] = None
    evidence: str = ""


class EntityOrder(BaseModel):
    """订单实体。"""
    order_ref: Optional[str] = None
    detail_type: Optional[str] = None           # component_confirmation | packaging | shipping_mark
    items: List[str] = Field(default_factory=list)
    evidence: str = ""


class EntityMeeting(BaseModel):
    """会议/拜访实体。"""
    meeting_type: Optional[str] = None          # online_meeting | onsite_visit | exhibition
    meeting_time: Optional[str] = None          # 具体时间（next Tuesday / 2024-05-12）
    location: Optional[str] = None              # 地点（warehouse / factory / showroom / address）
    meeting_link: Optional[str] = None          # Teams/Zoom link
    is_online: bool = False
    is_onsite: bool = False
    evidence: str = ""


class EntityCollection(BaseModel):
    """实体集合，作为 MultiLabelResult.entities 的类型。"""
    products: List[EntityProduct] = Field(default_factory=list)
    prices: List[EntityPrice] = Field(default_factory=list)
    quantities: List[EntityQuantity] = Field(default_factory=list)
    payments: List[EntityPayment] = Field(default_factory=list)
    logistics: List[EntityLogistics] = Field(default_factory=list)
    documents: List[EntityDocument] = Field(default_factory=list)
    orders: List[EntityOrder] = Field(default_factory=list)
    meetings: List[EntityMeeting] = Field(default_factory=list)


class DecisionEvidenceItem(BaseModel):
    """可审计证据映射：某个 topic/action/fact 为什么被打出来。"""
    label_type: str = ""                         # topic | action | fact | entity
    label: str = ""
    evidence: str = ""                           # 必须来自原文的短引用
    why: str = ""                                # 面向人的简短解释，不是隐藏 CoT


class DecisionRejectedCandidate(BaseModel):
    """被排除的候选标签：用于解释为什么不是另一个标签。"""
    label: str = ""
    reason: str = ""


class DecisionRuleTrace(BaseModel):
    """确定性后处理/规则介入记录。"""
    rule: str = ""
    effect: str = ""


class DecisionTrace(BaseModel):
    """产品级可审计推理链。

    注意：这不是模型隐藏 CoT，而是模型/系统显式产出的判定依据摘要。
    只用于展示、复盘和人工核验，不作为标签判定的单一真相源。
    """
    primary_topic_reason: str = ""
    primary_action_reason: str = ""
    evidence_map: List[DecisionEvidenceItem] = Field(default_factory=list)
    rejected_candidates: List[DecisionRejectedCandidate] = Field(default_factory=list)
    rule_overrides: List[DecisionRuleTrace] = Field(default_factory=list)


class CurrentEmailFacts(BaseModel):
    """输出：当前邮件明确提到的事实。

    由 MultiLabelExtractor 从当前邮件正文抽取。
    ContextUpdater 从中派生 recent_events 的布尔字段。
    不可从 ThreadContextHints 派生（避免历史状态污染）。

    边界：订单/样品/发货的“发生事实”完全来自 authoritative_events。
    这里保留字段名是为了让旧 raw JSON / replay 不炸，但任何构造都会强制清零。
    """
    # ── 核心交易事实 ──
    order_confirmed_in_email: bool = False
    payment_confirmed_in_email: bool = False
    shipment_mentioned_in_email: bool = False
    delivery_received_in_email: bool = False
    sample_mentioned_in_email: bool = False
    sample_pi_in_email: bool = False
    tracking_number_in_email: bool = False
    order_detail_confirmed_in_email: bool = False
    document_requested_in_email: bool = False
    document_provided_in_email: bool = False
    # ── 质量/价格/沟通事实 ──
    quality_complaint_in_email: bool = False
    price_quoted_in_email: bool = False
    price_inquiry_in_email: bool = False
    price_objection_in_email: bool = False
    # ── 营销/产品事实 ──（业务标签「新品推介」用）
    promotes_new_product_in_email: bool = False
    # ── 会面/会议事实 ──
    onsite_visit_in_email: bool = False
    online_meeting_in_email: bool = False

    def model_post_init(self, __context: Any) -> None:
        for field_name in AUTHORITATIVE_ONLY_CURRENT_FACT_FIELDS:
            object.__setattr__(self, field_name, False)


class ThreadContextHints(BaseModel):
    """输入：来自历史实体聚合的上下文标记。

    作为 MultiLabelExtractor 的输入，不在输出中。
    由 ThreadContextBuilderV2 从 multi_label_recent_events 聚合。
    """
    has_order_context: bool = False
    has_payment_context: bool = False
    has_shipment_context: bool = False
    has_delivery_received_context: bool = False
    has_sample_context: bool = False
    recent_entity_summary: str = ""  # 格式化给 LLM 看的文本


class MultiLabelResult(BaseModel):
    """V2 核心中间层：多 topic + 多 action + 结构化 entities。

    数据边界：
    - raw_llm_output / facts.raw_extraction = LLM 原始结构化抽取，供重放与审计；
    - facts.multi_label = 经过规则、封顶、主标派生、风险派生后的最终可用结果；
    - primary_topic 由 PrimaryTopicDeriver 后处理派生，不采信 LLM 自己的 role=primary。
    """
    schema_version: str = "multi_label_v1"
    summary: str = ""
    sender_type: str = ""
    topics: List[TopicItem] = Field(default_factory=list)
    actions: List[ActionItem] = Field(default_factory=list)
    entities: EntityCollection = Field(default_factory=EntityCollection)
    current_facts: CurrentEmailFacts = Field(default_factory=CurrentEmailFacts)
    decision_trace: DecisionTrace = Field(default_factory=DecisionTrace)
    primary_topic: Optional[str] = None          # 后处理派生，LLM 输出时可为空
    risk_signals: List[str] = Field(default_factory=list)  # 派生：风险信号
    confidence: float = 0.0
    needs_review: bool = False
    review_reason: str = ""
    raw_llm_output: str = ""                     # debug 用，保留 LLM 原始输出


# ── V2 Multi-Label 上下文结构 ──────────────────────────────


class MultiLabelRecentEvent(BaseModel):
    """V2 recent_events 条目。纯 multi-label，不沾旧标签。

    由 ContextUpdaterV2 写入 EmailLabelingThread.multi_label_recent_events。
    ThreadContextBuilderV2 只从这个结构读上下文。
    """
    schema_version: str = "multi_label_recent_event_v1"
    email_id: str = ""
    sender_type: str = ""
    primary_topic: str = ""
    topics: List[TopicItem] = Field(default_factory=list)
    actions: List[ActionItem] = Field(default_factory=list)
    entities: Dict[str, Any] = Field(default_factory=dict)       # EntityCollection model_dump()
    current_facts: Dict[str, Any] = Field(default_factory=dict)  # CurrentEmailFacts model_dump()
    evidence: str = ""
    excluded_from_context: bool = False  # SystemFilter 命中时标记为 true，聚合时跳过


class ThreadContextV2(BaseModel):
    """V2 线程上下文。只从 multi_label_recent_events 聚合。

    旧字段（email_topic / sub_action / primary_event / recent_events）完全不参与。
    """
    thread_id: str
    context_missing: bool = False
    multi_label_events: List[MultiLabelRecentEvent] = Field(default_factory=list)
    # ── 最近实体/事实汇总（给 MultiLabelExtractor prompt 的事实性上下文）──
    recent_entities_text: str = ""
    recent_facts_text: str = ""
    customer_ever_contacted: bool = False


class CustomerContextV2(BaseModel):
    """V2 客户级上下文。跨该客户**全部线程**聚合「状态/里程碑」，按时间截断。

    与 ThreadContextV2（单会话）正交：
      - 会话层给"本会话近期实体/事实"；客户层给"这个客户整体走到哪一步"。
      - 护栏①防污染：只聚状态/里程碑，**不聚产品实体**（产品级细节留在会话层）。
      - 护栏②防泄露：只取 sent_at < 当前邮件的其他线程事件。
      - 订单/样品/发货发生事实仍只来自 authoritative_events（ERP），不从邮件 LLM facts 推断。
    """
    customer_id: str = ""
    context_missing: bool = False
    other_thread_count: int = 0
    milestone_facts_text: str = ""


class PipelineMode(str, Enum):
    """Feature flag：新老 pipeline 切换。"""
    MULTILABEL = "multilabel"
    LEGACY = "legacy"
