"""Action 枚举 + 显示名 + Topic 映射 + 优先级 + 归一化映射。

⚠️ 本文件不再手写数据，全部从单一真相源 taxonomy.py 派生（保持公共名不变以兼容现有 import）。
新增/修改动作请改 taxonomy.py；taxonomy._validate() 会在 import 期保证：
  - 每个动作的父主题是合法 TopicCode；
  - code 不重复、别名不冲突、每个主题都有动作挂靠。
"""
from enum import Enum

from src.agents.agt_email_labeling.src.pipeline import taxonomy

# 从 taxonomy 动态生成 str 枚举（成员名 = code 大写）。
ActionCode = Enum(
    "ActionCode",
    {code.upper(): code for code in taxonomy.ACTION_CODES},
    type=str,
)

ACTION_DISPLAY_MAP: dict[str, str] = dict(taxonomy.ACTION_DISPLAY)
ACTION_TOPIC_MAP: dict[str, str] = dict(taxonomy.ACTION_TOPIC)
ACTION_PRIORITY: dict[str, int] = dict(taxonomy.ACTION_PRIORITY)
ACTION_NORMALIZE: dict[str, str] = dict(taxonomy.ACTION_NORMALIZE)
