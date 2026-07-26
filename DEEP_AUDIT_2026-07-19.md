# ATLAS 项目深度审计报告

- 审计日期：2026-07-19
- 报告版本：v8（v7 三方审核后订正，待第八轮三方复审）
- 审计对象：根控制面、`work/global-briefing`、`work/trading-core`、`src` 前端与线上站点
- 审计性质：只读代码审查、负向实验、测试与线上验证；未修改业务源码

## 1. 直接结论

ATLAS 目前不能被认定为“已经没有问题、启动后不会再出现这些问题、修改已自动生效”。这不是因为普通单元测试大量失败：相反，本机常规测试大多是绿色的；真正的问题是若干关键门禁只验证自声明字段或可回放的本地证据，导致“测试通过、审计通过”与实际金融语义、发布真实性和防回滚能力之间仍有落差。

本次没有发现当前 609 条 canonical ledger 事件已经被实际篡改，也没有发现 2026-07-17/18 的现存运行历史已经损坏。但这只能表明“当前文件没有显露异常”，不能证明机制能够抵抗下文已经复现的改写、回滚和证据伪造。

最需要立刻处理的事实是：

1. canonical ledger 的成交价、手续费、税费等历史语义可以变化，而连续性仍报告通过。
2. 已签名的旧账本、旧运行历史和旧锚可以整体回放，HMAC 仍然有效。
3. 发布备份门和 `--mark-deployed` 可以接受没有可信签名来源的本地证据。
4. 前端独立 CI 会接受当前未冻结、payload 哈希错误的发布清单。
5. 线上站点虽然 HTTP 200，但仍是 2026-07-16 内容；2026-07-18 候选明确处于阻断状态。
6. trading-core 的交易日历只到 2026-07-31；当前实现从 8 月开始会把 A 股交易日判成休市或直接抛异常。
7. 三个修复分支都已推送，但都没有 PR、没有对应 GitHub Actions 运行，也没有进入默认分支，因此不会自动生效。

## 2. “上传了”与“生效了”不是一回事

| 仓库 | 当前分支/HEAD | 已推送 | 相对默认分支 | PR | 该分支 CI |
|---|---|---:|---:|---:|---:|
| 根仓 | `codex/integrity-remediation-clean-20260718` / `e78904b` | 是 | 比 `origin/main` 多 12 提交 | 无 | 无 |
| 前端 | `codex/integrity-remediation-site-20260718` / `2e5f7d4` | 是 | 比 `origin/main` 多 11 提交 | 无 | 无 |
| trading-core | `codex/integrity-remediation-20260718` / `249ed6c` | 是 | 比 `origin/master` 多 8 提交 | 无 | 无 |

三仓的 feature-branch push 均不触发 CI；根仓另有 `workflow_dispatch`，trading-core 另有 `schedule` 和 `workflow_dispatch`，但当前分支也没有对应的手动或定时运行。`gh pr list --head <当前分支>` 和 `gh run list --branch <当前分支>` 在三仓均为空。因此准确说法是：代码上传到了远端分支，但没有合并、没有远端验收、没有部署。

根仓 `README.md:148` 也明确写明，生产上线需要独立可信部署、生产验证和 `--mark-deployed`，所以 `atlas cycle` 本来就不是自动部署器。如果目标是“修改后自动上线网页”，那是尚未实现的产品能力，而不是现有 cycle 的隐含行为。

当前站点状态：

- 线上 URL：[ATLAS 全球简报](https://atlas-global-brief-2026.poetic-kiwi-4295.chatgpt.site)
- HTTP：200
- 线上报告日：2026-07-16
- 本地最新简报：2026-07-18
- 本地候选：`staged_status=blocked`，有 6 条严格门禁原因
- 项目生产验证器：失败

生产验证器的关键输出为：

```text
local publication manifest must be schema 1 and frozen
local publication manifest payload hash does not match the site payload
local publication identity snapshot_revision is invalid
production HTML observed_* publication fields do not match
```

这说明网页不是不存在，而是新简报没有经过可验证的安全发布。

## 3. 审计方法与判断理由

本报告不提供不可核验的内部思维独白，而提供可复查的“证据—判断—结论”链。过程如下：

1. 建立三仓库分支、HEAD、远端、PR、CI、版本、依赖和工作区状态基线。
2. 按控制边界拆成根完整性、全球简报金融语义、trading-core 和前端/线上发布四条审计线。
3. 先运行项目已有正向测试，再构造不会改工作区的临时目录或内存负向实验。
4. 对上一份外部审查逐项核验，明确区分“已修复、部分修复、仍未修复、原说法无法复现”。
5. 使用项目自己的生产验证器直接访问线上站点，而不是相信本地部署状态字段。
6. 对时效性规则使用交易所官方来源复核，避免沿用过时的统一 ST 5% 说法。
7. 报告完成后由三个子智能体分别偏重完整性/安全、金融/数据、测试/发布，同时对整份报告做全局审核；有任何有效异议就订正并重新三方审核。

严重度口径：

- P0/Critical：能够完整绕过项目声称的核心完整性或发布真实性控制，或会在极近日期造成主流程停摆。
- P1/High：可造成公开金融信息错误、发布状态假绿、重大数据撕裂或干净环境不可运行。
- P2/Medium：在特定输入、市场或故障条件下产生实质错误，但影响范围较窄或需要本地权限。
- P3/Low：不会直接改变金融结果，但持续削弱诊断、维护或可复现性。

## 4. 测试与验证基线

| 范围 | 结果 | 解释 |
|---|---|---|
| 根仓 Python | 296 passed；覆盖率 71.20% | 总体通过，但 `china_market.py` 仅 15%、`resolution_evidence.py` 38% |
| Ruff / compileall | 通过 | 语法与静态风格无阻断 |
| Bandit 中高危 | 0 | 没发现 Bandit 可识别的中高危模式，不代表业务控制正确 |
| 根/global pip-audit | 0 已知漏洞 | 当前锁定依赖未命中已知漏洞库 |
| 前端 `npm run quality` | lint/typecheck/build/13 tests 通过 | 当前无效 publication manifest 仍被接受，属于本报告的假绿实证 |
| 前端 `npm audit` | 0 active advisory | 当前 React/RSC/Vite 版本已越过已知修复线 |
| global 定向回归 | 30 passed | 没覆盖本报告负向反例 |
| trading-core 本机全量 | 2165 passed，1 skipped，27:02 | 本机额外安装了未声明的 `pyarrow 24.0.0` |
| trading-core 最近一次默认分支 GitHub 定时回归 | `master@70308a9`：1730 passed，419 failed，1 skipped | 不是当前修复 HEAD；418 项缺 Parquet engine，1 项 POSIX 路径断言失败 |
| 线上验证 | HTTP 200，但发布验证失败 | 站点可访问不等于最新候选已可信部署 |

trading-core 的失败证据来自 [GitHub Actions run 29607819146](https://github.com/chuyc11/atlas-trading-core/actions/runs/29607819146)，对应默认分支 `master@70308a9`，不是当前审计分支 `249ed6c`。当前 HEAD 没有远端完整回归；它仍未声明 Parquet engine，且 POSIX 路径断言也未修，因此“干净环境仍会失败”是由代码与最近默认分支运行共同支持的推断，不能把旧 run 的精确计数冒充当前 HEAD 实跑结果。PR 门只跑精选测试，完整回归只在 schedule/workflow_dispatch 执行，见 `.github/workflows/quality.yml:30-52,78-96`。

前端当前依赖没有命中已知 RSC/Vite 漏洞：React 19.2.6 已高于相关公告的受影响上界，`@vitejs/plugin-rsc` 0.5.26 和 Vite 8.1.4 也已在修复版本之后。依据：[React 公告](https://github.com/facebook/react/security/advisories/GHSA-rv78-f8rc-xrxh)、[Vite RSC 公告](https://github.com/vitejs/vite-plugin-react/security/advisories/GHSA-w94c-4vhp-22gx)、[Vite 公告](https://github.com/advisories/GHSA-p9ff-h696-f583)。这是已核实的正面结果。

## 5. P0 / Critical 发现

### ATLAS-CTRL-001：canonical ledger 可静默改变既有事件的完整金融语义

- 位置：`atlas.py:1409-1414,1609-1623,2075-2084`
- 证据：连续性只比较 source locator、source hash 和 ledger event id，不比较同一 locator 的完整 canonical event。
- 负向结果：改变 `filled_price/fee/tax/status` 后，`canonical_fields_changed=true`，但 `baseline_verified=true`、`mutated_locators=[]`、`blocking_reasons=[]`。
- 判断理由：规范账本并非严格 append-only；代码升级或被替换的 canonicalizer 可以无迁移记录地改写历史结果。
- 修复：对已存在 locator 保存并核验完整规范事件哈希；schema 迁移必须有显式版本、迁移映射、迁移前后双哈希和独立审计。

### ATLAS-CTRL-002：账本与运行历史可回放旧的已签名快照

- 位置：账本 `atlas.py:1431-1451,1481-1499,1595-1608,2081-2084`；历史 `atlas.py:638-662,740-775`
- 证据：锚是可覆盖的本地签名 JSON，没有外部单调序号、可信时间戳或不可回退 witness。
- 负向结果：恢复旧 ledger/state/anchor 后继续 `verified=true`；即使 `cycle_state` 仍指向新版本也不阻断。旧 history/anchor 同样通过。
- 判断理由：HMAC 能发现未知修改，却无法区分“最新有效签名”和“以前的有效签名”。
- 修复：将链尾提交到 WORM、透明日志或外部单调计数服务；交叉绑定 ledger、cycle_state、history 和发布快照。

### ATLAS-REL-001：站点发布备份门只检查认证字段外形，不验真实备份

- 位置：`sync_briefing_site.py:3564-3575,3578-3716`；真实验签实现位于 `disaster_recovery.py:207-243`
- 证据：门禁只检查算法名、key id 与 HMAC 字符串格式，再比较攻击者也能重算的 archive/manifest SHA-256；不验证 HMAC、AES-GCM tag 或容器 magic。
- 负向结果：纯文本假 `.atlasdr`、全零伪 HMAC 和自洽 manifest 得到 `shape_errors=[]`、`binding_errors=[]`。
- 测试缺口：`test_workspace_sync.py:1616-1667,1713-1718` 使用任意 HMAC 和普通字节 fixture，反而固化了假证据可通过。
- 修复：发布门必须调用真实 archive verification，或只接受由备份流程签名且可独立验证的证明。

### ATLAS-REL-002：`--mark-deployed` 信任可编辑的离线 JSON

- 位置：`sync_briefing_site.py:2349-2462,4074-4100`
- 证据：只读取 JSON 中的 `passed`、observed 字段、DNS pinned、IP 和时间戳；不重新联网，也不验证 CI/部署控制面签名。
- 负向结果：完全离线构造的自洽 JSON 得到 `forged_offline_artifact_accepted=true`、`errors=[]`。
- 判断理由：实际站点未更新时仍可把本地状态写成 deployed，污染幂等判断和后续审计。
- 修复：命令内部直接运行生产验证器，或只接受有受信工作流身份、签名和不可重放 nonce 的 deployment attestation。

### ATLAS-CAL-001：trading-core 日历将在 2026-08-01 后实质失效

- 位置：`trading_calendar.py:24-37,45-70,100-116`；构建器 `equity_universe/calendar.py:33-43`
- 证据：跟踪日历最大日期为 2026-07-31。
- 负向结果：2026-08-03 返回 `calendar_file_missing_date` 且 `is_trading_day=false`；从 7 月 31 日求下一交易日直接 `ValueError`。
- 主流程证据：`daily_run.py:45-47,85` 每次日常运行都会调用 `previous_trading_day`，所以这不是未被使用的底层边界。
- 判断理由：这是临近的确定性可用性故障，会影响 T+1、信号执行和回放，不是遥远边界条件。
- 修复：启动门要求至少覆盖未来 90 日，自动刷新；缺失日期不得静默判休市；官方 2027 日历发布后及时更新并再次验收。

## 6. P1 / High 发现

### ATLAS-SITE-001：前端独立 CI 接受未冻结且哈希错误的 publication manifest

- 位置：`src/package.json:13-19`、`src/tests/rendered-html.test.mjs:91-106`、`src/app/publication.generated.json:3-16`
- 当前事实：manifest 为 `frozen=false`、`snapshotRevision=0`，记录 payload SHA-256 为 `0477...`，实际按项目算法计算为 `531821...`。
- 项目真实校验器返回 3 个错误，但 `npm run quality` 的 lint、typecheck、build 和 13 项测试全部通过。
- 原因：测试只检查 64 位格式，并显式允许 `frozen=false/revision=0`；package scripts 和 GitHub workflow 都不调用 Python publication verifier。
- 修复：CI 必须执行 `publication_identity`/完整生产前校验；发布构建只能接受 frozen=true、revision>0、payload 哈希一致且 commit 身份匹配。

### ATLAS-SITE-002：线上验证器把页面自报 marker 当作内容真实性

- 位置：`verify_production_site.py:194-223,246-296,526-575`；marker 由 `src/app/page.tsx:308-315` 输出。
- 负向结果：构造显示内容为 `TAMPERED CONTENT`、但保留正确 `data-atlas-*` marker 和安全头的 HTML，`evaluate_live_response` 返回 `passed=true, errors=[]`。
- 原因：验证器比较由同一 HTML 自报的 marker，不计算可见内容摘要；`source_digest` 虽被计算，却没有与冻结身份或外部锚比较。
- 判断理由：边缘层或部署产物可以改变展示内容而保留 marker，生产验证仍可能假绿。
- 修复：对部署产物/渲染结果生成受信 digest 并外部锚定；或公开可验证的签名 payload，验证器独立重建并比较关键展示字段。

### ATLAS-SITE-003：发布 retry 使用锁外旧 state，严格重验函数是零调用死代码

- 位置：重验定义 `sync_briefing_site.py:2774-2886`；旧 state 读取/使用 `2968-2974,3035-3044,3087-3097,3140-3151,3248-3256,3999`
- 证据：全仓只有 `revalidate_retry_authority` 定义，没有调用。
- 负向结果：并发进程把 active candidate 改到 2026-07-20 后，旧 retry 仍返回 0 并提交 2026-07-19 state。
- 影响：旧候选可能覆盖或清理新候选，形成发布 TOCTOU。
- 修复：stage/retry/mark-deployed 共用跨日期事务锁；获得锁后重新读取和验证 state，只提交重验返回值。

### ATLAS-SEC-001：当前外部锚和备份目录 ACL 不满足“受保护”假设

- 当前机器：`D:\ATLAS-Trust`、`D:\ATLAS-Backups` 均继承 `Authenticated Users: Modify, Synchronize`。
- 位置：doctor 仅检查存在性/namespace/key，`atlas.py:2307-2325`。
- 实际结果：doctor 仍把 external signed trust anchor 报为 `ok`。
- 影响：其他本机认证用户可以替换、删除或回放旧锚；与 ATLAS-CTRL-002 组合后回滚具备现实路径。
- 修复：安装/doctor 校验 ACL、owner、继承、只追加/WORM 能力；不满足时必须红灯，而不是把路径存在等同于受保护。

### ATLAS-OPS-001：子进程超时没有终止整个进程树

- 位置：`atlas.py:140-164,167-192`
- 负向结果：直接子进程超时返回 124 后，孙进程继续存活并写入临时 marker。
- 影响：已判失败的 pytest/npm/sync 后代可在门禁决策后继续改文件，并与下一 cycle 重叠。
- 测试缺口：`tests/test_atlas_cycle.py:1016-1027` 只 mock `TimeoutExpired`，不验证进程树死亡。
- 修复：Windows 使用 Job Object `KILL_ON_JOB_CLOSE`；POSIX 使用新 session/process group，超时杀组并 wait。

### ATLAS-DATA-001：公开站点把抓取日冒充中国行情价格日

- 位置：`sync_briefing_site.py:703-729`
- 当前数据：7 月 18 日中国快照 35 条的 `price_date` 全是 7 月 17 日，但 `generated_at` 是 7 月 18 日。
- 输出：公开 `asOf=2026-07-18`、`freshness=报告日`、`isStale=false`。
- 美国市场也有次级缺口：`sync_briefing_site.py:722` 只取快照中第一条有 `price_date` 的记录，不能代表页面所展示全部标的的最旧/最新估值日。
- 判断理由：周六把周五收盘标为周六行情，直接误导公开读者。
- 修复：逐项使用真实 price_date；展示最旧/最新估值日和缺失比例；周末/节假日不得把抓取时间当价格时间。

### ATLAS-FIN-001：站点与 evolution 用当前汇率重算历史成本，并丢失预测归因

- 位置：站点 `sync_briefing_site.py:748-820,919-946`；归因 `evolution.py:574-649,719-791`
- 负向结果：HK 买入 100@10、成交 FX=0.8、标记 FX=0.9，正确 base cost=800、未实现盈亏=100；站点把 cost 算成 900、盈亏=0。
- evolution 另会把同一标的 P1、P2 两笔买入合并后全部归给 P1；复现 module cost=2700，正确 base cost=2600，P2 从开放持仓归因消失。
- 根因：核心 paper account 已保存 `cost_basis_base`，复制到站点/evolution 的实现没有传导该修复，也没有 lot/prediction 维度。
- 修复：公开层只消费核心锁内快照和 base-currency lot ledger，不再复制成本算法。

### ATLAS-FIN-002：站点与 evolution 绕过多账户事务锁

- 位置：官方读取 `paper_trading.py:519-540`；批次/单账户写入 `757-766,898-910`；旁路读取 `sync_briefing_site.py:760,892-896`、`evolution.py:660-663`
- 证据：写端逐文件/逐账户提交，官方读端会拿全账户锁并恢复；两个公开消费者却直接裸读 ledger/state/valuations。
- 影响：发布窗口可见“新 ledger + 旧 state”或多账户半新半旧，形成公开撕裂快照。
- 修复：公开层必须调用官方快照 API，并验证 transaction_generation；跨账户快照要有单一 generation/commit marker。

### ATLAS-ALERT-001：外部告警“送达回执”可以任意自证

- 位置：`alert_dispatch.py:711-793`；健康度消费 `improvement_tracker.py:521-620`
- 已修部分：`alert_dispatch.py:362-519,617-620,864-890` 已真实处理 retry、ack deadline 和 escalation。
- 剩余问题：对各 destination 逐一提交任意非空 receipt id 后，状态可变成 `delivered`；任意非空 actor 只能在已经 delivered 后把状态推进为 `acknowledged`。全仓没有 Slack/Codex connector 的真实发送/回执验证实现。
- 负向结果：对每个已配置 destination 分别提交任意非空文本后，可令 `all_destinations_delivered=true`；单个 receipt 不会自动覆盖其他 destination。
- 修复：若是人工声明，状态应明确叫 operator attestation；若声称 provider receipt，必须验证 provider message id、签名或连接器查询结果。

### ATLAS-DR-001：灾备单包密码学强，但灾备历史链头可删除或回滚

- 位置：`disaster_recovery.py:1327-1336,1477-1492`
- 已修部分：AES-256-GCM、随机 nonce/AAD、metadata HMAC、重复键拒绝、安全解包、hash/size/git bundle 恢复验证均真实存在。
- 缺陷：链只信 workspace `latest.json`；删除时 previous 变空，回滚到旧的有效 latest 时从旧头分叉；不会先扫描外部备份找真实最新头。
- 影响：单个包不可伪造不等于历史不可截断。
- 修复：将 latest head 外部锚定到不可回退介质；每次备份前从外部档案重建并核验唯一链尾。

### ATLAS-DATA-002：Yahoo 中国行情 fallback 的“昨收”可能跨 5 日窗口

- 位置：`china_market.py:237,260-265`
- 证据：使用 `range=5d` 的 `meta.chartPreviousClose`，不是最后一个实际交易日 close。
- 线上只读复核：0700.HK 代码计算 +0.30%，而相邻交易日实际为 -4.63%，方向反转。
- 影响：公开涨跌、方向和事件解释错误。
- 修复：用 closes[-2] 或按交易日对齐的前一有效 bar；对 meta 字段仅作 fallback 并标注窗口语义。

### ATLAS-CAL-002：global paper trading 的业务日对所有市场仍按周一至周五

- 位置：`paper_trading.py:141-152,1913-1924,2537-2547`；站点复制逻辑 `sync_briefing_site.py:1215-1222`
- 负向结果：`business_day_age(2026-02-13, 2026-02-23)=6`，但项目权威 A 股日历显示期间 0 个交易日；`2026-07-02 → 2026-07-06` 返回 2，但 NYSE 在 7 月 3 日休市，实际只有 1 个交易日。
- 影响：A 股、港股和美股订单/估值都会在本地市场节假日后被错误判陈旧并阻断。
- 修复：按持仓市场选择 SSE/SZSE/BSE、HKEX、NYSE/Nasdaq 权威日历；日历缺失时 fail closed 并给可操作诊断。

### ATLAS-CAL-003：paper trading 会在周末、休市或收市后按不可成交价格成交

- 位置：`paper_trading.py:1850-1924`；现存账本 `paper_trades_china.jsonl:1-4,83-86`、`paper_trades_us.jsonl:97-99`
- 证据：订单执行路径既不验证标的市场当日是否开市，也不验证信号/成交时间因果。现存账本有 11 笔周六 BUY；另有 `paper_trades_china.jsonl:88,90-92` 在 15:31、16:12、16:10 生成成交，却使用沪深 15:00 或港股 16:00 已收市后的当日收盘价。
- 影响：周末或盘后新信息可以按此前已经确定的价格成交，形成现实中不可执行的成交和前视偏差，已进入组合与收益链。
- 修复：非交易日、休市和收市后订单必须明确拒绝，或排队到所属市场下一真实可交易时点并使用该时点可成交价格；记录信号时间、交易时区、session 和 execution timestamp。历史错误成交需要显式迁移/冲销，不得静默改写原账本。

### ATLAS-CI-001：trading-core 默认分支干净环境完整回归失败，当前 HEAD 仍缺同一依赖

- 位置：`.github/workflows/quality.yml:30-52,78-96`；`pyproject.toml:11-20`
- 最近默认分支 `master@70308a9` GitHub 结果：419 failures；418 项缺 `pyarrow/fastparquet`，1 项 Windows 风格路径断言在 POSIX 失败。该精确计数不是当前 `249ed6c` 的实跑结果。
- 本机为何全绿：全局环境额外安装了项目未声明的 `pyarrow 24.0.0`。
- 当前 HEAD 的静态延续证据：`pyproject.toml:11-20` 仍未声明任何 Parquet engine，相关 POSIX 断言也未修；因此本地 2165 passed 仍不可由标准安装复现。PR 可先合并，定时回归才暴露问题。
- 修复：声明 Parquet engine；PR 必跑完整回归；修正跨平台路径；增加 wheel/clean clone 测试。

### ATLAS-REL-003：trading-core 发布语义门仍由常量和旧结果驱动

- 位置：`equity_release_chain/generic.py:305-377,338-343,865-936`；`equity_v16_pit_backtest_market_rules/builder.py:214-230,450-463`
- 证据：除 pytest 派生字段外，required true/false 被直接写成期望值；空组件载荷也能让文档、冻结包、限制等字段为真。
- 当前矛盾：跟踪的 v40 结果仍声称 overall/full regression/full pytest 为真，但当前 validator 报 manifest unexpected、pytest evidence missing。
- v16 更直接：未分析数据就返回 lookahead passed、future usage false、blocker 0、full pytest true。
- 修复：每一门绑定具体产物和独立内容检查；无法验证写 `unknown/not_evaluated` 并阻断；旧结果追加 superseded/invalid 状态。

### ATLAS-REL-005：发布基线回退可能选择未来日期

- 位置：`equity_release_chain/generic.py:220-245,1118-1125`
- 证据：目标日期目录缺失时，代码选择全部目录排序后的最后一个，未限定 `candidate_date <= as_of_date`，也未核验源 result/audit 内部日期。
- 负向结果：请求 2026-01-01 时可以选中 2026-01-02 基线。
- 影响：历史发布评估会读取未来状态，构成 point-in-time 泄漏。
- 修复：只允许选择不晚于请求日期的最近目录，并强制 result、audit、目录和请求日期一致。

### ATLAS-ML-001：20 日标签 walk-forward 没有 purge/embargo

- 位置：`ml/walk_forward_dataset.py:37-46,190-224,265-272`；`labels/label_store.py:175-193`
- 证据：切分相邻，leakage check 只验证 train_end < validation_start < test_start，不理解标签 horizon。
- 负向结果：包含未来 20 日收益的训练标签与验证/测试价格重叠，检查仍 `passed=true`。
- 影响：模型评估存在 label-overlap 泄漏，结果系统性偏好。
- 修复：按最大 horizon purge，增加 embargo；horizon 写入契约，加入边界负向测试。

### ATLAS-PKG-001：trading-core wheel 安装后核心配置与 Parquet 能力缺失

- 位置：`config_loader.py:10-24`；`pyproject.toml:11-27`
- 隔离安装结果：`config_exists=false`，`load_config("settings.yaml")` 抛 `FileNotFoundError`；也没有 Parquet engine。
- 原因：配置按源码仓根路径寻找，未打入 package-data。
- 影响：console script 能显示版本，但标准安装后的业务命令不可用。
- 修复：使用 `importlib.resources`、声明 package-data/Parquet 依赖，CI 执行“build wheel → 隔离安装 → 核心 smoke”。

### ATLAS-FIN-003：trading-core 涨跌停规则统一写死 ±9.9%

- 位置：`broker/market_constraints.py:49-58`
- 负向结果：科创板 +10% 和北交所 +10% 被错误视为涨停；风险警示与上市阶段完全没有进入规则选择。
- 影响：20%/30% 市场合法成交被拒，其他分市场规则又可能错误放行。
- 修复：按交易所、板块、风险警示状态、上市阶段、生效日版本化，并按价格档位舍入。
- 规则校正：不能再写“所有 ST 都是 5%”。现行基准是沪深主板 10%、创业板/科创板 20%、北交所 30%；其中沪市主板风险警示股自 2026-07-06 起已调整到 10%。依据：[上交所公告](https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20260424_10816474.shtml)、[北交所规则](https://www.bse.cn/jygl_list/200028217.html)、[深交所现行交易规则（2026 年修订）](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)。

### ATLAS-FIN-004：回测初始资金与胜率口径错误

- 位置：`historical_backtester.py:201-216,242-245`；`broker/cost_model.py:35-47`
- 证据：报告/基准固定使用 100000，而账户实际从配置读取；胜率按每笔 `net_amount > 0`，并非平仓回合净盈亏。
- 负向结果：明确亏损回合仍同时输出 `Cumulative return: -0.00112` 和 `Win rate: 1.0`。
- 影响：配置资金改变时收益率错误；正常 BUY/SELL 的 `net_amount` 通常都大于零，所以当前胜率趋近把全部成交都算赢，常接近 100%。
- 修复：从首期账户读取初始资本；按完整 closed lot/round-trip 的已实现净盈亏计算，包含全部费用。

## 7. P2 / Medium 发现

### ATLAS-DATA-003：中国行情缺失和非有限数值可穿过 sourceHealth/payload

- 位置：`sync_briefing_site.py:1201-1267,2025-2031`；`china_market.py:178-225,689-707`；`market_snapshot.py:50-56`
- 全部 35 条 price=None 时，sourceHealth 可输出 score=100、errorCount=0、limitations=[]，因为缺价项目被 continue。
- NaN/Inf 会被当 float 和 status=ok，最终可生成 `change='+nan%'`，Python JSON 默认还允许 NaN。
- 修复：缺价必须进入 denominator/error；所有数值用 `math.isfinite`；JSON 写入统一 `allow_nan=false`；payload 校验数值语义。

### ATLAS-DATA-004：腾讯成交量/成交额字段按不同 instrument schema 解释不一致

- 位置：`china_market.py:304-328`
- 证据：代码把 field 36/37 原值直接 float。实测 sh600000 的 raw field 36=796240（手）、field 37=70780（万元），而 Yahoo 同期 volume=79,624,033 股；hk00700 个股 field 37=16928705332.905（绝对 HKD）。HK 指数布局不保证与 HK 个股相同。
- 实测：当前 A 股成交量约低 100 倍、成交额约低 10,000 倍，跨 instrument 不可比。
- 修复：按 `market + instrument_type + provider schema version` 明确单位；A 股 field 36 由手乘 100 转成股、field 37 由万元乘 10,000 转成人民币元；所有结果保留 raw field、unit 和 scale，不能只按 A/HK 两类硬分。

### ATLAS-DATA-005：配置声明支持北交所，但三套直接报价路由均不支持 `.BJ`

- 位置：`china_market.py:96-147`
- 证据：输入层能规范化 `.BJ`，但 Tencent、Eastmoney、Yahoo 三个 symbol mapper 都没有 BJ 分支；配置能力声明与 provider 实现不一致。
- 影响：北交所标的无法通过这三套直接报价获取；Tencent 返回 `None` 后会在批处理中静默遗漏，Eastmoney/Yahoo 明确返回 `unsupported_symbol`，导致 fallback 覆盖不完整且能力声明失真。
- 修复：为 Tencent、Eastmoney、Yahoo 明确实现 `.BJ` 映射；确实不能支持的 provider 必须声明 capability 并 fail closed；为三 provider 增加 BJ 正向映射与不支持时拒绝的反向测试。

### ATLAS-FIN-006：paper trading 的市场约束仍有多个 fail-open/过度阻断边界

- 位置：`paper_trading.py:1628-1671,1877-1895,2017-2028,2179-2209`；配置 `paper_trading.json:115-129`
- BSE 仍回落 10%，无昨收直接跳过限价守卫，显式 `price_limit_pct` 可任意覆盖。
- `paper_trading.json:126` 仍固定 ST=5%，且 `paper_trading.py:1635-1650` 先按自由文本名称匹配 ST；实测 `600xxx`、`000xxx`、`300xxx`、`688xxx` 的 ST 均返回 5%，不符合当前沪深主板 10%及创业板/科创板 20%规则。
- 创业板判断只识别 `300` 前缀；实测普通 `301001.SZ` 返回 10%，但其应适用创业板 20%。
- 实际持仓 ETF 也被错分：`159915.SZ`、`588000.SH` 均适用 20% 限制，当前函数却返回默认 10%；两者已进入 watchlist、组合和虚拟成交。
- 新股上市阶段完全缺失：当前没有上市日期/交易日序号；首次公开发行上市后的前五个交易日不设涨跌幅限制，代码仍会套用 10%/20%。
- T+1 按整仓 `last_buy_date`，当日加仓会阻止卖出前日已结算份额。
- 显式 fee=0 可覆盖最低佣金/法定费用。
- 修复：共享版本化规则引擎，按证券类型、上市阶段和交易所公布的 20% ETF 名单判断，不能只靠股票代码前缀；增加前五个交易日无固定涨跌幅的负向测试；按 lot 维护 settled/unsettled；费用 override 仅允许受控导入且不得低于法定税费。
- 权威依据：上交所 2026 交易规则明确特定 ETF 为 20%、科创板 IPO 前五个交易日不设限制；深交所规则明确创业板及相关基金 20%、IPO 前五个交易日不设限制并由交易所公布 20% 基金名单。[上交所现行规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20260424_10817739.shtml)、[深交所交易规则](https://docs.static.szse.cn/www/lawrules/rule/trade/W020230217564423808793.pdf)

### ATLAS-FIN-007：证券税费按粗粒度市场/卖出方向计算，已经污染虚拟收益

- 位置：`paper_trading.py:1567-1601,1877-1895,2199-2209`；现存账本 `paper_trades_china.jsonl:85,91-92`
- 证据：默认费用计算路径对 A_SHARE/HK 的 SELL 一律计印花税；显式 fee/tax override 又可完全替代计算结果。账本已对创业板 ETF `159915.SZ` 错收 4.32875 元印花税。
- 规则错误：中国证券交易印花税对象是股票/CDR，不包括 ETF；香港上市 ETP 买卖免印花税，而普通港股买卖双方均需计税，当前默认只计卖方。香港路径还遗漏买卖双方 SFC transaction levy 0.0027%、AFRC levy 0.00015% 和 HKEX trading fee 0.00565%；现存 ETF BUY 已少计这些费用。
- 影响：费用、现金、已实现收益、站点组合和后续归因均被系统性扭曲。
- 修复：按司法辖区、证券类型、买卖方向和生效日建模；香港三项 levy/fee 各自按交易所规则舍入到最近一分，普通股票印花税按每方 0.1% 向上取整至港元，ETP 免印花税。通过带迁移 ID 的冲销/更正事件修复既有少收和多收费用，不得删除或重写原账本事件。
- 权威依据：《中华人民共和国印花税法》第三条把证券交易限定为股票和以股票为基础的存托凭证，且自 2022-07-01 施行；HKEX 明确 ETP 免印花税但仍收 trading fee、transaction levy 和 settlement fee，并公布三项 levy/fee 的费率与逐项舍入；香港税务局确认普通香港股票自 2023-11-17 起买卖双方各 0.1%。[国家税务总局法律原文](https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193058/content.html)、[HKEX ETP 费用说明](https://www.hkex.com.hk/Products/Securities/Exchange-Traded-Products/Investors?sc_lang=en)、[HKEX 交易费用与舍入](https://www.hkex.com.hk/Services/Rules-and-Forms-and-Fees/Fees/Securities-%28Hong-Kong%29/Trading/Transaction?sc_lang=en)、[香港税务局 2023 生效公告](https://www.ird.gov.hk/chs/ppr/archives/23111501.htm)

### ATLAS-FIN-005：放宽风险配置后，现金风控不包含成交总费用

- 位置：`risk/risk_engine.py:53-67`、`broker/virtual_broker.py:75-84`、`accounting/account.py:46-49`；默认 `config/risk_rules.yaml:6`
- 复现边界：使用受支持但放宽的自定义规则，尤其 `min_cash_weight=0`。项目默认值 0.10 通常会更早阻断，因此这不是默认路径必现。
- 负向结果：现金 1000、名义额 1000 时风控通过；含滑点佣金支出 1006，随后抛 `buy_trade_exceeds_cash`。
- 影响：在受支持的边界配置中，订单不是结构化拒单，而会使回测/虚拟批次异常退出。
- 修复：风险与撮合共享全成本估算；记账异常转为结构化 reject；测试默认和放宽配置两条路径。

### ATLAS-CLI-001：本地发布命令的日期参数可成为路径片段

- 位置：`cli.py:510-517,1595-1607,1974-2000`；`generic.py:1031-1041`
- 证据：规范日期 validator 只用于少数命令；多项发布命令直接把输入日期拼进输出目录。
- 负向结果：本地调用者提供非 ISO 日期时，输出路径可解析到项目根之外。
- 严重度边界：未发现远程或低权限输入面，因此定为 P2，而不是远程路径穿越。
- 修复：所有日期参数统一 argparse type `_canonical_iso_date`；写入前 resolve 并验证仍位于项目根。

### ATLAS-NET-001：Queue、HTTP 与 RSS 时间边界仍存在可靠性/安全缺口

- `multiprocessing.Queue.empty()`：`market_snapshot.py:112-125`、`china_market.py:616-628`，官方语义不可靠，可误判 worker 无结果。
- 无大小上限：`china_market.py:241,355,399`、`market_snapshot.py:134`、`resolution_evidence.py:124`。
- RSS DNS rebinding TOCTOU：`rss_collect.py:86-117,130-147` 预解析后 opener 重新解析，未核实际 peer IP。
- 未来时间：`rss_collect.py:331-367` 把负 age clamp 为 0；2099 年条目会在系统时间到达该时间之前持续被判 fresh。
- 修复：用 blocking `get(timeout)`/sentinel；流式限额和总 deadline；复用生产 verifier 的 DNS pin；拒绝超过允许 clock skew 的未来时间。

### ATLAS-JSON-001：prediction strict JSON 不拒绝重复键

- 位置：`briefing_store.py:48-52`、`prediction_ledger_repair.py:123-147`
- 负向结果：`prediction_id` 重复时静默保留后一个值。
- 风险：人读、签名/哈希和程序解析可能对同一证据有不同理解。
- 修复：复用 `disaster_recovery.py:188-204` 已有 object_pairs_hook 重复键拒绝器。

### ATLAS-EVID-001：prediction 人工证据只验证文件存在/哈希，不验证语义与授权身份

- 位置：`prediction_ledger_repair.py:246-290`
- 证据：允许任意存在文件，验证 whole-file SHA 和行号范围，但不限制可信报告根，也不判断引用行是否支持所选 variant；authorization reason 只是字符串。
- 边界：事务锁、prepare/recovery 逻辑本身较扎实，不能夸大为任意账本篡改。
- 修复：可信 evidence root、结构化 claim schema、签名 actor identity、引用文本摘要和 claim-to-line 语义校验。

### ATLAS-SEC-002：HMAC/AES 密钥持久化在 CurrentUser 环境

- 位置：`atlas.py:418-438,489-494`；`disaster_recovery.py:405-438`；`scripts/run_due_alerts.ps1:25-35`
- 当前机器：相关 ATLAS 配置项存在于 Windows CurrentUser Environment；审计未读取或输出密钥值。
- 影响：任何同用户进程都可读密钥并伪造本地认证 metadata；trust key 还只要求非空，没有强度下限。
- 修复：Windows Credential Manager/DPAPI、系统级 KMS 或短期 CI secret 注入；key id/rotation/最小熵检查。

### ATLAS-REL-004：pytest evidence 有源码哈希，但缺受信执行身份

- 位置：`equity_release_chain/generic.py:496-510`
- 已修部分：producer 现在确实执行完整 pytest；缺证据会阻断；summary/source tree hash 会校验。
- 剩余问题：`generic.py:496-498` 只检查 command 列表尾部是 `-m pytest`，不确认首元素为实际 Python；commit 为空也不阻断。隔离 evidence 使用非 Python 标识且无 commit 仍得到 `status=verified`；此外没有 CI run identity、依赖锁指纹、JUnit hash 或工作流签名。
- 修复：强制 commit、CI OIDC/workflow identity、依赖/SBOM 指纹、JUnit artifact hash 和签名证明。

### ATLAS-SAFE-001：安全措辞扫描只检查关键词第一次出现

- 位置：`equity_owner_daily_pack/daily_pack_boundary.py:69-77`、`equity_owner_daily_pack_history/daily_pack_history_boundary.py:65-73`、`equity_build_output_ops_refresh/build_output_ops_boundary.py:67-75`
- 负向结果：同一关键词第一次在否定语境、后续在肯定语境出现时，扫描仍 `hits=[]`。
- 影响：研究输出可能漏过后续真实的交易建议/保证性措辞。
- 修复：遍历所有匹配位置并分别判断上下文；加入“一次否定、一次肯定”的负向 fixture。

### ATLAS-UI-001：payload/页面仍保留两个休眠数据契约缺口

- `metrics.freshness`：`validate_payload` 不检查，页面 `src/app/page.tsx:349` 直接渲染；删除该字段仍 0 validation errors。
- operational gate：`src/app/page.tsx:44` 仍用 `operationalGatePassed ?? overallPassed` 回退到已废弃混淆字段。
- 边界：有效 pipeline payload 现在已强制 explicit gate，所以第二项是休眠债务；但前端独立 CI 不调用 Python validator，会放大风险。
- 修复：TS/Python 共享 schema，移除 fallback；前端 CI 必须执行同一契约验证。

### ATLAS-OPS-002：cycle stale metadata 会因 PID 重用拒绝启动

- 位置：`atlas.py:3672-3694`
- 证据：OS advisory guard 已成功获得后，仍会按旧 metadata 中相同 PID 的任意活进程判占用。
- 影响：PID 被无关进程复用时可永久拒绝 cycle，属于可用性而非并发绕过。
- 修复：guard 成功即以 guard 为 authority；metadata 只用于诊断，或同时校验进程启动时间/token。

### ATLAS-TEST-001：关键模块低覆盖且大量测试只断言自声明 true

- 根 CI 阈值仅 60%，见 `.github/workflows/quality.yml:48`；`china_market.py` 15%、`resolution_evidence.py` 38%。
- trading-core 有 269 处 `overall_passed is True`；`tests/a_share_release_chain_test_utils.py:61-76` 的单元测试辅助函数把模拟 `1 passed` 当 release 通过条件。生产 producer 现在会真实运行完整 pytest，但这些 release 单元测试没有验证真实 subprocess 或最小收集数量。
- 影响：正向字段一致性测试很多，但真正的语义反例、证据缺失、过期和并发撕裂测试少。
- 修复：按控制风险设模块阈值；增加 mutation、rollback、future-date、cross-market、clean-install 和真实 subprocess tests。

### ATLAS-ARCH-001：版本复制和超大 CLI 继续制造修复漂移

- trading-core：`_audit.py` 68、`_report.py` 56、`_manifest.py` 37；`cli.py` 8,399 行、490 个子命令。
- 已见漂移：相同安全措辞窗口 40/45 字、paper 核心修复未传到 site/evolution、不同市场规则各写一份。
- 版本身份：v4 标签后已有 10 个提交和 66 个质量相关文件变化，但 `VERSION/pyproject` 仍为 4.0.0。
- 修复：抽取共享发布、市场日历、成本/归因、manifest 和边界扫描库；CLI 使用命令注册表；合并后发布 maintenance 版本并打不可变 tag。

## 8. P3 / Low 发现

### ATLAS-BUILD-001：中断的前端构建会遗留 `.vinext` snapshot 并污染 lint

- 位置：`src/build/preserve-live-assets.mjs:55-85,88-115`；lint 只忽略 dist/.next，`src/package.json:17`。
- 当前：6 个 `live-static-snapshot-*` 遗留目录，lint 产生 6 条 generated manifest warning。
- 判断：正常 finally 会删当前 snapshot，但进程被强杀时没有启动清理；影响限于生成物残留与诊断噪音。
- 修复：启动时清除超过 TTL 的 snapshot；eslint 忽略 `.vinext` 生成物；保留当前 generation 的安全恢复语义。

## 9. 已确认修复，不能继续沿用旧结论

| 旧问题 | 本次核验 |
|---|---|
| 删除/清空 canonical ledger 可重建绕过 | 已 fail-closed；但整体旧快照回放仍未解决 |
| history 尾部截断/前插 legacy/内容改写 | 当前外部锚能发现；但旧 history+anchor 整体回放仍未解决 |
| `cycle.lock` 空/半写竞态 | 已改 OS advisory guard + 原子 metadata，原 H1 已修 |
| 子进程完全无 timeout | 已有 30 分钟 timeout；剩余是没杀进程树 |
| 站点估值日后交易只进持仓不进现金 | 已修，回归验证 cash=95000、allocation 合计 100% |
| paper 核心没有 base cost | 已修；未传导到 site/evolution |
| 多账户写事务/恢复函数是死代码 | 核心写入与官方读取已修；旁路消费者仍裸读 |
| 回放用信号日收盘成交 | 已改成前一交易日信号在下一交易日开盘成交 |
| price-only/零成交回放仍通过 | 已 fail-closed |
| escalation_due_at/backoff 无消费者 | 已实现；剩余是外部送达回执自证 |
| 前端无 CI、无 npm Dependabot、`.npmrc` 未提交 | 已补齐 |
| payload 的 markets/risks/portfolio/evolution.integrity 大量缺校验 | 大体已补；剩 `metrics.freshness` 和站点独立 CI 缺口 |
| RSS 无响应大小上限 | RSS 已有 5 MiB；其他行情/证据 HTTP 仍无上限 |
| 灾备单包加密/恢复校验不可信 | 单包实现扎实；历史链头回滚仍未解决 |
| full pytest 完全硬编码、不运行测试 | generic producer 已真实运行；v16 旧字段和其他语义常量仍不真实 |
| `external_research` 真实券商仓库进入主业务 | 当前不在 Git 跟踪且无 first-party import，旧结论不再成立 |
| 腾讯港股价格必然错 10 倍 | 本次实时价格解析正确，不能继续列为确认缺陷；真正问题是成交量/成交额的跨品种单位与缩放语义 |
| Eastmoney f152 必然导致价格错 10 倍 | 本次无法复现，不能作为确认缺陷 |
| 所有 ST 都应 5% | 已过时，必须按交易所/板块/生效日版本化 |

## 10. 为什么每次深查还能找到一批问题

这不是简单的“测试数量不够”，而是四个系统性原因：

1. **同一金融语义被复制实现。** paper core、site、evolution、trading-core 各自重建成本、日历和规则；修一份不会自动传导。
2. **证据生产者和验证者处在同一可编辑信任域。** 本地 JSON、marker、anchor 即使有哈希，也缺少外部不可回退身份。
3. **测试偏向正向自洽。** 大量断言某个 `overall_passed` 为 true，却没有先证明该字段来自真实执行或真实数据。
4. **跨文件/跨仓库状态缺少统一事务。** 账本、state、candidate、manifest、部署状态和公开页面各自提交，旁路读取很容易看到撕裂或旧状态。

因此后续优化不能继续逐个补布尔字段，必须收敛权威实现和信任边界。

## 11. 修复优先级与验收标准

### P0：暂停完整性/可信发布声明，先封闭控制绕过

1. canonical event 完整哈希 + 显式迁移协议。
2. ledger/history/backup head 的远程不可回退 witness。
3. 备份门真实验签/解密验证；deployment evidence 使用受信 attestation。
4. 前端 CI 强制 frozen manifest、payload hash 和生产身份验证。
5. 把 trading calendar 扩到至少未来 90 日并设置到期硬门。

验收：对每项运行旧签名回放、canonical 字段突变、纯文本备份、离线 deploy JSON、过期日历五类负向测试，必须全部 fail closed。

### P1：恢复金融正确性和干净环境可复现

1. 公开层只消费核心锁内 snapshot，不复制成本/归因算法。
2. 统一交易所日历和版本化市场规则。
3. 修正 FX lots、prediction attribution、Yahoo previous session、回测胜率/资本和全成本现金检查。
4. 声明 pyarrow/package data，PR 跑完整测试和 wheel clean-install。
5. 实现 purge/embargo 和真实 v16/发布语义检查。
6. 修复进程树 timeout、ACL/key store、发布 retry 事务。

验收：Windows/Linux clean clone 均能 wheel 安装、运行核心命令、完整测试全绿；相同输入在 paper/site/evolution 得到一致成本和归因。

### P2：减少下一轮漂移

1. 抽取 `atlas_finance_core` 式共享库：calendar、market rules、cost basis、lot attribution、finite-number schema。
2. CLI 注册表化，移除冻结版本复制模块的活动逻辑。
3. source health、网络读取、JSON parser、证据 schema 使用统一组件。
4. 用 mutation/property-based/concurrency tests 替代更多自声明 true 测试。

## 12. 可上线的最低判定

只有同时满足以下证据，才能证明“本报告列出的已知问题已有关闭证据，并且修改已在目标环境生效”；有限审计不能给出“未来永不再出现问题”的永久保证：

- 三个修复分支都有 PR，默认分支包含修复 commit。
- 三仓默认分支 CI 全绿；trading-core 是干净环境完整回归，不是精选测试。
- 最新日历覆盖至少未来 90 日。
- 最新 briefing candidate frozen=true，payload SHA-256 匹配，snapshot revision > 0。
- 真实线上 URL 的生产验证通过，且验证器绑定可见内容/部署产物而非自报 marker。
- canonical mutation、signed rollback、fake backup、fake deployment、torn snapshot 负向测试全部失败关闭。
- 公开页面日期、行情 price_date、组合 base cost 与核心账本一致。

当前这些条件没有全部满足，所以本报告的最终回答仍是：**项目已有不少扎实修复，但尚未达到可自动生效、可信发布，以及对本报告已知问题逐项关闭的状态。**

## 13. 三方复审记录

v1：三方均为 `CHANGES_REQUIRED`。已订正的要点包括：

- 明确 GitHub 419 failures 属于默认分支 `70308a9`，不冒充当前 HEAD 实跑结果。
- 补主流程日历调用、未来基线泄漏、global ST/BJ 路由、安全措辞扫描等证据。
- 将本地 CLI、放宽风险配置下的现金边界降到 P2，将 `.vinext` 残留降到 P3。
- 更换现行深交所 2026 规则引用，收窄腾讯 amount、RSS future time 和告警 receipt 表述。
- 区分生产 pytest producer 与模拟 release 单元测试，取消“永不再出现”的不可证明保证。

v2：三方均为 `CHANGES_REQUIRED`。已订正：

- 告警 receipt 先形成 delivered，actor 只在 delivered 后推进 acknowledged，避免混淆两个状态迁移。
- 交易规则补齐深市主板 10%，并将 `000xxx` 纳入错误复现范围。
- 将北交所 provider 路由拆成独立 `ATLAS-DATA-005`，补齐能力声明、fail-closed 与三 provider 正反向测试闭环。

v3：审核 C 为 `PASS`；审核 A 指出 `.BJ` 三个 mapper 的实际失败方式被写成“错误编码/错误市场”，与代码不符；审核 B 在该结论返回前尚未形成最终一致意见。已按实际分支订正为 Tencent 静默遗漏、Eastmoney/Yahoo 明确不支持。

v4：审核 C 为 `PASS`；审核 B 发现工作流触发条件、跨市场业务日、腾讯成交量单位和 `301xxx` 创业板四处遗漏。已逐项补齐；审核 A 在形成 v5 前未再给出新的独立问题。

v5：审核 C 指出“已确认修复”表仍把腾讯缺陷缩写为 amount，与正文已经确认的 volume/amount 双重单位问题不一致；已统一表述。审核 A/B 在形成 v6 前未给出新的独立问题。

v6：审核 A/C 为 `PASS`；审核 B 新发现非交易日成交、20% ETF 分类和已经进入账本的印花税错误。已分别形成 `ATLAS-CAL-003`、补充 `ATLAS-FIN-006`，并新增 `ATLAS-FIN-007`。

v7：三方均提出订正。已把 `ATLAS-CAL-003` 扩展到休市/收市后时间因果；补充新股前五日无固定涨跌幅；把税费表述限定为默认路径，并补齐香港三项 levy/fee、舍入、历史迁移范围和中港官方一手依据。

v8 状态：待第八轮三方复审。

计划让三个子智能体都读取整份报告并全局核验：

- 审核 A：偏重完整性、安全边界与严重度，但检查整份报告。
- 审核 B：偏重金融正确性、数据语义与交易规则，但检查整份报告。
- 审核 C：偏重测试可复现、发布/部署与证据链，但检查整份报告。

任一审核给出 `CHANGES_REQUIRED`，即形成下一版并重新进行三方审核；只有三方都明确 `PASS` 才定稿。

## 14. 工作区影响

所有攻击性复现都在内存或临时目录完成；线上检查是只读网络访问。形成审计快照时三个源码工作区的 tracked 状态均干净，本报告是当时唯一新增文件。报告进入复审后，用户另行授权的深度修复已经开始，因此当前工作区会包含后续修复改动；这些改动不倒改本报告的审计快照结论。本报告不包含密钥值或凭据。
