# B2B 邮件打标 Agent（`agt_email_labeling`）— 现状权威文档

外贸往来邮件的自动结构化打标：**主题 + 动作 + 业务标签 + 风险信号 + 复核标记**。
独立打标服务（REST 接口），不是对话型 Agent。

> 本文是**当前逻辑/技术/流程/注意点/事实**的单一真相源。改动核心逻辑前先读懂相关章节，改完**回来更新本文 + 配置页**。
> 配置页（"Agent 认知视图"）与本文同源：`apps/web/.../agt_email_labeling/config/page.tsx`。

---

## 0. 一句话现状 + 关键指标

- **每封邮件输出**：`topics[]`(多主题) + `actions[]`(多动作) + `business_labels[]`(业务场景) + `risk_signals[]` + `confidence` + `needs_review`。
- **核心口径**：**多标签覆盖**——真实主题 ∈ `topics ∪ primary` 即算对（不追求单 primary 精确）。
- **设计哲学**：**动作为主、动作反推意图，无业务漏斗状态机**。
- **当前指标(四部测试集)**：golden 多标签 **≈86–88%**(re-roll 噪声带)、复核率 **13–14%**(≤15% 红线)、topics/封 **≈1.5**(不滥用)；质量不变量(NAR矛盾/双primary/无primary)**全 0**。
- **数据**：1405 封 / 6 客户(HU24·DM13·ae131·NE122·PROV-GMAIL·PROV-ETHNICRAFT) / 已去重清洗。
- **模型**：gpt-5.2(Azure)，temperature=0。
- **标签库规模**：16 主题 / 47 动作 / 17 事实 / 5 业务标签。

---

## 1. 流水线全貌（`services/orchestrator_v2.py::run_labeling_pipeline_v2`）

```
Stage 0  SystemFilter      自动回复/退信/空/过短 → 直接落库 system_noise，短路后续(不进 LLM、不派生业务标签)
Stage 1  Cleaner           clean_email：去签名/历史引用/免责声明，正文封顶
Stage 2a 会话层上下文       thread_context_v2：本 thread 近期实体+事实(见 §6 两层上下文)
Stage 2a'客户级上下文       customer_context_v2 + authoritative_context：跨该客户里程碑 + ERP 权威事件(按 sent_at 截断)
Stage 2b MultiLabelExtract  LLM 抽取 topics/actions/entities/current_facts/summary/decision_trace(唯一的不确定环节)
         ↓ 此处冻结 facts.raw_extraction(规则/封顶前的原始结构化输出，供离线重放，见 §7)
Stage 3  RuleValidator      确定性后处理：补漏/降置信/方向过滤/自洽闸门/去重(见 §5)
Stage 4  PrimaryTopicDeriver _derive_primary_topic：动作支撑的主题里按 TOPIC_PRIORITY 取最高(no_sink：有具体主题就不让垃圾桶当主标)
Stage 4·精度  _cap_topics    主题封顶 ≤topic_cap(=3)：剪无支撑噪声主题，保真实主题(keep_role：保 LLM 自标 primary 的弱支撑主题)
Stage 4·一致 _normalize_topic_roles  topics[] 恰一个 role=primary(对齐 primary_topic)，修双/无 primary
Stage 4a derive_risk_signals 动作即真相派生风险信号(漏斗层已移除)
Stage 4c _derive_business_labels  业务标签派生(动作+ERP+时序上下文，见 §4)
Stage 4b ConfidenceScorerV2 置信度(证据/动作对齐/话题清晰度) + 复核决策(结构硬触发 + 校准风险闸门，见 §8)
Stage 5  ContextUpdaterV2   持久化 + 写 thread.multi_label_recent_events(供同线程后续邮件的上下文)
```

---

## 2. 标签体系（`pipeline/taxonomy.py` 单一真相源）

`taxonomy.py` 派生 `topic_definitions.py` / `action_definitions.py`；import 期 `_validate()` 保证父子合法、code 不重复、每主题有动作挂靠。**改标签只动 `taxonomy.py`。**

- **16 主题(topic)**：业务领域。`system_noise / no_action_reply / marketing_promotion / follow_up / meeting_arrangement / requirement_discussion / quotation / price_negotiation / sample_preparation / order_payment_confirmation / order_detail_confirmation / fulfillment_logistics / post_delivery_support / quality_issue / cancellation_pause / partnership_distribution`。
- **47 动作(action)**：本封"字面做了什么"。每个动作有规范归属主题(`ACTION_TOPIC_MAP`)，下游用它判动作支撑，**不信 LLM 的 `topic_ref`**。
- **17 事实(current_facts)**：本封是否明确提到某事实(已下单/已收货/报价含金额/推介新品…)。
- **别名归一(`ACTION_NORMALIZE`)**：旧 code/同义词自动映射到规范 code(动作中性化后老数据靠它兼容)。

### 设计原则（固化）
1. **多标签**：一封可多主题/多动作；指标是"真实主题 ∈ topics∪primary"。
2. **动作为主、动作反推意图**：先看"发件人这封在干嘛"，不看出现了哪些词；**无漏斗状态机**(不跨邮件推断"客户当前阶段")。
3. **置信度不靠 LLM 自评**：由系统从落地信号(证据/动作对齐/清晰度)合成。
4. **主题归属不信 LLM 的 topic_ref**：用确定性的标签标准库。
5. **硬兜底 ≤1-2 条**：不靠 prompt 逐类打补丁。

---

## 3. 动作中性化（进行中的架构演进，重要）

**原则**：动作 = **纯观察**(字面做了什么)；关系/时序/ERP 的解释上移到**业务标签层**。
**已做(批1)**：`outreach_cold_email` / `outreach_new_product` / `customer_recall` 三个"上下文型"动作合并为中性 **`product_outreach`(产品推介)**——冷启动/新品/老客户召回的区别交给业务标签。旧 code 全列为别名，老数据 `ACTION_NORMALIZE` 自动归一。
**已做前置**：`first_reply`/`quotation_followup` 焊死的"首次/报价跟进"已升为业务标签(`first_contact`/`quotation_followup`)。
**待做(已暂停)**：批2 去 `first_reply` 动作、批3 并 `quotation_followup`→`follow_up`(业务标签价值已交付，剩动作层清理，建议合并成一次重抽以减 re-roll 噪声)。

> 分类清单见 git 历史/对话：**上下文型(中性化)** = outreach三件套(已合并) + first_reply + quotation_followup；**纯观察(保留)** = send_quotation / price_inquiry / sample_* / order_* / shipment_* / quality_complaint 等约40个。

---

## 4. 业务标签层（`pipeline/business_labels.py`）

**动作之上的派生场景标签**，由 `动作 + 邮件摘要 + ERP权威 + 时序上下文(+语义fact)` 派生。**多标签、规则为主(+边界 LLM 钩子预留)、声明式可扩展**。与已移除的漏斗状态机不同：这是**每封邮件自身**的场景标签，不跨邮件推断阶段。

| 业务标签 | 派生条件 | 类型 |
|---|---|---|
| 冷启动开发 `cold_start_development` | 业务员 + `product_outreach` + 客户尚未首次回复 | 确定性 |
| 新品推介 `new_product_promotion` | 业务员 + `product_outreach` + `promotes_new_product` fact | 确定性(+语义fact) |
| 老客户召回 `customer_recall` | 业务员 + 有ERP正式订单 + `product_outreach` + 距上次联系≥60天 | 确定性 |
| 首次接触 `first_contact` | 客户邮件 + 该客户首次回复(`customer_replied_before`=否) | 确定性 |
| 报价跟进 `quotation_followup` | 业务员 + 催回复/报价跟进动作 + 本线程此前已发报价 + 本封未进入成交后/履约动作 | 确定性 |

- **上下文信号**(`compute_business_signals`)：`has_formal_order`(ERP order_confirmed) / `customer_replied_before` / `days_since_last_contact` / `thread_had_quotation_before`。
- **多标签共存**：如"给冷客户推新品" = 冷启动开发 + 新品推介。
- **生产路径**：orchestrator Stage 4c 查该客户/线程历史算信号 → 派生 → 存 `facts.business_labels`(失败不致命)。
- **离线回填**：`scripts/business_label_sim.py --apply`(从冻结数据算，零 LLM；**跳过 system_noise**)。

---

## 5. 确定性后处理（`pipeline/rule_validator.py` + 声明式规则模块）

- **自洽闸门(`topic_consistency.py`，声明式 + 配置页同源)**：
  - `no_action_reply` ⊥ 具体业务主题(命中即剔除哨兵+其 short_reply 动作；例外软主题 `marketing_promotion`/`cancellation_pause` 可并存——golden 实证)。
  - `system_noise` ⊥ 一切(由 SystemFilter 上游短路保证)。
- **发件方向(`direction_rules.py`，声明式 + 配置页同源)**：
  - 客户邮件剔业务员专有动作(`SALES_ONLY_ACTIONS`：send_quotation/product_outreach/shipment_notice…共7个)。
  - 业务员邮件剔客户专有动作(`CUSTOMER_ONLY_ACTIONS`：first_reply/price_inquiry/各类买方问询，共5个)。
  - 主题方向：客户邮件的 `quotation` 实为询价 → 有他主题则剔除、唯一则 remap 成 `requirement_discussion`。
- **deriver `no_sink`**：有具体主题被支撑时，垃圾桶主题(requirement_discussion/follow_up/no_action_reply)不当 primary。
- **cap `keep_role`**：封顶保留 LLM 自标 role=primary 的弱支撑主题(召回安全，不滥用)。
- **主题去重 + 单 primary 归一**：同名主题只留一个；topics[] 恰一个 primary(=派生 primary_topic)。
- **唯一硬兜底(outreach_quote_backstop)**：业务员**纯展会** teaser 价 ≠ 报价(`_OUTREACH={outreach_exhibition}`；动作中性化后已不含 product_outreach，避免误删真实新品报价)。

---

## 6. 两层上下文模型（地基，勿擅改）

实体层级：`email → thread(真实会话) → customer(客户公司，主键=客户编码 customer_id)`。`thread ≠ customer`：一个客户多业务员/多联系人/多 thread。**上下文聚合的客户 key = `customer_id`**，不是邮箱。

打标需上下文做**时序消歧**(已下单后问交期=履约不是询盘)。采用**方案B 两层**(不采用"塌缩多 thread"——会破坏会话结构/交叉污染/丢业务员归属)：

| 层 | 粒度 | 内容 | 实现 |
|---|---|---|---|
| 会话上下文 | 单 thread | 近期实体(产品/文档/价格 最近10封) + 近期事实(最近5封) | `thread_context_v2` 读 `thread.multi_label_recent_events` |
| 客户级上下文 | 跨该客户全部 thread | 里程碑(已报价/已寄样/有投诉…) + ERP 权威事件 | `customer_context_v2` + `authoritative_context` |

**两条护栏**：① 客户级层只聚状态/里程碑，不灌跨会话产品实体(防污染)；② 只取 `sent_at` 之前的事件(防未来泄露——按客户时序串行重抽时尤其致命)。
**不变量**：进 prompt 的文本大小**恒定有界**(会话 10/5 封封顶、ERP ≤8 条、里程碑几个词、`_MAX_THREADS=50`/`_MAX_SCAN_EVENTS=60`)，与历史规模无关。
Prompt 两段：`## 当前会话上下文` + `## 客户全局上下文`(模板见 `multi_label_extractor._MULTI_LABEL_USER_PROMPT_TEMPLATE`)。

### 权威事件(ERP)
`authoritative_events.py` / `authoritative_context.py`：客户 ERP 下单(`order_confirmed`)/发货(`order_shipped`)切片(截至本邮件)。**只有真实 ERP 事实进权威层**——"是否已下单/已付款/已发货"只来自 authoritative_events，**不把邮件文本自行升级成权威事实**(防错误自传播)。测试期由 `scripts/inject_authoritative_orders.py` 灌入；上线把 `authoritative_events._load_orders` 换业务 API 即可。

---

## 7. 冻结抽取 + 离线重放 + 验证 gate（token-free 迭代地基）

**冻结抽取**：`facts.raw_extraction` = 规则/封顶/派生**之前**的 LLM 原始结构化输出。因此**确定性后处理(rule/cap/deriver/business_labels)的任何改动,都能在冻结数据上离线重放验证,零 LLM**。

- `scripts/full_repersist.py`：把当前确定性后处理就地重算到全量(`--apply`)，带**忠实性闸门**(no-op 子集上 conf 必须与落库一致)。
- `scripts/business_label_sim.py`：离线派生/回填业务标签。
- **验证 gate(falsifiable)**：任何后处理候选必须过——全量净 Δ>0 / 多标签召回不降 / 时间留出(后30%) + 客户留出(bart) 不退 / 回归拒绝。历史上 gate 拒掉了 ~9 个貌似合理但有害的候选,只放行真改进。
- **定向重抽**：改 prompt/动作枚举(抽取级)必须重跑 LLM。用 `scripts/label_sibu_dataset.py --force --data <子集>` **只重抽受影响邮件**(减 token + 减 re-roll 噪声)。

> ⚠️ 容器内跑脚本需 `PYTHONPATH=/app`；脚本读 `scripts/sibu_dataset_clean.jsonl`(= 当前 DB 1405 的干净副本，**不是** sibu_dataset.jsonl 那份含已删重复的旧表)。

---

## 8. 置信度 / 复核（`confidence_scorer.py` + `review_calibration.py`）

- **置信度**(不用 LLM 自评)：证据覆盖(55%) + 动作-话题对齐(25%) + 话题清晰度(20%)。
- **复核决策** = 结构硬触发(无主题/正文过短/规则命中坏模式) **OR** 校准风险闸门。
- **校准闸门**：按 golden 拟合的 P(标错) 逻辑回归(话题先验+低置信+发件方+动作数)，阈值来自 runtime config `review_risk_threshold`(当前 **0.22**，全量复核 ≈14%)。artifact: `review_calibration_artifact.json`，重拟合 `scripts/fit_review_calibration.py`。
- **运行时配置**(`runtime_config.py`，配置页可改)：`temperature=0` / `topic_cap=3` / `review_risk_threshold=0.22` / `outreach_quote_backstop=True`。

---

## 9. 配置页（`apps/web/.../config/page.tsx`，需与本文同步维护）

后台"Agent 认知视图"，**可见/可控/可复盘**。已有 REST 同源数据：
- 流水线节点(`GET /pipeline/nodes`)、运行时配置(`GET/PUT /config`)、配置变更审计(`/config/changes`)。
- **自洽规则**(`GET /config/topic-consistency`)、**方向约束**(`GET /config/direction-rules`)、**业务标签**(`GET /config/business-labels`)——三者均与流水线声明式规则**同源**,改规则即自动反映。
- 客户时间线(`/customers/{id}/timeline`)每封邮件卡片显示 topics/actions/**业务标签 chip**/摘要/COT。

> 新增业务标签/规则只动后端声明式模块,配置页与邮件卡片自动展示,**无需改前端**。但若改了**流水线阶段**或**指标口径**,记得同步更新配置页的"职责与红线/流水线"描述。

---

## 10. 关键脚本（容器内 `/app`，多数 `PYTHONPATH=/app`）

| 脚本 | 用途 |
|---|---|
| `label_sibu_dataset.py --force --data <jsonl>` | 重抽(调 LLM)，按 thread 串行、组间并发；定向重抽传子集 jsonl |
| `full_repersist.py [--apply --force]` | 全量离线重放确定性后处理 + 忠实性闸门(零 LLM) |
| `business_label_sim.py [--apply]` | 离线派生/回填业务标签 |
| `audit_sampler.py` | 拉真实邮件正文+标签供人读审计 |
| `backstop_replay.py` / `cap_replay.py` / `deriver_simulator.py` | 各类后处理候选的离线 gate 重放 |
| `fit_review_calibration.py` | 重拟合复核校准 artifact |
| `inject_authoritative_orders.py` | 测试期灌 ERP 权威事件 |
| `tune_review_threshold.py` | 在忠实置信度上重扫复核阈值 |

---

## 11. 注意点 / 陷阱（踩过的坑）

1. **重抽 = 重掷骰子**：gpt-5.2 非完全确定性,每次重抽 golden 浮动 ~±2–3pp(噪声)。**确定性后处理用 gate 在固定抽取上验**(无噪声);**绝对准确率数字本身带噪声**,别过度解读单次。多次重抽噪声累积——能合并的重抽合并。
2. **golden 可能滞后于领域决策**：如"节日问候"——报告说应 follow_up,旧 golden 标 no_action_reply。这种是**golden 该更新**,不是代码回归。改 ground truth 前先列具体 email_id 给人确认。
3. **报价权威数据拿不到 = 接受天花板**:quotation 漏标/报价相关准确度低是正常的,**不为它编奇怪规则**。
4. **冻结抽取的 evidence**:动作 evidence 全有、主题 evidence LLM 不给(靠规则补)。离线重放置信度时,陈旧落库置信度(dedup/方向/backstop 改了标签却没重算)会造成"漂移"——`full_repersist` 重算才是当前正确值。
5. **时区**:`emails.sent_at` 是 naive timestamp,`req.sent_at` 可能带时区——比较前去时区(业务标签信号查询踩过)。
6. **脚本数据源**:用 `sibu_dataset_clean.jsonl`(干净副本),别用含已删重复的旧 jsonl;容器跑脚本要 `PYTHONPATH=/app`。
7. **外部进程并发**:本仓 uat 分支有他人/外部进程在改/提交(曾在工作期把改动 git add 进 HEAD、删过重复行)。提交务必 **scoped 只 add 自己的文件**,别 `git add -A`(会带进别人未提交的活)。
8. **改 prompt/动作枚举必须重抽**;改纯后处理可离线重放(零 token)。
9. **system_noise 不派生业务标签**(生产上游短路;离线 sim 已补跳过)。

---

## 12. 关键实现位点（file map）

| 关注点 | 文件 |
|---|---|
| 编排 + 阶段顺序 + 业务标签派生 + 持久化 | `services/orchestrator_v2.py` |
| LLM 抽取 prompt / 调用 | `pipeline/multi_label_extractor.py` |
| 标签库单一真相源 | `pipeline/taxonomy.py`(派生 `topic_definitions.py`/`action_definitions.py`) |
| 业务标签(声明式) | `pipeline/business_labels.py` |
| 自洽闸门(声明式) | `pipeline/topic_consistency.py` |
| 发件方向(声明式) | `pipeline/direction_rules.py` |
| 确定性后处理(规则) | `pipeline/rule_validator.py` |
| 置信度 / 复核校准 | `pipeline/confidence_scorer.py` · `pipeline/review_calibration.py` |
| 两层上下文 | `pipeline/thread_context_v2.py` · `pipeline/customer_context_v2.py` · `pipeline/authoritative_context.py` |
| 风险信号 / 系统过滤 / 清洗 | `pipeline/stage_risk.py` · `pipeline/system_filter.py` · `pipeline/cleaner.py` |
| 运行时配置 | `pipeline/runtime_config.py` |
| 路由(含配置页 API) | `routes/email_labeling.py` |
| 配置页 | `apps/web/src/app/admin/agents/agt_email_labeling/config/page.tsx` |
