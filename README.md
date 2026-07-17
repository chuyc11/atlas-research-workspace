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
- Node.js 22.15+

首次克隆时同时拉取两个独立子仓的固定版本：

```powershell
git clone --recurse-submodules https://github.com/chuyc11/atlas-research-workspace.git
```

已有工作区升级后执行：

```powershell
git submodule sync --recursive
git submodule update --init --recursive
```

首次安装：

```powershell
python -m pip install -r requirements.txt
npm ci --prefix src
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

默认命令运行根层全部测试、global-briefing 全部测试、trading-core 的跨项目集成门禁，以及前端构建测试。
发布候选可运行完整 trading-core 回归（耗时明显更长）：

```powershell
python atlas.py test --full
```

根仓开发者的快速质量门禁：

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
python -m ruff check atlas.py tests work/global-briefing/scripts work/global-briefing/tests
python -m pytest -q
```

根目录直接执行 `pytest` 只会收集根层与 global-briefing 测试，不会误收集 `src`、trading-core 或
`external_research` 中独立项目的测试。

当前工程质量问题、已完成修复和后续优先级见 [QUALITY_AUDIT.md](QUALITY_AUDIT.md)。

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
python atlas.py cycle --date 2026-07-10 --full-tests
```

cycle 的稳定产物：

- 规范化虚拟执行账本：`work/shared/atlas/virtual_execution/atlas_virtual_execution_ledger.jsonl`
- 账本状态快照：`work/shared/atlas/virtual_execution/atlas_virtual_execution_state.json`
- 账本审计：`work/shared/atlas/virtual_execution/atlas_virtual_execution_audit.json`
- 运行审计：`work/shared/atlas/run_audits/atlas-cycle-YYYY-MM-DD.json`
- 哈希链运行历史：`work/shared/atlas/run_audits/history/YYYY-MM-DD/ATLAS-CYCLE-*-RUN-*.json`
- 三仓来源锁：`work/shared/atlas/workspace-lock.json`

cycle 使用内容指纹、三仓 `workspace-lock` 内容哈希和稳定事件 ID 保持幂等；代码或任一仓 commit 改变后必须重新完成两次运行，
账本、状态和账本审计在输入不变时保持字节稳定，
每次调用则单独保留一份运行历史。相同源事件不会被静默去重，而会触发阻断并保留上一份已通过审计的 canonical ledger。
cycle 通过 `work/shared/atlas/cycle.lock` 阻止并发运行；doctor 或 sync 上游门禁失败时只继续生成诊断和运行审计，不写 canonical ledger。
每日自动生成的 `temp-orders-YYYY-MM-DD.json` 会作为 `virtual_order_intent` 进入 canonical ledger；旧版下划线文件只作兼容输入，
同日两个非空别名并存会 fail-closed；
实际旧账本仍不会被 cycle 直接追加。

cycle 的 `overall_passed` 仅为向后兼容字段，语义等同 `operational_gate_passed`。`release_candidate_passed`
还要求 `--full-tests`、同一 workspace-lock 上输入不变的幂等复跑、三仓干净且每个当前 commit 都被至少一个远端 ref 明确发布；
`research_promotion_passed` 单独表示研究证据门禁，三者不能互相替代。普通站点同步在门禁或冻结证据不足时只写 staging，
不会覆盖可部署 JSON；只有与当前报告、输入、载荷、门禁 artifact 和三仓 commit 统一绑定的冻结快照才能进入站点。

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

`src/` 与 `work/trading-core/` 作为 Git 子模块保留各自独立历史；根仓只固定两个不可变 commit。
不要在根仓用重置命令覆盖子仓工作树。若子模块目录缺失，运行
`git submodule update --init --recursive` 恢复，不要手工复制本机目录冒充可复现工作区。

`atlas doctor` 会报告根仓与两个子仓的 commit、分支、工作树状态，并用只读远端探测验证当前完整 commit SHA
确实出现在至少一个远端 ref 中，而不只验证远端地址可连通。
缺少远端或存在未提交改动会显示警告：这不影响本地研究，但会阻断可重复发布。三个仓库必须分别通过自己的质量门禁。

## 虚拟执行与自我进化边界

- 唯一规范化账本是 `work/shared/atlas/virtual_execution/atlas_virtual_execution_ledger.jsonl`。
- `work/global-briefing/data/paper_trades_us.jsonl`、`paper_trades_china.jsonl` 和
  `work/trading-core/data/replays/global_briefing/trades/*.jsonl` 是只读历史来源。
- `work/global-briefing/data/temp-orders-*.json` 是自动虚拟订单意图来源，只写入 canonical ledger。
- 账本审计要求所有事件 `paper_trading_only=true`、`no_real_broker_order=true`，并拒绝 broker/live order 字段。
- paper-trading 写入器以固定顺序持有全部账户锁，使用 `preparing → prepared → committed` durable coordinator、全账户 journal、commit marker 与 generation 校验提交组合和账本；部分写日志、半发布或部分清理后崩溃均可恢复，读者不会看到半批。重复 ID、重复指纹、缺少可验证指纹的 legacy replay、币种冲突、日期倒退和过期成交价全部 fail-closed。
- replay gate 只接受 isolated historical replay evaluation，要求主账本未写入、`run-daily` 未调用、no-trade fallback 未启用。
- replay evaluation 按文件名中的回放结束日期选择 cycle 日期之前的最新周期，不依赖文件修改时间。
- shadow promotion gate 由 verified out-of-sample evidence 驱动；无证据时保持 shadow，且不会自动晋升到 active-normal。
- 影子证据的日期、样本数、收益、错误率、回撤、成本和来源链字段均采用 fail-closed 校验；畸形证据只会保持 shadow。
- 2026-07-12 起新增预测使用 v2 预注册契约：数值概率、明确截止日、成功/失败判定规则、证据快照与基准化市场映射。
- 2026-07-16 起市场解析必须使用预注册会话窗口与截止会话，保存原始起止价格，并由质量门禁重算标的、基准和超额收益。
- 旧的高/中/低概率和状态默认分只作兼容展示，不进入 Brier、Log Loss 或 ECE；研究晋升只接受合格到期解析样本。

## 安全与恢复

- Node 审计固定使用 npm 官方安全端点；当前策略不保留漏洞例外，未来例外必须有责任人、理由和到期日，high/critical 永不豁免。
- 根 CI 固定 GitHub Action 提交，执行 Ruff、Bandit、秘密扫描、Python 依赖审计、60% 覆盖率及 Python 3.11/3.12 矩阵。组合工作区 checkout 对私有子仓要求仓库 secret `ATLAS_SUBMODULE_TOKEN`；缺失时明确失败，不会降级成缺子仓的假绿。
- 灾备写入 `D:/ATLAS-Backups`，要求与工作区不同卷；生产配置强制使用 AES-256-GCM。32 字节密钥以 Base64 放入密钥管理器提供的 `ATLAS_BACKUP_ENCRYPTION_KEY` 环境变量，不得写入仓库。schema 4 快照使用分用途 HMAC 认证 manifest sidecar 与 latest 索引，前序介质必须先通过 AEAD、逐文件哈希以及 Git `bundle verify → mirror clone → fsck` 才能接链；明文 staging 仅位于受保护的备份目标并可靠清理。旧 schema 2 `latest.json` 必须由运维显式归档后建立新的加密 genesis，系统不会静默信任迁移。该证据不宣称操作系统凭据等完整运行时已恢复。
- 高/严重改进项需要确认；告警具备稳定 ID、重试上限、确认超时和升级状态。仅生成路由文件不算送达，每个目的地必须记录不可变回执后才能确认：`python atlas.py alerts --date YYYY-MM-DD --receipt-destination DESTINATION --receipt-id RECEIPT_ID`。真实 connector 回执还会更新独立、带新鲜度约束的 `channel_health.json`，使健康日能力验收保持幂等；需确认告警只有到 `acknowledged` 才通过。Slack 连接器未返回回执时不得宣称已送达。

预测系统的分层设计、契约示例和晋升标准见 [RESEARCH_ARCHITECTURE.md](RESEARCH_ARCHITECTURE.md)。

## 合并边界

- 已统一：启动入口、依赖检查、预测信号格式、跨项目测试、网页同步。
- 已统一：cycle 入口、规范化虚拟执行账本、幂等账本重建、安全门禁、replay/shadow 验证和运行审计。
- 保持隔离：旧账本原始文件、trading-core 的大量版本化审计产物、简报的原始报告库。
- 后续若要迁移旧写入方，应先做只读对账和双跑校验，不能直接覆盖历史文件。
