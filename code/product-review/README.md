# 评论分析代码入口

评论功能已经部署在 OS 的正式/UAT 服务上。页面入口：<http://ai-os-test.loctek.com/competitor>。

## 源码

- GitLab 项目：<http://192.168.0.135/wanghongsheng/loctek-ai-os>
- UAT 分支：<http://192.168.0.135/wanghongsheng/loctek-ai-os/-/tree/uat?ref_type=heads>
- Agent 目录：<http://192.168.0.135/wanghongsheng/loctek-ai-os/-/tree/uat/apps/api/src/agents/agt_product_review>
- 评论接口前缀：`/v1/product-review`

## 验收

UAT 记录中的评论详情迁移 commit 是 `87105ea7`，20/20 个真实商品 URL 回归通过。完整测试报告在 [`../../results/review/完整测试集-真实抓取报告-20260825.md`](../../results/review/完整测试集-真实抓取报告-20260825.md)。

抓取链路和下游 `/v1/competitor/reviews/tag` 打标链路分开验收；后者的模型标签校验或代理连接错误不能归类为抓取失败。
