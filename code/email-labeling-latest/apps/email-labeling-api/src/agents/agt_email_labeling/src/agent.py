"""
B2B 邮件打标 Agent

本 Agent 采用独立页面交互模式，功能由前端页面 + REST API 提供：
  - POST /api/v1/email-labeling/label — 单封打标
  - POST /api/v1/email-labeling/label/batch — 批量打标
  - GET  /api/v1/email-labeling/review/pending — 待复核列表
  - POST /api/v1/email-labeling/review/{task_id} — 提交复核
  - GET  /api/v1/email-labeling/results/{email_id} — 查询打标结果
  - GET  /api/v1/email-labeling/evaluation/stats — 评估统计

此入口函数仅供 Agent 注册系统使用，实际功能走独立的 REST 路由。
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


async def execute(
    run_id: str,
    agent_id: str,
    agent_id_str: str,
    user_id: str,
    input_data: Dict,
    db_session: Any,
    config: Optional[Dict] = None,
) -> Dict:
    return {
        "success": True,
        "result": {
            "type": "standalone_page",
            "message": "B2B 邮件打标使用独立页面交互，请通过前端页面访问。",
        },
    }
