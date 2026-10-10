"""风险信号(risk_signals) 派生 —— 确定性后处理。

设计：风险**不由 LLM 直接产出**，由系统从动作 / current_facts 确定性派生（动作即真相）。
注：business_stage（业务漏斗阶段）这一层已移除——现在以动作为主、动作反推意图(topic)，
不再讲业务漏斗。真实订单作为权威事件单独留在事件流/facts，仅用于动作校验，不产出阶段。
"""
from typing import List

# 动作 → 风险信号（确定性，动作即真相）
_RISK_BY_ACTION = {
    "quality_complaint": "质量风险",
    "order_cancellation": "流失风险",
    "price_negotiation": "价格压力",
}


def derive_risk_signals(multi) -> List[str]:
    """派生风险信号：动作（可靠）+ current_facts（价格异议）。"""
    risks: List[str] = []
    action_codes = {a.action for a in multi.actions}
    for act, risk in _RISK_BY_ACTION.items():
        if act in action_codes and risk not in risks:
            risks.append(risk)
    cf = multi.current_facts
    if getattr(cf, "price_objection_in_email", False) and "价格压力" not in risks:
        risks.append("价格压力")
    if getattr(cf, "quality_complaint_in_email", False) and "质量风险" not in risks:
        risks.append("质量风险")
    return risks
