# -*- coding: utf-8 -*-
"""主题自洽性(互斥)闸门 —— 声明式单一真相源。

被流水线(rule_validator)强制执行,也被配置页 API 读取展示(GET /config/topic-consistency)。

设计原则(经全量共现数据 + golden 验证):
  只声明"逻辑上不可能共存"的主题对 —— 即「无实质业务哨兵」与「具体业务主题」互斥。
  具体业务主题彼此之间(报价+需求、促销+报价、订单+物流 …)都是**合法多标签**,不互斥——
  不得为追求"看起来更干净"而给业务主题之间编造互斥规则(那是过拟合)。

当前仅两条哨兵规则:
  1) no_action_reply(无实质业务回复)⊥ 具体业务主题  —— 后处理执行;
  2) system_noise(系统噪声)⊥ 一切                  —— 由 SystemFilter(Stage0)短路保证,永不共存。
"""

from src.agents.agt_email_labeling.src.pipeline.topic_definitions import TOPIC_DISPLAY_MAP

# 软主题:收到促销 / 简短暂停时,"纯客套回复"可与 no_action_reply 合法并存。
# golden 实证:这两类与 no_action_reply 共存时,人工 gold 仍判 no_action_reply —— 盲目剔除会丢覆盖。
_NAR_COMPATIBLE = ("marketing_promotion", "cancellation_pause")

# ── 声明式互斥规则(单一真相源)──────────────────────────────────────────────
TOPIC_EXCLUSIONS = [
    {
        "id": "no_action_reply_vs_business",
        "sentinel": "no_action_reply",
        "scope": "substantive",                 # 与"所有具体业务主题"互斥
        "compatible_with": list(_NAR_COMPATIBLE),
        "drop_actions": ["short_reply"],
        "resolution": "drop_sentinel",          # 冲突时剔除哨兵主题,保留业务主题
        "title": "无实质业务回复 ⊥ 具体业务主题",
        "rationale": (
            "no_action_reply 表示本封无实质业务,与 order_payment / requirement / quotation 等"
            "具体业务主题逻辑互斥(例:一句话问「能付欧元吗」是付款问询,不是无动作回复)。"
            "命中即剔除 no_action_reply 及其 short_reply 动作,让 primary 落到真正的业务主题。"
            "例外:收到促销 / 简短暂停时,纯客套回复可与之并存(golden 实证)。"
        ),
    },
    {
        "id": "system_noise_vs_all",
        "sentinel": "system_noise",
        "scope": "all",                         # 与任何其它主题互斥
        "compatible_with": [],
        "drop_actions": [],
        "resolution": "enforced_upstream",      # 由 SystemFilter Stage0 短路保证
        "title": "系统噪声 ⊥ 一切业务主题",
        "rationale": (
            "system_noise = 自动回复 / 退信 / 读取回执 / 空内容 等非业务邮件;"
            "在 SystemFilter(Stage0)即短路落库,不进入业务打标,故永不与任何业务主题共存。"
        ),
    },
]


def apply_topic_exclusions(multi) -> None:
    """对 multi_label 强制执行声明式互斥规则(原地修改)。

    目前仅 resolution=='drop_sentinel' 的规则需在后处理执行;
    resolution=='enforced_upstream' 的(system_noise)由上游短路保证,这里无需处理。
    """
    for rule in TOPIC_EXCLUSIONS:
        if rule["resolution"] != "drop_sentinel":
            continue
        sentinel = rule["sentinel"]
        if not any(t.topic == sentinel for t in multi.topics):
            continue
        compatible = set(rule.get("compatible_with") or ())
        # "硬冲突"主题 = 既非哨兵本身、也不在兼容白名单内的任何主题
        has_hard = any(
            t.topic != sentinel and t.topic not in compatible for t in multi.topics
        )
        if has_hard:
            multi.topics = [t for t in multi.topics if t.topic != sentinel]
            drop = set(rule.get("drop_actions") or ())
            if drop:
                multi.actions = [a for a in multi.actions if a.action not in drop]


def exclusions_for_display() -> list:
    """配置页展示视图:补中文名,便于前端直接渲染。"""
    out = []
    for r in TOPIC_EXCLUSIONS:
        out.append({
            **r,
            "sentinel_display": TOPIC_DISPLAY_MAP.get(r["sentinel"], r["sentinel"]),
            "compatible_display": [
                TOPIC_DISPLAY_MAP.get(c, c) for c in (r.get("compatible_with") or ())
            ],
        })
    return out
