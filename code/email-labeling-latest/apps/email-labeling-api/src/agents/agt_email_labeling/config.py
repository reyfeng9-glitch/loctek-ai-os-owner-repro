"""Email Labeling Agent 静态默认配置。

当前主路径是 V2 multi-label：
  Cleaner → ThreadContextV2/authoritative context → MultiLabelExtractor
  → RuleValidator → PrimaryTopicDeriver → score/review → writeback

运行时可调项（temperature/topic_cap/review_risk_threshold 等）在
pipeline/runtime_config.py，由后台管理接口维护。这里仅保留低层 LLM 调用的默认兜底值。
"""

AGENT_ID = "agt_email_labeling"

# Legacy 单事件抽取默认模型；V2 主路径不再使用 FactExtractor。
FACT_EXTRACTOR_MODEL = "gpt-4o-mini"

# Legacy Adjudicator 默认模型；V2 主路径改为 multi-label raw JSON + 确定性后处理。
ADJUDICATOR_MODEL = "gpt-5.2"

# LLM 调用失败最大重试次数
LLM_MAX_RETRIES = 3
