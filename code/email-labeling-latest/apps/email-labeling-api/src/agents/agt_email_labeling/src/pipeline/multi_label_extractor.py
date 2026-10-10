"""V2 multi-label 抽取层。

职责边界：
  原文 + 线程/权威上下文 → LLM raw JSON → MultiLabelResult

本模块只负责现役 V2 的语义中间层构造：prompt 渲染、LLM 调用、JSON 修复、枚举归一化、
审计依据解析、primary_topic 的确定性派生。不要在这里写落库、复核路由或管理端跑批逻辑。
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.agt_email_labeling.src.pipeline import taxonomy
from src.llm.agent_helper import AgentLLM

logger = logging.getLogger(__name__)

_AGENT_ID = "agt_email_labeling"


class N122ExtractionContractError(ValueError):
    """A model response cannot be accepted as an N122 v8 result."""


def _n122_spec():
    from src.agents.agt_email_labeling.src.pipeline.n122_label_spec import load_n122_spec

    path = Path(__file__).resolve().parents[2] / "resources" / "n122_v8.json"
    return load_n122_spec(path)


def _n122_direction(sender_type: str) -> str:
    normalized = str(sender_type or "").strip().lower()
    if normalized in {"salesperson", "sales", "业务员", "业务员→客户"}:
        return "业务员→客户"
    if normalized in {"customer", "客户", "客户→业务员"}:
        return "客户→业务员"
    raise N122ExtractionContractError(f"unsupported sender_type: {sender_type!r}")


def _n122_contract_data() -> tuple[dict[str, dict], str]:
    spec = _n122_spec()
    return spec.labels_by_code, spec.spec_hash


def build_n122_response_schema() -> dict:
    """Return the one JSON schema used for both N122 Lite and Flash calls."""
    labels, taxonomy_hash = _n122_contract_data()
    codes = sorted(labels)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "taxonomy_hash", "direction", "labels", "overall_confidence", "confidence_level"],
        "properties": {
            "schema_version": {"const": "n122-label-output-v8"},
            "taxonomy_hash": {"const": taxonomy_hash},
            "direction": {"enum": ["业务员→客户", "客户→业务员"]},
            "labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "evidence", "confidence", "context_refs"],
                    "properties": {
                        "code": {"enum": codes},
                        "evidence": {"type": "string", "minLength": 1},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "context_refs": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "overall_confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "confidence_level": {"enum": ["high", "medium", "low"]},
        },
    }


def build_n122_prompt(
    cleaned_body: str,
    sender_type: str,
    subject: str = "",
    thread_context_text: str = "",
    customer_context_text: str = "",
    context_sources: Optional[dict[str, str]] = None,
) -> str:
    """Render the v8 prompt from the frozen taxonomy, for either model."""
    labels, taxonomy_hash = _n122_contract_data()
    direction = _n122_direction(sender_type)
    sources = context_sources or {}
    definitions = "\n".join(
        f"- {code} / {item['name']}：{item['definition']}；触发：{item['trigger']}；边界：{item['boundary']}"
        for code, item in sorted(labels.items())
        if item["direction"] in {direction, "通用"}
    )
    return f"""你是 N122 邮件业务标注器。按冻结的 n122-label-output-v8 合同，基于当前邮件正文进行并列多标签判断。

版本：n122-label-output-v8
taxonomy_hash：{taxonomy_hash}
发件方向：{direction}

只使用当前邮件正文作标签证据；历史上下文只用于消歧，并在 context_refs 中写明引用。不要把参考答案、旧系统标签或模型上一轮答案当输入。每个输出标签必须有当前正文中的短证据；无法从正文确认的候选不要输出。会议链接/时间、商务拜访主题或短确认只有在主题或上下文确实提供唯一可核对来源时才引用，并在 context_refs 中写入准确来源键，不得补造事实。

激活标签定义（代码和名称必须逐项匹配，不可改名）：
{definitions}

业务边界提醒（优先按当前正文动作，不按关键词堆叠）：
- O04 与 O27 互斥：明确建议客户选择产品/方案/替代型号才是 O04；只客观说明结构、配置、电机/控制盒升级或版本事实、没有推进选择才是 O27。
- O07 只在实际发送报价单、PI、明确价格更新或成交报价时使用；产品介绍中顺带说“better price/价格”不单独触发 O07。O21 只用于订单/样品履约中的装柜、CBM、托盘、货代或运输条件，报价阶段的 FOB/运费/目的地成本属于 O07 或 I08/I24，不是 O21/I16。
- O08 必须有历史报价且当前正文在追踪报价反馈；历史旗标由上下文提供。已有报价后问“是否还有机会/反馈/进口计划”优先 O08，不是 O04；仅说运费下降或价格有竞争力也不是产品推荐。O01/I01/O03 等生命周期标签只有在上下文事实满足时才输出。
- O27 的产品升级说明即使带有一个 better price 或金额，也仍可只标 O27；只有形成报价单/PI/明确报价更新动作才加 O07。
- Teams/Zoom/Webex 链接本身就是会议对象；业务员发送链接可用 O10，客户确认时间/链接可用 I13，但客户拜访/工厂到访仍用 I28/O23。
- O20 是询问是否需要或测试样品；已经开票、准备、寄送或执行样品改用 O09/O22。没有任何可确认业务事件时使用 C03/C04，不要输出空 labels。
- 仅收到适配器/货物并礼貌致谢、没有质量或测试结论时使用 C01；泛满意度问卷、合作感谢或征求反馈但没有当前业务动作时使用 C04。
- “Business Visit/拜访”出现在当前主题且正文协调邀请或接待时，O23 可以用主题原文作 evidence，并在 context_refs 中写入 "subject"；主题证据必须逐字复制，不得改写。
- 邀请函、证书、图纸等资料动作的 O15 evidence 必须逐字引用当前正文中的资料动作短语；不要用主题替代正文资料证据。
- O13 仅表示已经发生或已确认的交期延期；模具设计、产品开发或普通“仍在处理”不等于交期延期。发件方向是系统给定事实：业务员用 O/C，客户用 I/C；不要因句子是疑问句就反转方向。

输出要求：
- labels 是并列标签数组，不设主/次角色，也没有数量上限；每项必须有 code、evidence、confidence、context_refs。名称由本地冻结字典按 code 回填，不要输出 name。
- confidence 和 overall_confidence 均为 0 到 1；overall_confidence 必须等于所有标签 confidence 的最低值。high>=0.90，medium>=0.75 且<0.90，low<0.75。
- direction 必须为“业务员→客户”或“客户→业务员”。validation_issues 和 review_reasons 由本地校验/路由生成，不要输出。
- 严格只返回一个 JSON 对象，不要 Markdown、解释文字或额外字段。

当前邮件主题：
{subject or '无'}

当前会话上下文（仅消歧）：
{thread_context_text or '无'}

客户历史上下文（仅消歧）：
{customer_context_text or '无'}

允许引用的上下文来源（仅可使用下列键；值为原始可核对文本）：
{json.dumps(sources, ensure_ascii=False, sort_keys=True) if sources else '无'}

当前邮件正文：
{cleaned_body}

最后的返回格式再次强调：只返回一个 JSON 对象，不能省略任何顶层字段，不能添加 Markdown 或解释文字。顶层字段必须恰好是 schema_version、taxonomy_hash、direction、labels、overall_confidence、confidence_level；schema_version 必须为 "n122-label-output-v8"，taxonomy_hash 必须为上面的 "{taxonomy_hash}"。即使没有额外标签，也必须保留这些顶层字段；labels 中每项只能有 code、evidence、confidence、context_refs。
evidence 必须从当前邮件正文或主题中逐字复制连续短片段（最多 12 个英文词、建议 10–80 个字符），优先选择同一行内的短语，绝不跨空行或跨换行拼接；保留原始标点和空格，不得改写、翻译或压平空格；无法逐字复制的标签不要输出。
方向是硬约束：发件方向为“业务员→客户”时，labels 只能使用 O01–O27 或 C01–C04，绝不能输出任何 I 代码；发件方向为“客户→业务员”时，labels 只能使用 I01–I30（不含 I25）或 C01–C04，绝不能输出任何 O 代码。
"""


def parse_n122_label_output(
    raw: str,
    *,
    sender_type: str,
    current_body: str,
    subject: str = "",
    context_sources: Optional[dict[str, str]] = None,
    local_validation_issues: Optional[list[str]] = None,
    local_review_reasons: Optional[list[str]] = None,
):
    """Parse and validate one N122 v8 response; errors remain explicit."""
    from src.agents.agt_email_labeling.src.schemas import N122LabelItem, N122LabelOutput

    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise N122ExtractionContractError(f"invalid N122 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise N122ExtractionContractError("N122 response must be a JSON object")

    labels_by_code, taxonomy_hash = _n122_contract_data()
    expected_direction = _n122_direction(sender_type)
    required = {"schema_version", "taxonomy_hash", "direction", "labels", "overall_confidence", "confidence_level"}
    if set(data) != required:
        raise N122ExtractionContractError("N122 response fields do not match v8 contract")
    if data["schema_version"] != "n122-label-output-v8":
        raise N122ExtractionContractError("unsupported N122 schema_version")
    if data["taxonomy_hash"] != taxonomy_hash:
        raise N122ExtractionContractError("N122 taxonomy_hash does not match frozen spec")
    if data["direction"] != expected_direction:
        raise N122ExtractionContractError("N122 direction does not match sender_type")
    if not isinstance(data["labels"], list) or not data["labels"]:
        raise N122ExtractionContractError("N122 labels must be a non-empty array")
    if not isinstance(data["confidence_level"], str) or data["confidence_level"] not in {"high", "medium", "low"}:
        raise N122ExtractionContractError("invalid N122 confidence_level")
    items = []
    seen = set()
    body = str(current_body or "")
    subject_text = str(subject or "")
    sources = context_sources or {}
    if not all(isinstance(key, str) and key.strip() and isinstance(value, str) for key, value in sources.items()):
        raise N122ExtractionContractError("context_sources must map non-empty keys to source text")
    allowed_refs = set(sources)
    allowed_refs.add("subject")
    for raw_item in data["labels"]:
        if not isinstance(raw_item, dict):
            raise N122ExtractionContractError("each N122 label must be an object")
        if set(raw_item) != {"code", "evidence", "confidence", "context_refs"}:
            raise N122ExtractionContractError("N122 label fields do not match v8 contract")
        code = raw_item["code"]
        if not isinstance(code, str) or code not in labels_by_code:
            raise N122ExtractionContractError(f"unknown N122 label code: {code!r}")
        if labels_by_code[code]["direction"] not in {expected_direction, "通用"}:
            raise N122ExtractionContractError(f"N122 label direction mismatch for {code}")
        if code in seen:
            raise N122ExtractionContractError(f"duplicate N122 label code: {code!r}")
        seen.add(code)
        evidence = raw_item["evidence"]
        refs = raw_item["context_refs"]
        if not isinstance(refs, list) or not all(isinstance(ref, str) and ref.strip() for ref in refs):
            raise N122ExtractionContractError(f"N122 context_refs invalid for {code}")
        if any(ref not in allowed_refs for ref in refs):
            raise N122ExtractionContractError(f"N122 context_refs contain an unknown source for {code}")
        in_body = isinstance(evidence, str) and evidence.strip() and evidence in body
        in_subject = isinstance(evidence, str) and evidence.strip() and evidence in subject_text and "subject" in refs
        in_context = any(isinstance(evidence, str) and evidence in sources[ref] for ref in refs if ref in sources)
        if "subject" in refs and not in_subject:
            raise N122ExtractionContractError(f"N122 subject evidence does not match subject for {code}")
        # Historical evidence is only valid for the workbook's short-confirmation
        # and meeting-confirmation cases. It cannot create a current product/order
        # event merely because an earlier message contains matching words.
        if in_context and not in_body and code not in {"C01", "I13"}:
            raise N122ExtractionContractError(f"N122 historical context cannot supply evidence for {code}")
        if in_subject and code not in {"O10", "I13", "O23"}:
            raise N122ExtractionContractError(f"N122 subject evidence is not valid for {code}")
        if not in_body and not in_subject and not in_context:
            raise N122ExtractionContractError(f"N122 evidence is not present in an allowed source for {code}")
        confidence = raw_item["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            raise N122ExtractionContractError(f"N122 confidence out of range for {code}")
        items.append(N122LabelItem(code=code, name=labels_by_code[code]["name"], evidence=evidence, confidence=confidence, context_refs=refs))

    overall = data["overall_confidence"]
    if isinstance(overall, bool) or not isinstance(overall, (int, float)) or not 0.0 <= overall <= 1.0:
        raise N122ExtractionContractError("N122 overall_confidence out of range")
    expected_overall = min(item.confidence for item in items)
    if abs(float(overall) - float(expected_overall)) > 1e-9:
        raise N122ExtractionContractError("N122 overall_confidence must be the minimum label confidence")
    level = "high" if overall >= 0.90 else "medium" if overall >= 0.75 else "low"
    if data["confidence_level"] != level:
        raise N122ExtractionContractError("N122 confidence_level does not match overall_confidence")

    return N122LabelOutput(
        schema_version=data["schema_version"], taxonomy_hash=data["taxonomy_hash"], direction=data["direction"],
        labels=items, overall_confidence=overall, confidence_level=data["confidence_level"],
        validation_issues=list(local_validation_issues or []), review_reasons=list(local_review_reasons or []),
    )

# ── Unknown value tracking（Phase 1 监控用）──
_UNKNOWN_VALUE_STATS: dict[str, dict[str, int]] = {
    "topics": {},
    "actions": {},
}


def _track_unknown(category: str, raw_value: str, normalized: str) -> None:
    """累加未知 topic/action 出现次数，日志 WARNING 便于监测。"""
    stats = _UNKNOWN_VALUE_STATS[category]
    key = f"{raw_value!r}→{normalized!r}"
    stats[key] = stats.get(key, 0) + 1
    logger.warning(
        "Unknown %s value: %r (normalized=%r) — count=%d",
        category, raw_value, normalized, stats[key],
    )


# ── Phase 1: Multi-Label Extractor ──────────────────────────────

_MULTI_LABEL_SYSTEM_PROMPT = """你是一个 B2B 外贸邮件分析专家。分析邮件正文，按结构化输出。

## 输出原则
1. 精确：只写邮件中明确提到的内容，不推断、不补充。
2. 全面：邮件涉及多个 topic 和 action 时全部列出。
3. 证据链：每个判断必须附原文证据。
4. 严格 JSON：只输出 JSON，不要 Markdown 代码块包裹。
5. 多语言：邮件正文可能是任意语种（英/德/法/丹麦/匈牙利/荷兰/西/意/波/俄等），请正常理解并按同一套枚举打标；证据片段保留邮件原文（原语言），summary 用中文。若正文语言你无法可靠理解，宁可少标也不要臆测。
6. 不要输出任何置信度/概率数字（confidence、score 等）。你只负责"看懂并给出标签 + 原文依据"，确定性由系统依据证据自行判断。无把握时宁可不打该标签，而不是打一个低置信标签。

## 判定准则（通用，作用于所有标签——抓"发件人这封到底在干嘛"，不是出现了哪些词）
1. **意图 vs 提及**：一个 action 只在**发件人在这封邮件里实际执行/请求**了这件事时才记。
   单纯的**提及、卖点宣传、对"可提供项/历史/附件"的引用**一律不记。
   例：推销里的 "FOB $80/PCS"、"samples available" 是卖点，不是"报价 / 申请样品"。
   只有"业务员回应客户询价、给出针对性报价"才是 send_quotation；只有"实际申请/确认/寄送样品"才是 sample_*。
2. **发件方向**：业务员主动推介/催进展 = 营销触达/业务跟进；客户主动问价/问规格/索资料/求合作 =
   需求询盘/经销。同一句"价格"，业务员主动发 vs 客户来问，标签不同。付款确认即使极短（"Paid"/"已付"）也记 order_payment。
3. **结合历史时序消歧**（参考下方"当前会话上下文"与"客户全局上下文"，不要无视历史把后段邮件错当早段）：
   - 已下单/已付款后，客户问交期/物流 → 履约 fulfillment_logistics，**不是**询盘；
   - 已收货后客户的问题/诉求 → 售后 post_delivery_support / quality_issue，**不是**需求讨论；
   - 线程内客户从未询价时，业务员带价的产品介绍 → 营销，**不是**报价。
4. **节日/季节性问候**：当邮件是节日/季节祝福（Subject 或正文含 Christmas / Holiday / New Year /
   Season's Greetings / Merry / Happy New Year / 节日快乐 等），内容为问候、维系关系、无实质业务时 →
   记 action `holiday_greeting`，归 topic `follow_up`（跟进/唤醒）。它有明确问候意图、非空洞，**不要**标
   `no_action_reply`；它没有产品推介，**也不要**标 `marketing_promotion`。
5. **样品反馈看是否已收样**：客户对**已经收到的样品**给出使用/质量反馈 → 属售后语境，
   topic 记 `post_delivery_support`（交付后支持）；若客户尚处采样/评估、样品**未交付**，
   则仍归 `sample_preparation`（样品申请/备样）。两者按"是否已收到样品"区分，不要一律归一类。
6. **业务推介一律用中性动作 `product_outreach`（产品推介）**：业务员主动向客户推介/介绍产品（含首次开发、
   自我介绍、产品介绍、给老客户推新等）→ 一律记 action `product_outreach`，归 topic `marketing_promotion`。
   **不要**在动作里区分"冷启动/新品/老客户召回"——那是系统按 ERP+时序自动派生的业务标签,与你无关。
   你只需做一件语义判断:**若本封明确在推介"新品/最新款"(new / latest / 新品 等),置 `promotes_new_product_in_email` = true**。
7. **新增动作(按字面识别即可)**:
   - `out_of_stock_notice` 缺货/无法供货告知:业务员告知缺货/德国仓无货/不能加柜/无法按需供货 → 归 fulfillment_logistics。
   - `damage_shortage_report` 缺件损坏反馈:客户反馈缺件/损坏/漏发(报告问题本身)→ 归 quality_issue。
   - `return_exchange` 退换货处理:因错发/不符/不满意发起退货或换货并协商 → 归 quality_issue;可与 quality_complaint / damage_shortage_report **共存**。
   - `pause_hold` 暂停搁置 / `terminate_cooperation` 终止合作:客户暂停推进/项目搁置、或终止合作关系 → 归 cancellation_pause。
   - `visit_followup` 展会/拜访跟进:展会结束后或线下拜访后的后续跟进 → 归 meeting_arrangement。
8. **边界消歧(易混三组)**:
   - 产品细节咨询 `product_detail_inquiry`(客户**单向**问产品细节/适配/认证/包装/定制)↔ 规格沟通 `spec_discussion`(型号替代/技术规格的**双向**讨论)↔ 需求确认 `requirement_clarification`(仅澄清数量/MOQ/目标市场,**不涉技术规格**)。
   - 质量投诉 `quality_complaint`(报告质量异常/不符本身)↔ 缺件损坏反馈 `damage_shortage_report`(缺件/损坏/漏发本身)↔ 退换货处理 `return_exchange`(进入退换协商);前两者报告问题、后者协商处理,可共存。
   - 催回复 `follow_up_reminder`(无特定标的物的泛泛催进展)↔ 报价跟进 `quotation_followup`(专门催客户查看/反馈**报价单**)。"""

# current_facts 字段 → prompt 描述映射（单一真相源：taxonomy.py）
_CURRENT_FACTS_PROMPT_DESC: dict[str, str] = dict(taxonomy.FACT_DESC)

_MULTI_LABEL_USER_PROMPT_TEMPLATE = """分析以下 B2B 外贸邮件。

## 发件方类型
{sender_type}

## 当前会话上下文（本会话历史邮件结构化聚合，非当前邮件）
{thread_context_text}

## 客户全局上下文（跨该客户全部会话聚合·截至本邮件；客户整体进展/里程碑 + ERP 权威事件）
{customer_context_text}

## 邮件正文
{cleaned_body}

## 输出结构

### topics[]
topic 是邮件涉及的业务领域。允许多个，每项包含：
- `topic`: 枚举值（见下方可选值）
- `role`: "primary" | "secondary"（哪个是最核心的领域）

可选 topic 枚举值：
{topic_defs}

### actions[]
action 是邮件中的具体动作。允许多个，每项包含：
- `action`: 枚举值（见下方可选值）
- `topic_ref`: 关联的 topic 枚举值
- `target_refs`: 关联的 entity id 列表（如指向某个产品/订单）
- `evidence`: 支撑该动作的**原文引用**（必填，用于人工核验）

可选 action 枚举值：
{action_defs}

### entities{{}}
邮件中提及的结构化实体。各项均为数组，无相关实体时给空数组。

#### products[]
{{ "id": "唯一标识(如p1)", "sku": "SKU/型号", "product_name": "产品名", "category": "品类", "quantity": "数量", "color": "颜色", "related_parts": ["配件1","配件2"], "evidence": "原文依据" }}

#### prices[]
{{ "product_ref": "关联产品id", "price_value": "金额", "currency": "币种", "unit": "per unit|per piece|per kg", "price_type": "warehouse_price|fob|cif|exw", "terms": "条款说明", "evidence": "原文依据" }}

#### quantities[]
{{ "product_ref": "关联产品id", "value": "数值", "unit": "pcs|sets|containers|cartons", "raw_text": "原文说法", "evidence": "原文依据" }}

#### payments[]
{{ "payment_status": "paid|pending|partial", "payment_type": "full_payment|deposit|sample_pi", "order_ref": "关联订单号", "evidence": "原文依据" }}

#### logistics[]
{{ "shipment_stage": "goods_ready|shipped|in_transit|customs|delivered", "eta_etd": "预计时间", "tracking_number": "运单号", "delay_reason": "延迟原因", "evidence": "原文依据" }}

#### documents[]
{{ "document_type": "manual|cdr|artwork|certificate|drawing|hs_code", "document_name": "文件名", "document_action": "provided|requested|confirmed|amended", "product_ref": "关联产品id", "evidence": "原文依据" }}

#### orders[]
{{ "order_ref": "订单号", "detail_type": "component_confirmation|packaging|shipping_mark", "items": ["明细1","明细2"], "evidence": "原文依据" }}

#### meetings[]
{{ "meeting_type": "online_meeting|onsite_visit|exhibition", "meeting_time": "具体时间", "location": "地点", "meeting_link": "链接", "is_online": true/false, "is_onsite": true/false, "evidence": "原文依据" }}

### current_facts{{}}
当前邮件是否明确提到以下事实（true/false）：
{current_facts_defs}

边界：
- 是否已下单、是否已付款、是否已发货、是否已收货、样品单是否成立，只来自 authoritative_events；
- 这封邮件可以打“在谈订单/样品/物流”的 topic/action，但不要把邮件文本自行升级成订单/样品权威事实。

### decision_trace{{}}
产品级可审计推理链（不是隐藏思维链/CoT）。用于人工核验：
- `primary_topic_reason`: 一句话说明为什么主主题最合适
- `primary_action_reason`: 一句话说明为什么主动作最合适；若无动作，说明“无实质动作”
- `evidence_map`: 每个关键 topic/action/fact 对应的原文证据和简短解释
- `rejected_candidates`: 容易混淆但被排除的候选标签及原因（最多 3 个）

要求：
- `evidence` 必须是原文短引用，能在邮件正文里找到
- `why/reason` 只写可审计的业务依据，不要写隐藏思维过程
- 不要输出任何置信度/概率数字

### summary
邮件一句话摘要（中文，不超过80字）

## 输出要求
返回严格 JSON，不要 Markdown 包裹。

{{
  "summary": "...",
  "topics": [
    {{ "topic": "follow_up", "role": "primary" }},
    {{ "topic": "quotation", "role": "secondary" }}
  ],
  "actions": [
    {{ "action": "follow_up_reminder", "topic_ref": "follow_up", "target_refs": [], "evidence": "原文引用" }},
    {{ "action": "send_quotation", "topic_ref": "quotation", "target_refs": [], "evidence": "原文引用" }}
  ],
  "entities": {{
    "products": [],
    "prices": [],
    "quantities": [],
    "payments": [],
    "logistics": [],
    "documents": [],
    "orders": [],
    "meetings": []
  }},
  "current_facts": {{
{current_facts_example}
  }},
  "decision_trace": {{
    "primary_topic_reason": "为什么选择这个主主题",
    "primary_action_reason": "为什么选择这个主动作",
    "evidence_map": [
      {{ "label_type": "action", "label": "follow_up_reminder", "evidence": "原文引用", "why": "这句话是在催促对方回复/跟进进展" }}
    ],
    "rejected_candidates": [
      {{ "label": "quotation", "reason": "只是提到价格背景，没有实际报价动作" }}
    ]
  }}
}}"""


def _render_topic_defs() -> str:
    """从 TopicCode + TOPIC_DISPLAY_MAP 生成 prompt 中的 topic 列表。
    注：曾试过给 requirement_discussion 等加"边界"说明(Step5),离线 gate 验证回归
    (-4.3pt primary/-4.9pt recall：LLM 过度回避 requirement)，已回退。requirement 垃圾桶问题
    部分不可由 prompt 边界根治,deriver 的 no_sink 已处理其"抢主标"的一面。"""
    from src.agents.agt_email_labeling.src.pipeline.topic_definitions import TopicCode, TOPIC_DISPLAY_MAP
    lines = []
    for tc in TopicCode:
        display = TOPIC_DISPLAY_MAP.get(tc.value, tc.value)
        lines.append(f"- {tc.value}: {display}")
    return "\n".join(lines)


def _render_action_defs() -> str:
    """从 ActionCode + ACTION_DISPLAY_MAP 生成 prompt 中的 action 列表。"""
    from src.agents.agt_email_labeling.src.pipeline.action_definitions import ActionCode, ACTION_DISPLAY_MAP
    lines = []
    for ac in ActionCode:
        display = ACTION_DISPLAY_MAP.get(ac.value, ac.value)
        lines.append(f"- {ac.value}: {display}")
    return "\n".join(lines)


def _render_current_facts_defs() -> str:
    """从 CurrentEmailFacts 字段 + 描述映射生成 prompt 中的 current_facts 列表。"""
    from src.agents.agt_email_labeling.src.schemas import (
        AUTHORITATIVE_ONLY_CURRENT_FACT_FIELDS,
        CurrentEmailFacts,
    )
    lines = []
    for field_name in CurrentEmailFacts.model_fields:
        if field_name in AUTHORITATIVE_ONLY_CURRENT_FACT_FIELDS:
            continue
        desc = _CURRENT_FACTS_PROMPT_DESC.get(field_name, field_name)
        lines.append(f"- {field_name}: {desc}")
    return "\n".join(lines)


def _render_current_facts_example() -> str:
    """从 CurrentEmailFacts 字段生成示例 JSON（所有字段 false）。输出已适配 .format() 双花括号。"""
    from src.agents.agt_email_labeling.src.schemas import (
        AUTHORITATIVE_ONLY_CURRENT_FACT_FIELDS,
        CurrentEmailFacts,
    )
    lines = []
    visible_fields = [
        field_name for field_name in CurrentEmailFacts.model_fields
        if field_name not in AUTHORITATIVE_ONLY_CURRENT_FACT_FIELDS
    ]
    for i, field_name in enumerate(visible_fields):
        comma = "," if i < len(visible_fields) - 1 else ""
        lines.append(f'    "{field_name}": false{comma}')
    return "\n".join(lines)


def _normalize_topic(raw: str) -> str:
    """将 LLM 输出的 topic 变体归一化为标准枚举值。"""
    from src.agents.agt_email_labeling.src.pipeline.topic_definitions import TOPIC_NORMALIZE
    key = raw.strip().lower().replace("-", "_")
    return TOPIC_NORMALIZE.get(key, key)


def _normalize_action(raw: str) -> str:
    """将 LLM 输出的 action 变体归一化为标准枚举值。"""
    from src.agents.agt_email_labeling.src.pipeline.action_definitions import ACTION_NORMALIZE
    key = raw.strip().lower().replace("-", "_")
    return ACTION_NORMALIZE.get(key, key)


# JSON 解析失败重试时追加的强约束（多因输出截断/夹带散文）
_JSON_RETRY_SUFFIX = (
    "\n\n【重要】上一次输出不是合法 JSON。请**只**返回一个完整闭合的 JSON 对象，"
    "不要任何解释文字、前后缀或 Markdown 代码块。若内容较多，可精简 evidence 文本，"
    "但必须保证 JSON 完整且能被解析。"
)


def _extract_json(raw: str) -> str:
    """从 LLM 原始输出中稳健抽取 JSON：剥离 ``` 代码块，截取最外层 {…}，容忍前后散文。"""
    t = raw.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        t = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
    t = t.strip()
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start:end + 1]
    return t


# 值被「\"…\"」（转义引号）整体包裹：原文含引号时 LLM 常把字符串值写成
#   "evidence": \"I'm good.\"   ← 值位置以反斜杠开头 → 非法 JSON（Expecting value）
# 匹配 :  \"  …(非贪婪)…  \"  且其后是值终止符（, } ] 或行尾）
_ESCAPED_QUOTE_VALUE = re.compile(r':\s*\\"(.*?)\\"(?=\s*[,}\]\r\n])', re.S)


def _repair_json(s: str) -> str:
    """对 LLM 常见的可恢复畸形做**确定性**修复（仅在严格解析失败时调用，不碰合法 JSON）。

    覆盖两类高频畸形：
      1. 字符串值被 \"…\"（转义引号）整体包裹 → 还原为合法 JSON 字符串（正确转义内部引号）；
      2. 对象/数组结尾的多余逗号（trailing comma）。
    这不是「降级」：不返回空标签、不吞错，只是把可解析但不规范的输出正确解析出来。
    """
    def _fix_val(m: "re.Match") -> str:
        inner = m.group(1).replace('\\"', '"')   # 内部若仍有 \" 先还原成 "
        return ": " + json.dumps(inner, ensure_ascii=False)  # 再统一正确转义
    s = _ESCAPED_QUOTE_VALUE.sub(_fix_val, s)
    s = re.sub(r",(\s*[}\]])", r"\1", s)          # 去尾逗号
    return s


def _loads_lenient(raw: str) -> dict:
    """先严格解析；失败则对抽取出的 JSON 做一次确定性修复后再解析。"""
    extracted = _extract_json(raw)
    try:
        return json.loads(extracted)
    except json.JSONDecodeError:
        repaired = _repair_json(extracted)
        data = json.loads(repaired)  # 仍失败 → 抛出，保持 fail-loud
        logger.info("JSON 经确定性修复后解析成功（修复转义引号/尾逗号）")
        return data


def build_multi_label_prompt(
    cleaned_body: str,
    sender_type: str,
    thread_context_text: str = "",
    customer_context_text: str = "",
) -> str:
    """组装 Multi-Label 抽取的 user prompt（从 taxonomy 渲染枚举）。

    抽成独立函数：正式流水线与"冻结重放"测试用同一套 prompt，避免两边漂移。
    """
    return _MULTI_LABEL_USER_PROMPT_TEMPLATE.format(
        sender_type=sender_type,
        thread_context_text=thread_context_text or "无历史上下文",
        customer_context_text=customer_context_text or "无客户级上下文",
        cleaned_body=cleaned_body,
        topic_defs=_render_topic_defs(),
        action_defs=_render_action_defs(),
        current_facts_defs=_render_current_facts_defs(),
        current_facts_example=_render_current_facts_example(),
    )


async def multi_label_extract(
    cleaned_body: str,
    sender_type: str,
    thread_context_text: str = "",
    customer_context_text: str = "",
    db: Optional[AsyncSession] = None,
    target_model: Optional[str] = None,
    temperature: float = 0.0,
) -> "MultiLabelResult":
    """V2: Multi-Label LLM 抽取。

    这是唯一会调用 LLM 生成“语义中间层”的地方：
      原文 + 线程/权威上下文 → raw JSON → MultiLabelResult

    这里不做最终主标裁决、不做 topic_cap、不做复核路由；这些都在 orchestrator_v2 的
    deterministic post-processing 里完成，保证后处理可离线重放、可比较、可回滚。
    解析/重试仍失败时 fail-loud，让跑批暴露 failed，而不是静默生成空标签。
    """
    user_prompt = build_multi_label_prompt(
        cleaned_body, sender_type, thread_context_text, customer_context_text
    )

    # 第一次用 3000 token；若 JSON 解析失败（多因超长邮件输出被截断）→ 重试一次：
    # 提高 token 上限 + 追加"只回合法完整 JSON"的强约束。其它异常不重试，直接抛出。
    last_exc: Optional[Exception] = None
    for attempt, max_tok in ((0, 3000), (1, 4500)):
        try:
            llm = await AgentLLM.create_with_key_manager(
                db=db,
                agent_id=_AGENT_ID,
                target_model=target_model,
                system_prompt=_MULTI_LABEL_SYSTEM_PROMPT,
            )
            question = user_prompt if attempt == 0 else user_prompt + _JSON_RETRY_SUFFIX
            raw = await llm.ask(
                question=question,
                temperature=temperature,  # 来自运行时配置（默认 0 可复现）
                max_tokens=max_tok,
            )
            return _parse_multi_label(raw, sender_type=sender_type)
        except json.JSONDecodeError as exc:
            last_exc = exc
            logger.warning("Multi-label JSON 解析失败(第%d次): %s%s",
                           attempt + 1, exc, "；提高 token 重试" if attempt == 0 else "")
            continue

    # 测试阶段：不做降级。重试仍失败 → 直接抛出，让问题在批处理里显形为 failed，
    # 而不是静默返回空标签把截断/异常盖住（等代码真正没问题后再考虑生产容错）。
    raise last_exc if last_exc else RuntimeError("multi_label_extract failed without exception")


def _parse_multi_label(raw: str, sender_type: str = "") -> "MultiLabelResult":
    """解析 LLM 输出的多标签 JSON，并收敛为稳定中间层。

    边界很重要：
      - 只做 JSON 修复、枚举归一化、schema 构造、审计依据解析。
      - 不在这里做复杂业务规则，避免“解析层”悄悄变成第二个分类器。
      - normalize 后不在枚举中的 topic/action 会丢弃，并标记 needs_review。
    """
    from src.agents.agt_email_labeling.src.schemas import (
        ActionItem, CurrentEmailFacts, EntityCollection, EntityDocument, EntityLogistics,
        EntityMeeting, EntityOrder, EntityPayment, EntityPrice, EntityProduct,
        EntityQuantity, MultiLabelResult, TopicItem, DecisionTrace,
    )
    from src.agents.agt_email_labeling.src.pipeline.topic_definitions import (
        TOPIC_DISPLAY_MAP, TopicCode,
    )
    from src.agents.agt_email_labeling.src.pipeline.action_definitions import (
        ACTION_DISPLAY_MAP, ActionCode,
    )

    _VALID_TOPICS = frozenset(tc.value for tc in TopicCode)
    _VALID_ACTIONS = frozenset(ac.value for ac in ActionCode)

    data = _loads_lenient(raw)

    review_reasons: list[str] = []

    # 解析 topics —— 严格校验枚举值
    topics_raw = data.get("topics", [])
    topics: list[TopicItem] = []
    for t in topics_raw:
        if not isinstance(t, dict) or "topic" not in t:
            continue
        code = _normalize_topic(t["topic"])
        if code not in _VALID_TOPICS:
            review_reasons.append(f"unknown topic: {t['topic']!r}→{code!r}")
            _track_unknown("topics", t["topic"], code)
            continue
        topics.append(TopicItem(
            topic=code,
            display=TOPIC_DISPLAY_MAP.get(code, code),
            role=t.get("role", "secondary"),
            # 不再读取 LLM 自评置信度（已从 prompt 移除）；confidence 保持 schema 默认占位。
            evidence=str(t.get("evidence", "")),
        ))

    # 解析 actions —— 严格校验枚举值
    actions_raw = data.get("actions", [])
    actions: list[ActionItem] = []
    for a in actions_raw:
        if not isinstance(a, dict) or "action" not in a:
            continue
        code = _normalize_action(a["action"])
        if code not in _VALID_ACTIONS:
            review_reasons.append(f"unknown action: {a['action']!r}→{code!r}")
            _track_unknown("actions", a["action"], code)
            continue
        actions.append(ActionItem(
            action=code,
            display=ACTION_DISPLAY_MAP.get(code, code),
            topic_ref=a.get("topic_ref"),
            target_refs=list(a.get("target_refs", [])),
            # 不再读取 LLM 自评置信度（已从 prompt 移除）；confidence 保持 schema 默认占位。
            evidence=str(a.get("evidence", "")),
        ))

    # 解析 entities —— 测试阶段不吞异常，结构有问题直接抛出显形
    ents_raw = data.get("entities", {})
    entities = EntityCollection(**{
        "products": [_safe_entity(EntityProduct, e) for e in ents_raw.get("products", [])],
        "prices": [_safe_entity(EntityPrice, e) for e in ents_raw.get("prices", [])],
        "quantities": [_safe_entity(EntityQuantity, e) for e in ents_raw.get("quantities", [])],
        "payments": [_safe_entity(EntityPayment, e) for e in ents_raw.get("payments", [])],
        "logistics": [_safe_entity(EntityLogistics, e) for e in ents_raw.get("logistics", [])],
        "documents": [_safe_entity(EntityDocument, e) for e in ents_raw.get("documents", [])],
        "orders": [_safe_entity(EntityOrder, e) for e in ents_raw.get("orders", [])],
        "meetings": [_safe_entity(EntityMeeting, e) for e in ents_raw.get("meetings", [])],
    })

    # 解析 current_facts —— 只认 schema 定义字段；不吞异常
    cf_raw = data.get("current_facts", {})
    current_facts = CurrentEmailFacts(**{
        k: bool(v) for k, v in cf_raw.items()
        if k in CurrentEmailFacts.model_fields
    })
    decision_trace = _parse_decision_trace(
        data.get("decision_trace") or {},
        topics=topics,
        actions=actions,
        current_facts=current_facts,
    )

    primary_topic = _derive_primary_topic(topics, actions)
    if primary_topic is None and review_reasons:
        review_reasons.append("derived primary_topic is None")

    summary = str(data.get("summary", ""))

    # 限制 raw_llm_output 长度，防止 JSONB 膨胀
    max_raw_len = 5000
    raw_truncated = raw[:max_raw_len] + "..." if len(raw) > max_raw_len else raw

    return MultiLabelResult(
        schema_version="multi_label_v1",
        summary=summary,
        sender_type=sender_type,
        topics=topics,
        actions=actions,
        entities=entities,
        current_facts=current_facts,
        decision_trace=decision_trace,
        primary_topic=primary_topic,
        # confidence 不再来自 LLM 自评；真实置信度由 ConfidenceScorerV2 基于落地信号计算。
        confidence=0.0,
        needs_review=bool(review_reasons),
        review_reason="; ".join(review_reasons) if review_reasons else "",
        raw_llm_output=raw_truncated,
    )


def _parse_decision_trace(raw: dict, topics: list, actions: list,
                          current_facts) -> "DecisionTrace":
    """解析模型显式输出的可审计依据；旧模型未输出时生成兜底摘要。"""
    from src.agents.agt_email_labeling.src.schemas import (
        AUTHORITATIVE_ONLY_CURRENT_FACT_FIELDS,
        DecisionEvidenceItem, DecisionRejectedCandidate, DecisionRuleTrace, DecisionTrace,
    )

    if not isinstance(raw, dict):
        raw = {}

    evidence_map = []
    for item in raw.get("evidence_map") or []:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("label_type") or "") == "fact"
            and str(item.get("label") or "") in AUTHORITATIVE_ONLY_CURRENT_FACT_FIELDS
        ):
            continue
        evidence_map.append(DecisionEvidenceItem(
            label_type=str(item.get("label_type") or ""),
            label=str(item.get("label") or ""),
            evidence=str(item.get("evidence") or "")[:500],
            why=str(item.get("why") or "")[:300],
        ))

    if not evidence_map:
        for action in actions[:8]:
            if getattr(action, "evidence", ""):
                evidence_map.append(DecisionEvidenceItem(
                    label_type="action",
                    label=action.action,
                    evidence=action.evidence[:500],
                    why=f"原文证据支撑动作「{action.display or action.action}」",
                ))
        for topic in topics[:6]:
            if getattr(topic, "evidence", ""):
                evidence_map.append(DecisionEvidenceItem(
                    label_type="topic",
                    label=topic.topic,
                    evidence=topic.evidence[:500],
                    why=f"原文证据支撑主题「{topic.display or topic.topic}」",
                ))

    rejected = []
    for item in raw.get("rejected_candidates") or []:
        if not isinstance(item, dict):
            continue
        rejected.append(DecisionRejectedCandidate(
            label=str(item.get("label") or "")[:120],
            reason=str(item.get("reason") or "")[:300],
        ))

    rule_overrides = []
    for item in raw.get("rule_overrides") or []:
        if not isinstance(item, dict):
            continue
        rule_overrides.append(DecisionRuleTrace(
            rule=str(item.get("rule") or "")[:120],
            effect=str(item.get("effect") or "")[:300],
        ))

    primary_topic = next((t for t in topics if getattr(t, "role", "") == "primary"), None) or (topics[0] if topics else None)
    primary_action = actions[0] if actions else None

    return DecisionTrace(
        primary_topic_reason=str(raw.get("primary_topic_reason") or (
            f"主主题来自邮件最核心业务意图：{getattr(primary_topic, 'display', '') or getattr(primary_topic, 'topic', '')}"
            if primary_topic else "未识别到明确主题"
        ))[:400],
        primary_action_reason=str(raw.get("primary_action_reason") or (
            f"主动作来自最明确的发件人动作：{getattr(primary_action, 'display', '') or getattr(primary_action, 'action', '')}"
            if primary_action else "未识别到明确动作"
        ))[:400],
        evidence_map=evidence_map,
        rejected_candidates=rejected[:5],
        rule_overrides=rule_overrides[:8],
    )


def _safe_entity(model_class, raw_dict: dict):
    """构造实体，过滤掉不在 model_fields 中的键。

    测试阶段不吞异常：非 dict 或字段类型不合法直接抛出，让 LLM 输出问题显形。
    """
    if not isinstance(raw_dict, dict):
        raise ValueError(f"{model_class.__name__} 期望 dict，得到 {type(raw_dict).__name__}: {raw_dict!r}")
    filtered = {k: v for k, v in raw_dict.items() if k in model_class.model_fields}
    return model_class(**filtered)


# 残差/catch-all 主题：低特异度，有具体主题时不当主标（见 _derive_primary_topic）。
_PRIMARY_SINK_TOPICS = {"requirement_discussion", "follow_up", "no_action_reply"}


def _derive_primary_topic(topics: list, actions: list) -> Optional[str]:
    """派生 primary_topic。

    关键设计：
    1. **不盲信 LLM 的 role=primary / topic_ref**（均为判断类输出，不采信）。
    2. **尊重动作方向**：先取"被动作的规范归属(ACTION_TOPIC_MAP)支撑的主题"，在这些主题间
       按 TOPIC_PRIORITY 取最高；当没有主题被动作支撑时，回退到全体主题按 TOPIC_PRIORITY。

    设计取舍（实测决定，非理念）：曾试过"纯动作驱动(取最高重要度动作的归属主题)"，但在
    golden 集上准确率反而下降 2.8pt——弱动作(short_reply/other)或动作重要度排序偏差会盖过
    本该保留的已列业务主题。故保留此"动作支撑 + TOPIC_PRIORITY 回退"的混合逻辑（实测更优）。
    主题准确率的真正瓶颈在 LLM 抽取(动作/主题误抽)，不在派生。
    """
    from src.agents.agt_email_labeling.src.pipeline.topic_definitions import TOPIC_PRIORITY
    from src.agents.agt_email_labeling.src.pipeline.action_definitions import (
        ACTION_PRIORITY, ACTION_TOPIC_MAP,
    )

    supported = {ACTION_TOPIC_MAP.get(a.action) for a in actions if ACTION_TOPIC_MAP.get(a.action)}

    if topics:
        supported_topics = [t for t in topics if t.topic in supported]
        pool = supported_topics or topics
        # 残差/catch-all 主题（泛泛需求沟通 / 催跟进 / 无动作回复）语义低特异度，不与具体业务
        # 主题争主标：当有具体主题被支撑时，catch-all 仅作背景。离线 deriver gate 验证：
        # 全量 +1.1pt primary、recall 不降、时间/客户两留出零回归（见 scripts/deriver_simulator.py）。
        non_sink = [t for t in pool if t.topic not in _PRIMARY_SINK_TOPICS]
        pool = non_sink or pool
        return max(pool, key=lambda t: TOPIC_PRIORITY.get(t.topic, 0)).topic

    if actions:
        best_action = max(actions, key=lambda a: ACTION_PRIORITY.get(a.action, 0))
        return ACTION_TOPIC_MAP.get(best_action.action)

    return None


# 注：整体置信度不再由 LLM 自评聚合得出；改由 ConfidenceScorerV2 基于落地信号（证据覆盖/
# 实体支撑/动作对齐/话题清晰度）计算，见 pipeline/confidence_scorer.py。
