# ATLAS 研究级预测架构

## 目标与边界

ATLAS 的研究对象不是“新闻是否重要”，而是三个彼此独立的问题：

1. 事件是否在预注册期限内发生。
2. 事件是否通过预期机制传导。
3. 指定资产是否相对预注册基准产生预期方向的表现。

事件判断正确不能替代资产映射正确。主观复盘分只用于诊断，不能作为概率校准或模型准确率。

## 分层架构

```text
来源发现（RSS/公开检索）
  → 证据快照（来源、时间、层级、独立域）
  → 事件 dossier（事实/主张/反证/机制）
  → v2 预测预注册（概率、截止日、判定标准）
  → 市场映射（标的、方向、基准、验证规则）
  → 到期解析（observed_outcome + 解析证据）
  → 正确评分（Brier / Log Loss / ECE）
  → 影子晋升门禁（样本、覆盖、校准、数据完整性）
```

运行安全、内容质量和预测有效性分别审计，任何一个都不能替代另一个：

- `atlas doctor`：运行环境与依赖。
- `atlas quality`：预测契约、报告内容与研究指标。
- `atlas cycle`：同步、虚拟账本、隔离回放、测试与不可变运行审计。

## v2 预测契约

2026-07-12 起的新预测必须满足 `work/global-briefing/config/prediction.schema.json`。最小示例：

```json
{
  "schema_version": 2,
  "prediction_id": "2026-07-12-P01",
  "date": "2026-07-12",
  "deadline": "2026-07-13",
  "horizon": "1d",
  "scenario": "在截止日前可被二元判定的命题",
  "probability": 0.68,
  "status": "open",
  "trigger": "触发预测的新增信息",
  "verification_signals": ["支持信号"],
  "falsification_signals": ["反驳信号"],
  "resolution": {
    "question": "截止日前命题是否发生？",
    "success_criteria": "可重复执行的成功判定规则",
    "failure_criteria": "可重复执行的失败判定规则"
  },
  "evidence": [
    {"source": "一手来源", "url": "https://example.org/source", "tier": 1}
  ],
  "market_mapping": [
    {
      "symbol": "510300.SH",
      "direction": "outperform",
      "benchmark": "000300.SH",
      "verification_rule": "截止日总回报高于基准",
      "evaluation_deadline": "2026-07-13"
    }
  ]
}
```

到期复盘必须追加解析记录，不能覆盖原始预测。研究评分要求 `review.observed_outcome` 为 `0` 或 `1`，并保存解析证据。提前结束必须明确 `terminal_evidence=true`。

事件解析截止日与资产映射截止日可以不同。周末或休市日创建的预测必须把
`market_mapping.evaluation_deadline` 对齐到下一可观察交易时点，禁止用陈旧收盘价解析新资产预测。

## 研究晋升门禁

默认门槛位于 `config/settings.json`：

- 合格解析样本至少 30 条。
- 到期预测的合格解析覆盖率至少 80%。
- Brier score 不高于 0.25。
- Expected Calibration Error 不高于 0.15。
- 新增预测全部通过 v2 契约，报告通过来源、篇幅与结构门禁。

这些门槛用于“能否称为研究就绪”，不是收益承诺。样本不足时系统保持 `shadow`，不会用状态默认分填补缺失结果。

## 内容质量契约

每个核心主题只保留：结论、硬证据、因果机制、反证、证伪信号。高影响主题至少包含一手/机构来源、事件地区来源和独立外部来源；通讯社转载不能被重复计算为多个独立证据。

`research_quality.py` 会审计报告长度、链接、独立域、必要章节和论证层。发现缺口时降低置信度或保持开放，不用增加泛化背景来填充篇幅。

## 历史数据政策

旧记录保持不可变，分类概率（高/中/低）和状态默认分仅作为兼容数据展示。它们不会进入 Brier、Log Loss 或校准误差。历史记录只有在存在当时的可验证原始证据时，才能通过单独、可审计的迁移记录纳入研究样本；禁止事后补概率。
