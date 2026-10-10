"""复核触发器校准 —— 让"是否转人工"由**数据**驱动，而不是手调阈值。

背景（证据）：原始置信度分对"对/错"几乎没有区分力（审 9%→80% 漏网率几乎不变），
错误高度集中在少数话题（post_delivery_support/follow_up/requirement_discussion/
marketing_promotion 占 ~75% 的错）。因此用「话题错误先验 + 低置信 + 业务员 + 单动作」
拟合一个轻量风险分 P(标错)，按目标复核率取阈值。

训练（fit）在 sklearn 下做一次，产出 artifact（话题先验 + 逻辑回归系数 + 阈值）落盘；
推理（review_risk）只做 numpy 点积 + sigmoid，**不依赖 sklearn**。

第四层「经验沉淀」：人工复核纠错回流后，重跑 fit_from_records 即可更新 artifact，
触发器随真实错误分布持续自我校准。artifact 带 provenance（n_samples/fitted_at），可回溯。

特征顺序（务必与 artifact.feature_names 一致）：
    [topic_prior, conf_low(<0.66), sender_salesperson, single_action]
"""
import json
import math
import os
from typing import Optional

_ARTIFACT_PATH = os.path.join(os.path.dirname(__file__), "review_calibration_artifact.json")

# 低置信分界：经验上 <0.66 错误率显著抬升（其余分段是噪声）
_CONF_LOW_CUT = 0.66

_artifact_cache: Optional[dict] = None


def load_artifact(force: bool = False) -> dict:
    global _artifact_cache
    if _artifact_cache is not None and not force:
        return _artifact_cache
    with open(_ARTIFACT_PATH, "r", encoding="utf-8") as f:
        _artifact_cache = json.load(f)
    return _artifact_cache


def _features(primary_topic: str, confidence_score: float, sender_type: str,
              n_actions: int, art: dict) -> list:
    base = art.get("base_rate", 0.10)
    topic_prior = art.get("topic_priors", {}).get(primary_topic or "", base)
    conf_low = 1.0 if (confidence_score or 0.0) < art.get("conf_low_cut", _CONF_LOW_CUT) else 0.0
    sender_sales = 1.0 if sender_type == "salesperson" else 0.0
    single_action = 1.0 if n_actions == 1 else 0.0
    return [topic_prior, conf_low, sender_sales, single_action]


def review_risk(primary_topic: str, confidence_score: float, sender_type: str,
                n_actions: int, artifact: Optional[dict] = None) -> float:
    """返回该封被标错的估计概率 P(error) ∈ [0,1]（推理期，不依赖 sklearn）。"""
    art = artifact or load_artifact()
    f = _features(primary_topic, confidence_score, sender_type, n_actions, art)
    z = art.get("intercept", 0.0) + sum(c * x for c, x in zip(art.get("coef", []), f))
    try:
        return 1.0 / (1.0 + math.exp(-z))
    except OverflowError:
        return 0.0 if z < 0 else 1.0


def should_review(primary_topic: str, confidence_score: float, sender_type: str,
                  n_actions: int, threshold: Optional[float] = None,
                  artifact: Optional[dict] = None) -> tuple:
    """(needs_review, risk)。threshold 缺省用 artifact 内阈值（可被运行时配置覆盖）。"""
    art = artifact or load_artifact()
    risk = review_risk(primary_topic, confidence_score, sender_type, n_actions, art)
    thr = threshold if threshold is not None else art.get("threshold", 0.5)
    return (risk >= thr, risk)


# ── 训练 / 再校准（第四层经验沉淀复用） ────────────────────────────────────────

def fit_from_records(records: list, target_review_rate: float = 0.20,
                     smoothing_alpha: float = 8.0,
                     min_topic_samples: int = 20) -> dict:
    """用带标注的样本拟合校准 artifact。

    records: [{"primary": str, "sender": str, "score": float,
               "n_actions": int, "wrong": 0/1}, ...]
    target_review_rate: 默认运行点（决定阈值）；可被运行时配置覆盖。
    min_topic_samples: 话题先验的可靠性门槛。样本少的话题会继续向全局 base_rate 收缩，
                       避免少数错样本把某个 topic 永久打成高危补丁。
    需要 sklearn（仅训练期）。返回 artifact dict。
    """
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from datetime import datetime, timezone

    n = len(records)
    if n < 30:
        raise ValueError(f"样本太少（{n}），无法稳健校准（至少 30）")
    y = np.array([int(r["wrong"]) for r in records])
    base = float(y.mean())

    # 话题错误先验（两层收缩）：
    # 1) beta-binomial 加性平滑 → 收缩到全局 base；
    # 2) 样本量可靠性收缩 → count < min_topic_samples 时继续向 base 收缩。
    # 这样 topic prior 是"监控/路由信号"，不是把少数样本写死成业务补丁。
    topics = {}
    topic_counts = {}
    topic_errors = {}
    topic_reliability = {}
    for t in {r["primary"] for r in records}:
        errs = sum(int(r["wrong"]) for r in records if r["primary"] == t)
        cnt = sum(1 for r in records if r["primary"] == t)
        smoothed = (errs + smoothing_alpha * base) / (cnt + smoothing_alpha)
        reliability = min(1.0, cnt / max(float(min_topic_samples), 1.0))
        topics[t] = base + reliability * (smoothed - base)
        topic_counts[t] = cnt
        topic_errors[t] = errs
        topic_reliability[t] = reliability

    feature_context = {"base_rate": base, "topic_priors": topics, "smoothing_alpha": smoothing_alpha,
                       "conf_low_cut": _CONF_LOW_CUT}

    X = np.array([_features(r["primary"], r["score"], r["sender"], r["n_actions"], feature_context)
                  for r in records])
    clf = LogisticRegression(C=1.0, max_iter=2000).fit(X, y)
    coef = clf.coef_[0].tolist()
    intercept = float(clf.intercept_[0])

    # 取阈值：在训练集 score 分布上对齐目标复核率
    probs = clf.predict_proba(X)[:, 1]
    threshold = float(np.quantile(probs, max(0.0, 1.0 - target_review_rate)))

    return {
        "base_rate": base,
        "topic_priors": topics,
        "topic_counts": topic_counts,
        "topic_errors": topic_errors,
        "topic_reliability": topic_reliability,
        "smoothing_alpha": smoothing_alpha,
        "min_topic_samples": min_topic_samples,
        "feature_names": ["topic_prior", "conf_low", "sender_salesperson", "single_action"],
        "coef": coef,
        "intercept": intercept,
        "threshold": threshold,
        "target_review_rate": target_review_rate,
        "conf_low_cut": _CONF_LOW_CUT,
        "n_samples": n,
        "n_errors": int(y.sum()),
        "fitted_at": datetime.now(timezone.utc).isoformat(),
        "note": "fit_from_records",
    }


def save_artifact(artifact: dict) -> str:
    with open(_ARTIFACT_PATH, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)
    load_artifact(force=True)
    return _ARTIFACT_PATH
