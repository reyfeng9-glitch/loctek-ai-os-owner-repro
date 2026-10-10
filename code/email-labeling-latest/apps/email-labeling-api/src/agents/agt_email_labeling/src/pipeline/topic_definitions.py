"""Topic 枚举 + 显示名 + 优先级 + 归一化映射。

⚠️ 本文件不再手写数据，全部从单一真相源 taxonomy.py 派生（保持公共名不变以兼容现有 import）。
新增/修改主题请改 taxonomy.py。
"""
from enum import Enum

from src.agents.agt_email_labeling.src.pipeline import taxonomy

# 从 taxonomy 动态生成 str 枚举（成员名 = code 大写）。
TopicCode = Enum(
    "TopicCode",
    {code.upper(): code for code in taxonomy.TOPIC_CODES},
    type=str,
)

TOPIC_DISPLAY_MAP: dict[str, str] = dict(taxonomy.TOPIC_DISPLAY)
TOPIC_PRIORITY: dict[str, int] = dict(taxonomy.TOPIC_PRIORITY)
TOPIC_NORMALIZE: dict[str, str] = dict(taxonomy.TOPIC_NORMALIZE)
