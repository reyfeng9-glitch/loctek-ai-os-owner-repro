"""邮件打标运行时配置 —— 让"可控"项真正可改可存（不改代码即可调行为）。

单行 JSONB 存储（email_labeling_config），带短 TTL 缓存。
变更审计：所有 update_config 都写入 email_labeling_config_changes（who/when/from/to/reason），
让"配置改了什么、为什么改、谁改的"成为可回放的资产。
免 Alembic 迁移：CREATE TABLE IF NOT EXISTS 在首次访问时建表。
"""
import json
import time
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 可编辑项白名单（也是默认值）。新增可调项只改这里 + 在管线读取处接上。
_DEFAULTS = {
    "temperature": 0.0,          # 抽取温度（可复现：0）
    "topic_cap": 3,              # 主题封顶（现实里一封 ≤3）
    # 复核风险阈值：校准模型给出的 P(标错) ≥ 此值 → 转人工。
    # 越低=审得越多。默认 0.14 ≈ 20% 复核率（LOO-CV：错误召回 ~54%、漏网 ~6%）。
    "review_risk_threshold": 0.14,
    "outreach_quote_backstop": True,  # 唯一硬兜底：业务员 outreach+报价 → 删报价
    # N122 Lite→Flash routing is separate from the legacy topic cap and review score.
    "n122_cascade_enabled": True,
    "n122_low_confidence_threshold": 0.75,
    "n122_lite_model": "gemini-2.5-flash-lite",
    "n122_flash_model": "gemini-2.5-flash",
}
_BOOL_KEYS = {"outreach_quote_backstop", "n122_cascade_enabled"}
_INT_KEYS = {"topic_cap"}
_STRING_KEYS = {"n122_lite_model", "n122_flash_model"}

_CACHE: dict = {"data": None, "ts": 0.0}
_TTL = 10.0  # 秒


async def _ensure(db: AsyncSession) -> None:
    await db.execute(text(
        "CREATE TABLE IF NOT EXISTS email_labeling_config "
        "(id int PRIMARY KEY DEFAULT 1, config jsonb NOT NULL, updated_at timestamp DEFAULT now())"
    ))
    await db.execute(
        text("INSERT INTO email_labeling_config (id, config) VALUES (1, :c) ON CONFLICT (id) DO NOTHING"),
        {"c": json.dumps(_DEFAULTS)},
    )
    # 变更审计表：每次 PUT /config 在这里追加一行
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS email_labeling_config_changes (
            id           BIGSERIAL PRIMARY KEY,
            changed_at   timestamp NOT NULL DEFAULT now(),
            changed_by   varchar(255),
            field_key    varchar(80) NOT NULL,
            from_value   jsonb,
            to_value     jsonb,
            reason       text,
            full_config  jsonb
        )
    """))
    await db.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_email_labeling_config_changes_at "
        "ON email_labeling_config_changes(changed_at DESC)"
    ))
    await db.commit()


def _coerce(patch: dict) -> dict:
    out = {}
    for k, v in patch.items():
        if k not in _DEFAULTS:
            continue
        if k in _BOOL_KEYS:
            out[k] = bool(v)
        elif k in _INT_KEYS:
            out[k] = max(1, int(v))
        elif k in _STRING_KEYS:
            value = str(v).strip()
            if value:
                out[k] = value
        else:
            out[k] = float(v)
    return out


async def get_config(db: AsyncSession) -> dict:
    """读取配置（短 TTL 缓存）。永远返回含全部默认键的完整 dict。"""
    if _CACHE["data"] is not None and (time.time() - _CACHE["ts"]) < _TTL:
        return _CACHE["data"]
    await _ensure(db)
    row = (await db.execute(text("SELECT config FROM email_labeling_config WHERE id=1"))).scalar_one_or_none()
    cfg = {**_DEFAULTS, **(row or {})}
    _CACHE["data"] = cfg
    _CACHE["ts"] = time.time()
    return cfg


async def update_config(
    db: AsyncSession,
    patch: dict,
    *,
    changed_by: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict:
    """覆盖更新配置 + 把每个变化的字段写入审计表。"""
    await _ensure(db)
    cur_row = (await db.execute(
        text("SELECT config FROM email_labeling_config WHERE id=1")
    )).scalar_one()
    cur = {**_DEFAULTS, **(cur_row or {})}
    coerced = _coerce(patch)
    new = {**cur, **coerced}

    # 落审计：只记录真正发生变化的字段
    changes_logged = 0
    for k, v in coerced.items():
        if cur.get(k) != v:
            await db.execute(text("""
                INSERT INTO email_labeling_config_changes
                    (changed_by, field_key, from_value, to_value, reason, full_config)
                VALUES (:by, :k, CAST(:fv AS jsonb), CAST(:tv AS jsonb), :reason, CAST(:full AS jsonb))
            """), {
                "by": changed_by, "k": k,
                "fv": json.dumps(cur.get(k)), "tv": json.dumps(v),
                "reason": reason, "full": json.dumps(new),
            })
            changes_logged += 1

    if changes_logged:
        await db.execute(
            text("UPDATE email_labeling_config SET config=:c, updated_at=now() WHERE id=1"),
            {"c": json.dumps(new)},
        )
    await db.commit()
    _CACHE["data"] = new
    _CACHE["ts"] = time.time()
    return new


async def list_config_changes(
    db: AsyncSession,
    *,
    field_key: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """读取最近 N 条配置变更（按时间倒序）。供"配置变更日志"页使用。"""
    await _ensure(db)
    where = ""
    params: dict = {"lim": int(limit)}
    if field_key:
        where = "WHERE field_key = :fk"
        params["fk"] = field_key
    rows = (await db.execute(text(f"""
        SELECT id, changed_at, changed_by, field_key, from_value, to_value, reason
        FROM email_labeling_config_changes
        {where}
        ORDER BY changed_at DESC
        LIMIT :lim
    """), params)).fetchall()
    return [{
        "id": r[0],
        "changed_at": r[1].isoformat() if r[1] else None,
        "changed_by": r[2],
        "field_key": r[3],
        "from_value": r[4],
        "to_value": r[5],
        "reason": r[6],
    } for r in rows]
