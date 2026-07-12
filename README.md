# ATLAS 统一研究工作台

这个目录现在是一套可统一运行的研究项目，而不是把两个成熟代码库强行揉成一个包：

- `work/global-briefing/`：新闻、市场快照、预测、虚拟组合与中文晨报。
- `work/trading-core/`：结构化研究、回放、A 股研究平台与审计能力。
- `src/`：ATLAS 网页展示层。
- `atlas.py`：统一入口，负责健康检查、数据桥接、统一 cycle、规范化虚拟执行账本、测试和站点启动。

这种组合式合并保留了 trading-core 的现有路径、测试和历史产物。旧 US/CHINA paper ledger 与
trading-core isolated replay ledger 现在作为只读来源保留；ATLAS 规范化虚拟执行事实源统一写入
`work/shared/atlas/virtual_execution/atlas_virtual_execution_ledger.jsonl`。统一入口不会创建真实订单。

## 环境

- Python 3.11+
- Node.js 22.13+

首次安装：

```powershell
python -m pip install -r requirements.txt
Set-Location src
npm install
Set-Location ..
```

## 常用命令

检查整个工作台：

```powershell
python atlas.py doctor
```

把最新晨报预测转换为 trading-core 宏观信号，同时更新网页数据：

```powershell
python atlas.py sync
```

指定日期或仅检查、不写文件：

```powershell
python atlas.py sync --date 2026-07-10
python atlas.py sync --date 2026-07-10 --dry-run
```

指定日期会同时应用于预测导出、记录验证、trading-core 宏观信号和网页数据。
`--dry-run` 会执行这四层解析与校验，但不会写入宏观信号副本、网页 JSON 或同步状态。

运行跨项目验证：

```powershell
python atlas.py test
```

审计内容质量、预测契约和概率校准就绪度：

```powershell
python atlas.py quality --date 2026-07-11
python atlas.py quality --date 2026-07-11 --strict
```

普通审计只在运行契约或已生效的内容门禁失败时返回非零；`--strict` 还要求 Brier、Log Loss、ECE、样本量和解析覆盖率全部达到研究晋升门槛。机器可读结果写入
`work/global-briefing/data/research-quality-YYYY-MM-DD.json`。

运行完整门控 cycle（doctor → sync → 唯一规范化虚拟账本 → replay/shadow gate → tests → run audit）：

```powershell
python atlas.py cycle --date 2026-07-10
python atlas.py cycle --date 2026-07-10 --dry-run
```

cycle 的稳定产物：

- 规范化虚拟执行账本：`work/shared/atlas/virtual_execution/atlas_virtual_execution_ledger.jsonl`
- 账本状态快照：`work/shared/atlas/virtual_execution/atlas_virtual_execution_state.json`
- 账本审计：`work/shared/atlas/virtual_execution/atlas_virtual_execution_audit.json`
- 运行审计：`work/shared/atlas/run_audits/atlas-cycle-YYYY-MM-DD.json`
- 不可变运行历史：`work/shared/atlas/run_audits/history/YYYY-MM-DD/ATLAS-CYCLE-*-RUN-*.json`

cycle 使用内容指纹和稳定事件 ID 保持幂等；账本、状态和账本审计在输入不变时保持字节稳定，
每次调用则单独保留一份运行历史。相同源事件不会被静默去重，而会触发阻断并保留上一份已通过审计的 canonical ledger。
cycle 通过 `work/shared/atlas/cycle.lock` 阻止并发运行；doctor 或 sync 上游门禁失败时只继续生成诊断和运行审计，不写 canonical ledger。
每日自动生成的 `temp_orders_YYYY-MM-DD.json` 会作为 `virtual_order_intent` 进入 canonical ledger；
实际旧账本仍不会被 cycle 直接追加。

启动网页：

```powershell
python atlas.py serve
```

单独构建并测试网页：

```powershell
python atlas.py build-site
```

安装根项目后，也可以把 `python atlas.py` 换成 `atlas`：

```powershell
python -m pip install -e .
atlas doctor
```

## 数据桥接

`work/global-briefing/scripts/export_trading_core_signals.py` 会：

1. 读取 `work/global-briefing/data/predictions.jsonl`。
2. 选择指定日期仍为 open/active 的预测。
3. 只保留 trading-core `config/universe_china_etf.yaml` 支持的中国/香港 ETF。
4. 原子写入 `work/global-briefing/data/macro_signals-YYYY-MM-DD.jsonl`。
5. 由 trading-core 的现有 `load-macro` 命令验证并复制到其本地数据目录。

该桥接层只生成研究信号文件，不调用旧 `run-daily`，不创建订单，不写真实账户。

虚拟交易费用按市场配置。A 股和港股账户与
`work/trading-core/config/broker_rules.yaml` 使用相同的佣金最低收费和卖出印花税假设；
既有历史成交保持原始记录，新成交开始应用该费用模型。

## 版本控制恢复

根目录如果出现空 `.git/`，不要直接覆盖或假定历史不存在。先备份工作区并确认原远端、备份或
工作树元数据；无法恢复时，再明确选择建立新的集成仓库。`src/` 与
`work/trading-core/` 仍保留各自独立的 Git 历史。

## 虚拟执行与自我进化边界

- 唯一规范化账本是 `work/shared/atlas/virtual_execution/atlas_virtual_execution_ledger.jsonl`。
- `work/global-briefing/data/paper_trades_us.jsonl`、`paper_trades_china.jsonl` 和
  `work/trading-core/data/replays/global_briefing/trades/*.jsonl` 是只读历史来源。
- `work/global-briefing/data/temp_orders_*.json` 是自动虚拟订单意图来源，只写入 canonical ledger。
- 账本审计要求所有事件 `paper_trading_only=true`、`no_real_broker_order=true`，并拒绝 broker/live order 字段。
- paper-trading 写入器按账户加锁并使用恢复日志提交组合与成交账本；相同 `order_id` 或相同订单内容会幂等复用，复用 ID 但修改内容会 fail-closed，同批后续订单失败不会留下半事务。
- replay gate 只接受 isolated historical replay evaluation，要求主账本未写入、`run-daily` 未调用、no-trade fallback 未启用。
- replay evaluation 按文件名中的回放结束日期选择 cycle 日期之前的最新周期，不依赖文件修改时间。
- shadow promotion gate 由 verified out-of-sample evidence 驱动；无证据时保持 shadow，且不会自动晋升到 active-normal。
- 影子证据的日期、样本数、收益、错误率、回撤、成本和来源链字段均采用 fail-closed 校验；畸形证据只会保持 shadow。
- 2026-07-12 起新增预测使用 v2 预注册契约：数值概率、明确截止日、成功/失败判定规则、证据快照与基准化市场映射。
- 旧的高/中/低概率和状态默认分只作兼容展示，不进入 Brier、Log Loss 或 ECE；研究晋升只接受合格到期解析样本。

预测系统的分层设计、契约示例和晋升标准见 [RESEARCH_ARCHITECTURE.md](RESEARCH_ARCHITECTURE.md)。

## 合并边界

- 已统一：启动入口、依赖检查、预测信号格式、跨项目测试、网页同步。
- 已统一：cycle 入口、规范化虚拟执行账本、幂等账本重建、安全门禁、replay/shadow 验证和运行审计。
- 保持隔离：旧账本原始文件、trading-core 的大量版本化审计产物、简报的原始报告库。
- 后续若要迁移旧写入方，应先做只读对账和双跑校验，不能直接覆盖历史文件。
