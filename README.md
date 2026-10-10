# Loctek AI OS owner 复现入口

这份仓库给另一个 AI 或项目 owner 使用，入口、结果、代码快照和部署基线都在仓库内。先读 [`docs/入口文档.md`](docs/入口文档.md)，再按页面入口和结果文件复现。

## 两个正式页面

- 评论分析：<http://ai-os-test.loctek.com/competitor>
- 邮件营销/打标：<http://ai-os-test.loctek.com/email-marketing>

页面需要使用者自己的 OS 账号登录。仓库不保存账号、密码、API key、数据库连接串或 SSH 私钥。

## 目录

- [`docs/入口文档.md`](docs/入口文档.md)：可直接交给 AI 的复现说明。
- [`results/review/完整测试集-真实抓取报告-20260825.md`](results/review/完整测试集-真实抓取报告-20260825.md)：评论完整测试集真实抓取报告。
- [`results/email/N122_新旧标签对比_业务说明.xlsx`](results/email/N122_新旧标签对比_业务说明.xlsx)：老版本/生产基线对照结果。
- [`results/email/N122_新旧标签对比_反馈修正版10.10.xlsx`](results/email/N122_新旧标签对比_反馈修正版10.10.xlsx)：最新业务反馈修正版结果。
- [`code/email-labeling-latest/`](code/email-labeling-latest/)：最新邮件打标代码快照，来源 commit `c4f37101ee6b960adb0a54e18be4a4a102a87b29`。
- [`code/product-review/`](code/product-review/)：评论代码部署与 GitLab 源码入口说明。

## 版本边界

正式服务器目前仍是 8.7/8.x 老版本和老结果。仓库里的最新邮件代码与最新结果是独立验收快照，不表示已经覆盖正式服务器部署；评论代码按 UAT 记录的 `87105ea7` 回归结果整理。
