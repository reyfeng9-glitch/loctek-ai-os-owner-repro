import re
from typing import List, Optional, Tuple

from src.agents.agt_email_labeling.src.pipeline.lexicons import (
    ATTRIBUTION_VERBS,
    SIGNATURE_OPENERS,
)
from src.agents.agt_email_labeling.src.schemas import CleaningMeta, CleaningResult

# 历史引用检测模式（保留：英文 / 荷兰文等显式 header；通用判定见下方结构化检测）
_QUOTE_PATTERNS = [
    re.compile(r"^On .+wrote:\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^>.*$", re.MULTILINE),
    re.compile(r"-{3,}\s*Original Message\s*-{3,}", re.IGNORECASE),
    re.compile(r"-{3,}\s*Doorsluis(t|bericht)\s*-{3,}", re.IGNORECASE),  # Dutch: forwarded message
    re.compile(r"-{3,}Forwarded by.*-{3,}", re.IGNORECASE),
    re.compile(r"From:\s*.+\nSent:\s*.+\nTo:\s*.+\nSubject:", re.IGNORECASE),
    # Dutch headers: Van: ... Verzonden: ... Aan: ... Onderwerp:
    re.compile(r"Van:\s*.+\nVerzonden:\s*.+\nAan:\s*.+\nOnderwerp:", re.IGNORECASE),
    re.compile(r"_{3,}\s*\n.*From:.*\n.*Sent:", re.IGNORECASE | re.DOTALL),
]

# ── 语言无关的结构化引用检测 ──────────────────────────────────
# 思路：被引用邮件的头部，无论什么语言，都是若干行 "标签: 值"，
# 且整块里同时出现邮箱地址 + 日期。命中即视为引用块起点，不认具体语言词。
_HEADER_LABEL_LINE = re.compile(r"^\s*[^\s:][^:\n]{0,32}:\s*\S")
_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w{2,}")
# 日期判定收紧：真实年份(19xx/20xx) / 时间(HH:MM) / 带分隔符日期 / 月份名 / 星期名，
# 避免电话号码、型号、数量里的 4 位数被误当日期。
_DATE_HINT = re.compile(
    r"(\b(?:19|20)\d{2}\b|\b\d{1,2}:\d{2}\b|\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b"
    r"|\b\d{1,2}\s*(?:am|pm)\b)",
    re.IGNORECASE,
)
# 归属行（"<某人/日期> <动词>:"）—— 动词来自集中维护的多语言词表
_ATTRIBUTION_LINE = re.compile(
    r"^.{0,200}?\b(?:" + "|".join(re.escape(v) for v in ATTRIBUTION_VERBS) + r")\b\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# 签名里的联系方式标签（Tel/Fax/Mobile 等）——头簇含这些则判为签名而非引用头
_SIG_CONTACT_LABEL = re.compile(
    r"^\s*(?:tel|telephone|mobile|mob|phone|fax|whatsapp|wechat|skype|web|www|e-?mail)\b",
    re.IGNORECASE,
)


def _find_structural_quote_start(body: str) -> int:
    """扫描语言无关的"被引用邮件头簇"。返回引用块起点偏移；无则返回 len(body)。

    收紧判定（避免误吃签名）：从某行起连续 ≥3 行匹配 "标签: 值"，
    窗口内同时出现邮箱 + 真日期，且不含 Tel/Fax/Mobile 等签名联系方式标签。
    （真实邮件头是 From/Sent/To/Subject 等价的 3~4 行，而签名是 Tel/Fax/Email 块。）
    """
    lines = body.split("\n")
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    n = len(lines)
    for i in range(n):
        labels = has_email = has_date = sig_label = 0
        j = i
        while j < n and j < i + 8:
            ln = lines[j]
            if _HEADER_LABEL_LINE.match(ln):
                labels += 1
                has_email = has_email or bool(_EMAIL_RE.search(ln))
                has_date = has_date or bool(_DATE_HINT.search(ln))
                sig_label = sig_label or bool(_SIG_CONTACT_LABEL.match(ln))
                j += 1
            else:
                break
        if labels >= 3 and has_email and has_date and not sig_label:
            return offsets[i]
    return len(body)

# 内联图片占位符
_CID_IMAGE_RE = re.compile(r"\[?cid:image[^\]]*\]?", re.IGNORECASE)

# 签名检测模式（限制 DOTALL 最大匹配长度，避免吃掉内联回复内容）
# 问候语起始词来自集中维护的多语言词表（lexicons.SIGNATURE_OPENERS）。
_SIGNATURE_OPENER_PATTERN = re.compile(
    r"\n\s*(?:" + "|".join(re.escape(s) for s in SIGNATURE_OPENERS) + r")[,.!]?\s*\n.{0,300}",
    re.DOTALL | re.IGNORECASE,
)
_SIGNATURE_PATTERNS = [
    re.compile(r"\n--\s*\n.{0,300}", re.DOTALL),
    _SIGNATURE_OPENER_PATTERN,
]

# 语言无关的"联系方式信号"——用于按密度识别尾部签名块（电话/邮箱/URL/tel-fax 等）
_CONTACT_SIGNAL = re.compile(
    r"(\b(?:tel|telephone|mobile|mob|phone|fax|whatsapp|wechat|skype|e-?mail|www)\b"
    r"|https?://|www\."
    r"|[\w.\-+]+@[\w.\-]+\.\w{2,}"
    r"|[+(]\d[\d\-\s().]{6,}\d)",
    re.IGNORECASE,
)

# 免责声明检测模式
_DISCLAIMER_PATTERNS = [
    re.compile(r"CONFIDENTIALITY NOTICE.*", re.DOTALL | re.IGNORECASE),
    re.compile(r"This (e-?mail|message) (and any attachments?|is intended).*", re.DOTALL | re.IGNORECASE),
    re.compile(r"DISCLAIMER:.*", re.DOTALL | re.IGNORECASE),
    re.compile(r"The information (contained|transmitted).*confidential.*", re.DOTALL | re.IGNORECASE),
]

# 内联回复检测（i put my answers below / answers inline / see below）
_INLINE_REPLY_PATTERNS = [
    re.compile(r"\bput\s+my\s+(answers?|response)\s+(below|inline)", re.IGNORECASE),
    re.compile(r"\b(answers?|response)\s+(are\s+)?(below|inline)", re.IGNORECASE),
    re.compile(r"\bfind\s+(my\s+)?(answers?|response)\s+(below|inline)", re.IGNORECASE),
    re.compile(r"\bsee\s+(my\s+)?(answers?|response)\s+(below|inline)", re.IGNORECASE),
    re.compile(r"answers?\s+in\s+the\s+e-?mail\s+below", re.IGNORECASE),
]

# 在引用段中标记「嵌套引用开始」的模式（遇到这些就不再属于内联回复）
_MULTI_QUOTE_START = re.compile(
    r"^(?:"
    r">\s*>"                            # 2层或更深引用 (>>, > >, >>> 等)
    r"|>\s*On\s+[^\n]+\s+wrote:\s*$"   # > On ... wrote:
    r"|>\s*-{3,}\s*(?:Original Message|Doorsluis)"  # > --- Original Message / Doorsluis
    r"|>\s*Van:\s+"                     # > Van: (Dutch From)
    r"|>\s*From:\s+"                    # > From:
    r"|>\s*_{3,}"                       # > ___ followed by From
    r")",
    re.IGNORECASE | re.MULTILINE,
)

_MIN_BODY_LENGTH = 10
_OVER_CLEAN_RATIO = 0.20
_OVER_CLEAN_MIN_ORIGINAL = 100


_MAX_CLEANED_LEN = 6000  # 清洗后正文封顶字符数（超过则截断头部，防 LLM 输出截断）


def clean_email(raw_body: str) -> CleaningResult:
    """清洗邮件正文，将历史引用折叠为简短引用标记，移除签名和免责声明。"""
    body = raw_body
    removed: List[str] = []

    # 0. 移除内联图片占位符（cid:image）
    body, _ = _CID_IMAGE_RE.subn("", body)

    # 1. 折叠历史引用为简短标记（如 [引用 John 2024-01-15 的邮件]）
    body, collapsed = _collapse_quoted_reply(body)
    if collapsed:
        removed.append("quoted_reply_collapsed")

    # 2. 移除签名（基于换行结构）
    body, signed = _remove_signature(body)
    if signed:
        removed.append("signature")

    # 2b. 无空行邮件的签名兜底（部分客户端把正文+签名压成单行）：
    #     sentinel + 紧随联系方式 → 强制切除尾部签名块
    body, signed_inline = _remove_inline_signature(body)
    if signed_inline:
        removed.append("signature_inline")

    # 3. 移除免责声明
    body, disclaimed = _remove_disclaimer(body)
    if disclaimed:
        removed.append("disclaimer")

    cleaned = body.strip()

    # 4. 超长封顶：折叠历史后仍过长（如内联回复夹大量历史、表格类邮件）→ 截断保留头部。
    #    当前邮件的新内容通常在顶部，截断尾部冗余/残留历史，避免 LLM 输出超 max_tokens
    #    被截断导致 JSON 解析失败（fail-loud 下会直接失败）。
    if len(cleaned) > _MAX_CLEANED_LEN:
        cleaned = cleaned[:_MAX_CLEANED_LEN].rstrip() + "\n…[正文过长，已截断冗余/历史部分]"
        removed.append("body_capped")

    quality = _assess_quality(cleaned, raw_body)

    return CleaningResult(
        cleaned_body=cleaned,
        meta=CleaningMeta(removed_sections=removed, quality=quality),
    )


def _extract_quote_reference(header: str) -> str:
    """从引用头部提取信息，生成简短的中文描述。"""
    # "On [date], [name] wrote:" → 引用 <name> <date> 的邮件
    m = re.search(r"^On\s+(.+?),\s*(.+?)\s+wrote:", header, re.MULTILINE | re.IGNORECASE)
    if m:
        name = m.group(2).strip()
        date = _shorten_date_str(m.group(1).strip())
        return f"引用 {name} {date} 的邮件"
    # "Van: ... Verzonden: ..." (Dutch)
    m = re.search(r"Van:\s*(.+?)[\r\n]+Verzonden:\s*(.+?)[\r\n]", header, re.IGNORECASE)
    if m:
        sender = m.group(1).strip()
        date = _shorten_date_str(m.group(2).strip())
        return f"引用 {sender} {date} 的邮件"
    # "From: ... Sent: ..." (English)
    m = re.search(r"From:\s*(.+?)[\r\n]+Sent:\s*(.+?)[\r\n]", header, re.IGNORECASE)
    if m:
        sender = m.group(1).strip()
        date = _shorten_date_str(m.group(2).strip())
        return f"引用 {sender} {date} 的邮件"
    return "历史邮件引用"


# 常见长日期模式的精简映射
_DATE_SHORTENERS = [
    (re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)"),
     lambda m: m.group(1)[:3]),
    (re.compile(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,\s]*,?"), lambda m: ""),
]


def _shorten_date_str(raw: str) -> str:
    for pat, fn in _DATE_SHORTENERS:
        raw = pat.sub(fn, raw)
    return " ".join(raw.split()).strip()


def _find_earliest_quote(body: str) -> int:
    """找到最早的显式引用标记位置（仅 _QUOTE_PATTERNS）。

    内联回复路径依赖本函数的"保守"语义，故不在此混入结构化/归属行检测；
    语言无关的结构化引用检测仅用于普通分支（见 _normal_quote_start）。
    """
    earliest_pos = len(body)
    for pattern in _QUOTE_PATTERNS:
        m = pattern.search(body)
        if m and m.start() < earliest_pos:
            earliest_pos = m.start()
    return earliest_pos


def _normal_quote_start(body: str) -> int:
    """普通（非内联回复）分支用：显式标记 + 多语言归属行 + 语言无关结构化头簇 取最早。"""
    earliest_pos = _find_earliest_quote(body)
    m = _ATTRIBUTION_LINE.search(body)
    if m and m.start() < earliest_pos:
        earliest_pos = m.start()
    struct_pos = _find_structural_quote_start(body)
    if struct_pos < earliest_pos:
        earliest_pos = struct_pos
    return earliest_pos


def _collapse_quoted_reply(body: str) -> Tuple[str, bool]:
    """将历史引用折叠为简短标记，而非直接移除。

    如检测到 i put my answers below 等内联回复模式：
    - 保留单层 > 的内联回复内容
    - 仅折叠多层引用（>> 及更深）
    """
    if not body:
        return body, False

    # 内联回复模式：保留单层 >，折叠深层引用
    # 只检查正文前 5 行（内联回复标记通常在开头），避免正文中间的 "answers below" 误触发
    first_lines = "\n".join(body.split("\n")[:5])
    if any(p.search(first_lines) for p in _INLINE_REPLY_PATTERNS):
        return _collapse_inline_reply(body)

    # ── 普通模式：折叠所有引用 ──
    earliest_pos = _normal_quote_start(body)
    if earliest_pos >= len(body):
        return body, False

    # 上一段落收尾 + 引用头部（约 200 字符）用于提取元信息
    ctx_start = max(0, earliest_pos - 40)
    header_sample = body[ctx_start:earliest_pos + 200]
    quote_ref = _extract_quote_reference(header_sample)

    before_quote = body[:earliest_pos].rstrip()
    return f"{before_quote}\n\n[{quote_ref}]", True


def _collapse_inline_reply(body: str) -> Tuple[str, bool]:
    """内联回复模式：保留内联回复内容，折叠深层引用。

    支持两种格式：
    1. > 引用格式：保留单层 > 内容，从 >> 或 > On ... wrote: 处折叠
    2. 转发格式：跳过头部的邮件头（Van:/From: 等），保留到下一个引用标记之前
    """
    gt_m = re.search(r"^>", body, re.MULTILINE)

    if gt_m:
        return _collapse_inline_gt(body)

    # ── 转发邮件格式（无 > 行）──
    earliest_pos = _find_earliest_quote(body)
    if earliest_pos >= len(body):
        return body, False

    before_quote = body[:earliest_pos].rstrip()

    # 找第二个引用标记（首个之后最近的那个，至少间隔 10 字符避免自匹配）
    second_pos = len(body)
    for pattern in _QUOTE_PATTERNS:
        for m in pattern.finditer(body):
            p = m.start()
            if p > earliest_pos + 10 and p < second_pos:
                second_pos = p

    if second_pos < len(body):
        # 有更深引用：保持两个引用标记之间的内容（去掉邮件头）
        inline_raw = body[earliest_pos:second_pos]
        inline_content = _strip_email_headers(inline_raw).strip()
        deeper = body[second_pos:]
        ref = _extract_forwarded_reference(deeper) or _extract_quote_reference(
            deeper[:300]
        ) or "历史邮件引用"
        if inline_content:
            return f"{before_quote}\n\n{inline_content}\n\n[{ref}]", True
        return f"{before_quote}\n\n[{ref}]", True

    # 仅一个引用标记：去掉邮件头后全部保留
    inline_content = _strip_email_headers(body[earliest_pos:]).strip()
    if inline_content:
        return f"{before_quote}\n\n{inline_content}", True
    return f"{before_quote}", False


def _collapse_inline_gt(body: str) -> Tuple[str, bool]:
    """内联回复 — > 引用格式：保留单层 >，折叠 >> 及更深。"""
    earliest_pos = _find_earliest_quote(body)
    if earliest_pos >= len(body):
        return body, False

    before_quote = body[:earliest_pos].rstrip()
    quoted_section = body[earliest_pos:]

    # 在引用段中找第一个 > 行（跳过 Van:/From: 等非 > 头）
    gt_inner = re.search(r"^>", quoted_section, re.MULTILINE)
    if not gt_inner:
        # 退化
        ref = _extract_quote_reference(body[max(0, earliest_pos - 40) : earliest_pos + 200])
        return f"{before_quote}\n\n[{ref}]", True

    inline_region = quoted_section[gt_inner.start() :]

    # 找多层引用边界
    boundary = _find_inline_boundary(inline_region)
    if boundary is None:
        return f"{before_quote}\n\n{inline_region.strip()}", True
    if boundary <= 0:
        return f"{before_quote}\n\n[{_extract_inline_reference(quoted_section) or '历史邮件引用'}]", True

    inline_content = inline_region[:boundary].strip()
    deeper = inline_region[boundary:]
    ref = _extract_inline_reference(deeper) or "历史邮件引用"
    if inline_content:
        return f"{before_quote}\n\n{inline_content}\n\n[{ref}]", True
    return f"{before_quote}\n\n[{ref}]", True


def _find_inline_boundary(quoted: str) -> Optional[int]:
    """在引用段中扫描第一个多层引用标记的位置。

    多层引用标记包括：>>/>>> 行、> On ... wrote:、> From:、> --- Original Message 等。
    返回该边界在原字符串中的绝对偏移，无匹配则返回 None。
    """
    for m in _MULTI_QUOTE_START.finditer(quoted):
        return m.start()
    return None


_EMAIL_HEADER_LINE = re.compile(
    r"^(?:"
    r"Van:|Verzonden:|Aan:|Onderwerp:|"
    r"From:|Sent:|To:|Subject:|Cc:|Date:|"
    r"Reply-To:|MIME-Version:|Content-Type:|Content-Transfer-Encoding:|"
    r"DKIM-Signature:|Received:|Message-ID:|In-Reply-To:|References:|Thread-Index:|"
    r"Return-Path:|Delivery-date:|Authentication-Results:|"
    r"X-[a-zA-Z-]+:|"
    r"-{3,}\s*(?:Original Message|Doorsluis|Forwarded)\s*-{3,}|"
    r"_{3,}\s*$|"
    r"\[cid:[^\]]*\]"
    r")",
    re.IGNORECASE,
)


def _strip_email_headers(text: str) -> str:
    """去掉开头的邮件头行（Van:/From:/Subject: 等），含 RFC 2822 续行。"""
    lines = text.split("\n")
    cut = 0
    in_header = False
    for line in lines:
        stripped = line.strip()
        if _EMAIL_HEADER_LINE.match(stripped):
            cut += len(line) + 1
            in_header = True
        elif stripped == "":
            cut += len(line) + 1
            in_header = False
        elif in_header and (line.startswith(" ") or line.startswith("\t")):
            cut += len(line) + 1
        else:
            break
    return text[cut:]


def _extract_inline_reference(text: str) -> str:
    """从深层引用内容中提取引用来源（如引用者姓名、日期）。"""
    m = re.search(r'>\s*On\s+(.+?),\s*(.+?)\s+wrote:', text[:300], re.IGNORECASE)
    if m:
        name = m.group(2).strip()
        date = _shorten_date_str(m.group(1).strip())
        return f"引用 {name} {date} 的邮件"
    m = re.search(r'>\s*Van:\s*(.+?)\s*Verzonden:', text[:300], re.IGNORECASE)
    if m:
        return f"引用 {m.group(1).strip()} 的邮件"
    m = re.search(r'>\s*From:\s*(.+?)\s*Sent:', text[:300], re.IGNORECASE)
    if m:
        return f"引用 {m.group(1).strip()} 的邮件"
    return "历史邮件引用"


def _extract_forwarded_reference(text: str) -> Optional[str]:
    """从转发格式深层引用中提取引用来源。"""
    m = re.search(r'Van:\s*(.+?)\s*Verzonden:', text[:300], re.IGNORECASE)
    if m:
        return f"引用 {m.group(1).strip()} 的邮件"
    m = re.search(r'From:\s*(.+?)\s*(?:Sent:|Date:)', text[:300], re.IGNORECASE)
    if m:
        return f"引用 {m.group(1).strip()} 的邮件"
    m = re.search(r'On\s+(.+?),\s*(.+?)\s+wrote:', text[:300], re.IGNORECASE)
    if m:
        return f"引用 {m.group(2).strip()} {_shorten_date_str(m.group(1).strip())} 的邮件"
    return None


# 「无空行邮件」的签名兜底切割：
# 某些邮件客户端（移动端 / 部分 OWA）会把正文 + 签名压成一段无换行的长文本，
# 例如 "...thanks.Best RegardsBruce JiangSales SpecialistPH：+86..."。
# 上面 _remove_signature 依赖换行结构，对这种邮件无效；这里基于「sentinel + 紧随联系方式」做兜底。
#
# 规则：
#   1) 在 cleaned body 中扫描 _INLINE_SIGNATURE_SENTINELS 命中点。
#   2) 命中点之后必须 80 字符内出现至少一个联系方式信号（电话/邮箱/URL/职位关键词），
#      避免把客户的"Best regards, please send me the price."误切。
#   3) 命中点保留为字符 index：从命中起到结尾被视为签名整体丢弃。
#   4) 命中点之前必须仍有 ≥30 字符正文（防止整封被吃掉）。
_INLINE_SIGNATURE_SENTINELS = re.compile(
    r"(?ix)"
    r"(?<![A-Za-z])"                                  # 左边不是字母（避免 "thebest regards" 这种）
    r"("
    r"best\s+regards|kind\s+regards|warm\s+regards|"
    r"with\s+regards|best\s+wishes|sincerely|"
    r"yours\s+(?:truly|sincerely|faithfully)|"
    r"thanks\s+and\s+regards|"
    r"med\s+venlig\s+hilsen|"
    r"mit\s+freundlichen\s+gr(?:ü|u)(?:ß|ss)en|viele\s+gr(?:ü|u)(?:ß|ss)e|"
    r"cordialement|cordiali\s+saluti|"
    r"üdv(?:özlettel)?|tisztelettel"
    r")"
)

# 命中点之后必须看到的"职位/联系方式"特征（任意一个出现即视为签名块）
_INLINE_SIGNATURE_TAIL = re.compile(
    r"(?ix)"
    r"(?:sales\s+(?:specialist|manager|representative|director|engineer)"
    r"|account\s+(?:manager|executive|director)"
    r"|business\s+development"
    r"|managing\s+director|general\s+manager|director"
    r"|tel[:.]?|telephone[:.]?|mobile[:.]?|mob[:.]?|phone[:.]?|"
    r"office[:.]?\s*\+?\d|ph[ÿ:：:.]?\s*\+?\d|fax[:.]?|"
    r"whatsapp|wechat|skype|"
    r"\+?\d[\d\-\s().]{6,}\d|"                       # 长串电话
    r"[\w.\-+]+@[\w.\-]+\.\w{2,}|"                   # 邮箱
    r"www\.[\w.\-]+|https?://[\w.\-]+"
    r")"
)


def _remove_inline_signature(body: str) -> Tuple[str, bool]:
    """无空行邮件的签名兜底切割。当 _remove_signature 失败时使用。

    防误切：sentinel 后 80 字符内必须存在职位/联系方式特征；切点前必须保留 ≥30 字符正文。
    """
    if not body:
        return body, False
    # 选择最早出现且后续含联系方式特征的 sentinel
    cut_at: Optional[int] = None
    for m in _INLINE_SIGNATURE_SENTINELS.finditer(body):
        start = m.start()
        if start < 30:
            # 正文太短，可能整封都是签名 / 假阳性，跳过这个 sentinel
            continue
        window = body[m.end(): m.end() + 80]
        if _INLINE_SIGNATURE_TAIL.search(window):
            cut_at = start
            break
    if cut_at is None:
        return body, False
    return body[:cut_at].rstrip(), True


def _remove_signature(body: str) -> Tuple[str, bool]:
    """剥签名。三条互补路径 + 统一安全护栏（靠结构而非位置）：

    a) "以问候语开头的短行"（多语言词表，含 "Med venlig hilsen/Best regards/..." 这类组合问候）。
    b) "-- " 分隔线。
    c) 兜底：尾部"联系方式块"（电话/邮箱/URL 密集，底部锚定向上扩展）。
    安全护栏：截断点之后不得出现"成段新正文"（>60 字符、无联系方式、以句末标点结尾），
    否则放弃（避免吃掉内联回复正文）。
    """
    lines = body.split("\n")
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1

    cut: Optional[int] = None
    # a) 问候语开头的短行
    for i, ln in enumerate(lines):
        s = ln.strip()
        low = s.lower()
        if s and len(s) < 60 and any(low.startswith(op) for op in _SIGNATURE_OPENER_LOWER):
            cut = offsets[i]
            break
    # b) "-- " 分隔线
    if cut is None:
        m = re.search(r"\n--\s*\n", body)
        if m:
            cut = m.start()
    # c) 联系方式块兜底
    if cut is None:
        cut = _contact_block_start(body)

    if cut is None or cut <= 0:
        return body, False

    # 安全护栏：截断点后若仍有"成段新正文"，放弃剥离
    for x in body[cut:].split("\n"):
        t = x.strip()
        if len(t) > 60 and not _CONTACT_SIGNAL.search(t) and t[-1:] in _SENTENCE_END:
            return body, False
    return body[:cut].rstrip(), True


_SENTENCE_END = ".!?。！？:"
_SIGNATURE_OPENER_LOWER = tuple(s.lower() for s in SIGNATURE_OPENERS)


def _is_signature_line(ln: str) -> bool:
    """判断一行是否属于签名块（空行 / 联系方式 / 问候语 / 短姓名职位行）。"""
    s = ln.strip()
    if not s:
        return True
    if _CONTACT_SIGNAL.search(s):
        return True
    low = s.lower()
    if any(op in low for op in _SIGNATURE_OPENER_LOWER):
        return True
    # 短行且不像完整句子（姓名 / 职位 / 公司行）
    if len(s) <= 45 and s[-1] not in _SENTENCE_END:
        return True
    return False


def _contact_block_start(body: str) -> Optional[int]:
    """从**底部锚定**识别尾部签名块起点，避免误吃正文。

    步骤：
      1. 全文联系方式信号 < 2 → 不处理（None）。
      2. 定位最后一个含联系方式信号的行，从它向上扩展，
         逐行纳入"签名行"（空行/联系方式/问候语/短姓名职位行），
         遇到"句子型正文行"即停。
      3. 块起点必须 > 0（绝不从首行截断，杜绝整封清空），
         且块内仍含 ≥2 个联系方式信号。
    """
    lines = body.split("\n")
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1

    total_signals = sum(len(_CONTACT_SIGNAL.findall(ln)) for ln in lines)
    if total_signals < 2:
        return None
    contact_idx = [i for i, ln in enumerate(lines) if _CONTACT_SIGNAL.search(ln)]
    if not contact_idx:
        return None

    last = contact_idx[-1]
    start = last
    while start - 1 >= 0 and _is_signature_line(lines[start - 1]):
        start -= 1

    if start <= 0:
        return None  # 整封都像签名 → 不截断（交给质量评估）
    block = "\n".join(lines[start:])
    if len(_CONTACT_SIGNAL.findall(block)) < 2:
        return None
    return offsets[start]


def _remove_disclaimer(body: str) -> Tuple[str, bool]:
    for pattern in _DISCLAIMER_PATTERNS:
        m = pattern.search(body)
        if m:
            return body[: m.start()] + body[m.end() :], True
    return body, False


def _assess_quality(cleaned: str, original: str) -> str:
    if not cleaned:
        return "empty"
    if len(cleaned) < _MIN_BODY_LENGTH:
        return "too_short"
    if (
        len(original) >= _OVER_CLEAN_MIN_ORIGINAL
        and len(cleaned) < len(original) * _OVER_CLEAN_RATIO
    ):
        return "possible_over_clean"
    return "ok"
