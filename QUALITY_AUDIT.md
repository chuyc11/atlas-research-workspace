# ATLAS 工程质量审计

审计日期：2026-07-15

## 结论

ATLAS 的运行安全与研究门禁已经较强，但此前的工程反馈链存在两类“本机假绿”：根目录默认
`pytest` 会误收集第三方研究仓库并产生 504 个导入错误；改成全量根测试后，又有 9 项测试依赖
被忽略的本地历史工件，无法在干净克隆中复现。本轮已修复这两类问题，并建立静态检查、完整根测试、
包入口验证和依赖自动更新门禁。

当前仍不能把项目判定为“可重复发布”。`src` 与 `work/trading-core` 是独立仓库，但两者都没有配置
Git 远端且工作树非干净。根仓 CI 只能证明根仓控制面和 global-briefing，不能替代两个子仓的质量结果。

## 问题清单

| ID | 问题 | 影响 | 状态 |
|---|---|---|---|
| Q-001 | 根目录 `pytest` 误收集 `external_research` | 默认开发命令产生 504 个无关导入错误 | 已修复 |
| Q-002 | 测试依赖被忽略的 outputs/data | 本机全绿、干净 CI 失败 | 已修复；8 项历史工件测试显式分层 |
| Q-003 | 根 CI 手工点名部分测试 | 新测试可不进入 CI | 已修复；CI 运行全部根拥有测试 |
| Q-004 | 无 Python 静态质量门禁 | 未使用导入、异常链、集合重复等问题长期累积 | 已修复；Ruff 已启用且零告警 |
| Q-005 | 子仓来源不可重建 | 无法从任意环境复现发布工作区 | 开放，发布阻断 |
| Q-006 | 大文件与职责集中 | 评审困难、变更半径大、单元测试隔离成本高 | 开放，架构债务 |
| Q-007 | trading-core 快速集与全量集缺少统一入口 | 日常反馈和发布证据容易混淆 | 已修复；新增 `atlas test --full` |
| Q-008 | 跨仓集成测试产生 16 个日历回退警告 | 真实新警告可能被噪声淹没 | 开放，应建立精确警告预算 |
| Q-009 | 根 CI Action 仅使用浮动大版本标签 | 上游标签变化会降低供应链可重复性 | 已修复；固定 commit 并由 Dependabot 更新 |

## 验证证据

- 根仓静态检查：Ruff 通过，0 项。
- 根仓完整工作区：124 passed，1 subtest passed。
- 干净根仓仿真：116 passed，8 skipped，1 subtest passed；无子仓、无历史 outputs/data。
- 跨仓快速门禁：trading-core 33 passed；前端构建和 4 项 Node 测试通过。
- 安装验证：editable wheel 构建成功，`atlas --help` 成功。
- 现有 trading-core 完整 JUnit：2,149 tests，0 failure，0 error，1 skipped，耗时 1,383.616 秒；
  该证据生成于 2026-07-14，不替代本轮变更后的发布候选全量回归。

## 借鉴的成熟项目原则

本轮没有复制大型框架，而是采用与当前规模匹配的工程原则：

- [Microsoft Qlib](https://github.com/microsoft/qlib) 强调松耦合、可独立使用的研究组件；ATLAS 应继续把
  来源、预测、组合、执行和展示拆成带契约的边界，而不是继续扩充单文件脚本。
- [QuantConnect LEAN](https://github.com/QuantConnect/Lean) 采用可替换组件和明确 CLI 工作流；ATLAS 的
  下一步应把 `atlas.py` 的账本、门禁、工作区探测和命令编排拆成内部包与稳定接口。
- [Freqtrade](https://github.com/freqtrade/freqtrade) 把 dry-run 作为安全默认值；ATLAS 现有
  `paper_trading_only` 与 `no_real_broker_order` 边界应继续作为不可绕过的系统契约。

## 后续优先级

### P0：发布前

1. 为 `src` 与 `work/trading-core` 配置可获取远端，记录不可变 commit，并清理或提交各自工作树。
2. 在同一发布 commit 上运行 `python atlas.py test --full`，保存 JUnit 和三仓 commit 清单。
3. 完成生产边缘、源站隔离、响应头和网络出口的仓库外验证；这些不能由本地测试替代。

### P1：近期架构治理

1. 将 `atlas.py` 拆为 `workspace`、`ledger`、`gates`、`commands` 四个内部模块，保留薄 CLI。
2. 将 `sync_briefing_site.py`、`research_quality.py`、`paper_trading.py` 从脚本变为可安装包，移除
   `sys.path` 注入与 E402 例外。
3. 为 8 个历史工件集成测试建立最小、脱敏、版本化 fixture，使干净 CI 不再需要跳过。
4. 将日历回退警告改为精确 allowlist，并对所有新增 warning 失败。
5. 建立覆盖率基线，先按关键域设阈值，不以单一全局百分比替代风险覆盖。

### P2：规模控制

1. 合并 trading-core 中重复的 A 股报告/manifest/audit 模板，避免一个版本阶段复制一组模块。
2. 为三仓建立机器可读 workspace lock，记录路径、远端、commit、质量结果与产物哈希。
3. 将快速、集成、全量、生产验收四类证据分别归档，避免“某一层绿色”被解释为整体上线通过。
