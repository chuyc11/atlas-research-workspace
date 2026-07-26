# ATLAS 深度优化与外部方案取舍

## 结论

ATLAS 当前最需要的不是继续增加“通过”字段，而是把四类事实绑定到独立证据：运行事实、金融语义、构建来源、线上可见内容。外部项目只在能缩小自研可信基、且不会取代官方金融数据权威时引入。

## 建议采用

1. **GitHub Artifact Attestations：构建/发布来源证明。** GitHub 官方 action 能把产物摘要绑定到 Actions OIDC 身份和工作流，消费者可用 `gh attestation verify` 验证。适合 wheel、站点构建包、SBOM 和 frozen publication bundle。[官方使用文档](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)、[actions/attest](https://github.com/actions/attest)
2. **Sigstore Rekor：防止“旧签名整体回放”。** GitHub 自有 Sigstore 实例没有透明日志，因此 Artifact Attestations 本身不能替代单调外部见证。Rekor 提供可查询的防篡改透明日志和 inclusion proof，适合记录 ledger/history/backup/publication 最新链头的小型签名声明。[Rekor](https://github.com/sigstore/rekor)
3. **in-toto Statement：统一证据结构。** 用 subject digest 绑定 artifact，用 predicate 记录日期、commit、测试计数、前驱链头和部署身份；它适合作为内部 JSON 证据的目标模型，不必一次性重写全部实现。[in-toto Attestation Framework](https://github.com/in-toto/attestation/blob/main/spec/README.md)
4. **Hypothesis：关键不变量的属性测试。** 优先用于 ledger append-only、有限数值、市场规则、日期边界、JSON 重复键和状态机迁移，替代只覆盖单个正例的测试。[Hypothesis](https://github.com/HypothesisWorks/hypothesis)
5. **pytest-xdist：缩短 trading-core 完整回归。** 先对无共享文件写冲突的测试启用 `-n auto`，按组隔离会写固定目录的测试；不能在未消除共享状态前直接全仓并发。[pytest-xdist](https://github.com/pytest-dev/pytest-xdist)
6. **exchange_calendars：交叉验证而非权威源。** 它覆盖 XSHG、XHKG、XNYS，可用于发现工作日算法错误和官方日历漏项；其日历由社区维护，A 股正式运行仍必须以交易所公布日程为准。[exchange_calendars](https://github.com/gerrymanoim/exchange_calendars)

## 暂不直接采用

- **自建 Rekor/数据库替换现有文件链。** 当前团队规模下运维成本过高；先把小型链头声明写入公共 Rekor，并保留本地可恢复证据。
- **全仓 mutation testing。** trading-core 完整套件耗时长且有大量文件型集成测试；先只对 ledger continuity、market rules、publication identity 三个小模块做定向 mutation。
- **让社区交易日历直接决定正式交易日。** 只能作为 cross-check，不能覆盖官方公告。

## 分阶段落地

### 第一阶段：本轮

- canonical event 采用完整语义哈希连续性检查。
- 日历扩至未来 90 日以上并设置 30 日运行硬门。
- PR 执行完整 trading-core 回归；声明 pyarrow 和 wheel package-data。
- 固化 `atlas-integrity-maintainer` skill 与只读 preflight。

### 第二阶段：合并前

- CI 生成 wheel、site bundle、SBOM，并用 `actions/attest` 证明来源。
- 添加 clean-wheel-install job 和 publication manifest 独立门。
- 引入 Hypothesis 的 ledger/market/date 定向测试；验证 xdist 可并发测试集合。

### 第三阶段：可信上线

- 把 ledger、history、backup、publication 的最新链头提交到 Rekor；本地验证必须检查 inclusion proof 和单调 predecessor。
- 部署器只接受 frozen bundle 的有效 attestation；线上验证从可见内容/部署产物重新计算身份。
- provider receipt 通过真实 connector 查询或签名验证，不再接受任意文本。

## 验收

- 旧签名、旧锚和旧目录整体回放全部 fail closed。
- 干净 wheel 安装可加载全部配置并读写 Parquet。
- PR 完整测试和产物 attestation 绑定同一 commit。
- 发布 manifest 的 payload/snapshot hash 与实际产物一致。
- 线上验证通过后才能写 deployed 状态；失败时保留 staged candidate 供幂等重试。
