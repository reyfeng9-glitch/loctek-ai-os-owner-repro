"""Stage 0: SystemFilter — 在进入 LLM 前规则过滤系统类邮件。

命中以下情形直接短路，跳过后续流水线，由 orchestrator 落库为 system_noise：
- 空正文 / 清洗后过短
- 退信 / 投递失败（NDR）
- 已读回执（read receipt）—— 纯噪声
- 自动回复（Out of Office / 休假 / 自动应答）

判定策略（两层）
================
1. **结构化为主（语言无关）**：
   - 空正文（< 15 字符）。
   - SMTP 错误码（如 550 5.x.x）。
   - 已读回执：正文是"邮件头回显"且几乎无业务内容（短 + 含头部字段）。
2. **小语种词表兜底**：结构化覆盖不到的本地化措辞，
   统一来自 `lexicons.py`（AUTO_REPLY_MARKERS / READ_RECEIPT_MARKERS /
   READ_RECEIPT_BODY_OPENERS / BOUNCE_MARKERS）。新增语言只改 lexicons.py。
"""
import re
from typing import Optional

from src.agents.agt_email_labeling.src.pipeline.lexicons import (
    AUTO_REPLY_MARKERS,
    BOUNCE_MARKERS,
    READ_RECEIPT_BODY_OPENERS,
    READ_RECEIPT_MARKERS,
)
from src.agents.agt_email_labeling.src.schemas import FilterResult


def _markers_pattern(markers) -> re.Pattern:
    """把词表编成一个大小写无关的 alternation 正则。"""
    return re.compile("|".join(re.escape(m) for m in markers), re.IGNORECASE)


_AUTO_REPLY_RE = _markers_pattern(AUTO_REPLY_MARKERS)
_BOUNCE_RE = _markers_pattern(BOUNCE_MARKERS)
_READ_RECEIPT_SUBJECT_RE = _markers_pattern(READ_RECEIPT_MARKERS)
_READ_RECEIPT_BODY_RE = _markers_pattern(READ_RECEIPT_BODY_OPENERS)

# 结构化信号（语言无关）
_SMTP_ERROR_RE = re.compile(r"\b5\d{2}\s+5\.\d\.\d\b")  # SMTP 5xx 5.x.x
# 已读回执正文常见的"邮件头回显"字段（任一语言的 To/Subject/Sent 行模式）
_HEADER_ECHO_RE = re.compile(
    r"^\s*[^\s:][^:\n]{0,24}:\s*\S",  # 行形如 "标签: 值"
    re.MULTILINE,
)

_MIN_BODY_LEN = 15
_READ_RECEIPT_MAX_LEN = 400  # 已读回执正文通常很短


def system_filter(cleaned_body: Optional[str], subject: Optional[str]) -> Optional[FilterResult]:
    """检测系统类邮件。命中返回 FilterResult，否则返回 None 进入正常流水线。

    顺序：空正文 → 退信 → 已读回执 → 自动回复。
    退信先于自动回复（退信文案可能含"away/out"等误触词）。
    """
    body = (cleaned_body or "").strip()
    subj = (subject or "").strip()

    # 1. 真·空正文（结构化、语言无关）：无任何字母/数字/CJK（纯标点/emoji/空白）→ 噪声。
    #    注意：像 "Paid"、"已付"、"OK" 这类极短但**有实义**的内容会放行给 LLM
    #    （付款确认/客套短回复等由 LLM + 规则判定），不再因"过短"被一刀切当空邮件。
    if not re.search(r"[^\W_]", body, re.UNICODE):
        return FilterResult(topic="system_noise", sub_action="empty", reason="no_meaningful_content")

    combined = f"{subj}\n{body}"

    # 2. 退信 / SMTP 错误码（结构化 + 词表）
    m = _SMTP_ERROR_RE.search(combined)
    if m:
        return FilterResult(topic="system_noise", sub_action="bounce", reason=f"smtp_error: {m.group(0)[:60]}")
    m = _BOUNCE_RE.search(combined)
    if m:
        return FilterResult(topic="system_noise", sub_action="bounce", reason=f"bounce_marker: {m.group(0)[:60]}")

    # 3. 已读回执（结构化 + 词表）
    receipt = _detect_read_receipt(subj, body)
    if receipt:
        return receipt

    # 4. 自动回复（词表兜底）
    m = _AUTO_REPLY_RE.search(combined)
    if m:
        return FilterResult(topic="system_noise", sub_action="auto_reply", reason=f"auto_reply_marker: {m.group(0)[:60]}")

    return None


def _detect_read_receipt(subj: str, body: str) -> Optional[FilterResult]:
    """已读回执判定：

    A) 主题以 read-receipt 措辞开头（"Read:" / "Elolvasva:" / "Gelezen:" …），或
    B) 结构化：正文很短 + 以"邮件头回显措辞"开头（"Your message" / "Uw bericht" …）
       或正文几乎只剩"标签: 值"头部行（无成段正文）。
    """
    # A) 主题措辞
    if subj and _READ_RECEIPT_SUBJECT_RE.match(subj.lower()):
        return FilterResult(topic="system_noise", sub_action="read_receipt", reason="read_receipt_subject")

    body_lower = body.lower()
    # B) 正文头回显措辞（多语言词表）
    if any(body_lower.startswith(op) for op in (o.lower() for o in READ_RECEIPT_BODY_OPENERS)):
        return FilterResult(topic="system_noise", sub_action="read_receipt", reason="read_receipt_body_opener")

    # B') 纯结构化：短正文且主要由"标签: 值"头部行构成、无成段正文
    if len(body) <= _READ_RECEIPT_MAX_LEN:
        lines = [ln for ln in body.split("\n") if ln.strip()]
        if lines:
            header_like = sum(1 for ln in lines if _HEADER_ECHO_RE.match(ln))
            prose = sum(1 for ln in lines if len(ln.strip()) > 60)
            if header_like >= 2 and prose == 0 and header_like >= len(lines) * 0.6:
                return FilterResult(
                    topic="other", sub_action="read_receipt", reason="read_receipt_header_echo"
                )
    return None
