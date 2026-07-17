# ATLAS 工程质量审计

审计日期：2026-07-17

## 结论

本轮完成的是控制面、研究账本、虚拟交易、发布链和灾备的系统性加固，而不是只修单个测试。
根仓与两个子仓现在具备可获取远端和不可变版本关系；运行门禁、发布候选与研究晋升继续保持
独立，任何缺失证据都会 fail closed。

代码已达到提交与推送条件，但这不等于研究策略已获准晋升，也不等于生产站点已部署。生产发布仍需：

1. 由密钥管理器提供 `ATLAS_BACKUP_ENCRYPTION_KEY`，生成日期对齐的加密异盘备份；
2. 显式归档旧 schema 2 `latest.json`，创建首个经过 HMAC 认证的 schema 4 genesis 快照；
3. 为 GitHub Actions 配置只读 `ATLAS_SUBMODULE_TOKEN`，并在同一干净 commit 上完成两次相同指纹的全量 cycle；
4. 冻结 publication snapshot 后部署，并对真实 HTTPS 响应执行身份标记、响应头、同源重定向和运行时隔离验证；
5. 对关键告警取得每个配置目的地的实际送达与确认回执。

## 本轮修复

编号延续上一轮审计的 Q-001–Q-018；下表记录本轮新增发现及处置。

| ID | 缺陷 | 修复结果 |
|---|---|---|
| Q-019 | 原始虚拟账本可在规范化前夹带 broker/live 字段，错误安全声明会被覆盖 | 在 canonicalize 前递归审计原始成交、订单、回放和账户快照；禁止字段、矛盾安全声明及非有限数值直接阻断 |
| Q-020 | `cycle --skip-sync` 仍可能形成运行绿色，发布候选未强制同步和规范账本落盘 | 跳过同步明确阻断；发布候选必须同步成功、全部必需阶段通过、规范账本写入完成、全量测试和幂等证据齐全 |
| Q-021 | 工作区来源锁只看远端名称，时间戳导致内容哈希不稳定 | 三仓用只读 `git ls-remote` 验证当前完整 SHA 被远端 ref 明确发布；来源锁内容哈希排除 `generated_at`，且不记录远端 URL/错误正文 |
| Q-022 | paper trading 接受 NaN/Infinity、负费用或税费以及布尔数值 | 输入、配置、费用、价格、数量和外汇转换统一有限数校验；JSON 读写拒绝非标准数值常量 |
| Q-023 | 订单载荷日期可绕过命令日期，`price_date` 未形成估值新鲜度契约 | 订单日期必须与运行日期相同；成交和估值保留价格日期；未来、缺失、冲突或过期价格按配置阻断/降级且不得标记健康 |
| Q-024 | 外币持仓按当前汇率回算历史成本，价格和汇率损益混淆 | 持仓保存原生及本币成本基础与入场汇率；卖出释放对应本币成本，并分解价格损益与外汇损益 |
| Q-025 | 多账户交易仅逐账户事务，崩溃可留下不可恢复的半批日志 | 引入 `preparing → prepared → committed` durable coordinator、全账户 journal/commit marker、generation 与 SHA 校验；读取方先锁定并恢复，任何崩溃点都不会暴露半批状态 |
| Q-026 | 预测 JSONL 无并发锁，载荷日期可回填，重复 original 在消费者间 first/last 语义不一 | 加入跨平台文件锁和原子重写；记录日期必须等于运行日期；original 统一 first-wins；相同 review key 的冲突记录直接 fail closed |
| Q-027 | 正文中的任意链接可冒充“一手/当地/外部核验”证据角色，同源镜像可虚增独立来源 | 每个角色必须显式绑定互不复用的可审计链接；一手角色必须绑定机构来源；独立性按来源家族别名计算 |
| Q-028 | 站点快照只要求 operational 状态，部署确认未绑定冻结 revision | 冻结前必须具备 release candidate、同步、规范写入、全量测试、幂等、可复现三仓和深度严格自愈；部署确认重新校验 revision、快照哈希、载荷与当前输入 |
| Q-029 | 生产验证允许重定向和 DNS 目标漂移到非预期网络 | 每一跳先解析且只接受全局公网地址，再固定已验证 IP 建连，同时保留 Host/TLS SNI；要求 HTTPS、443、原同源并核对实际 peer IP |
| Q-030 | 告警生成 `pending_handoff` 文件即可被能力验收，确认可跳过送达 | 每个配置目的地必须提供不可变回执；严重告警还必须确认；独立 channel-health 证据不再改写业务告警状态，跨进程写入用 OS 文件锁防丢失 |
| Q-031 | 灾备为未加密 ZIP，可能把凭据与研究状态一起复制到异盘 | 升级严格 schema 4：AES-256-GCM、用途分离 HMAC、精确 ZIP 成员集、逐文件哈希、`bundle verify → mirror clone → fsck --full --strict`；明文仅进入异盘私有 staging 并保证清理 |
| Q-032 | 多个命令用主机本地时区生成业务日期 | 新增统一报告时钟，按 `settings.json` 的 `Asia/Shanghai` 生成业务日期；UTC 仅用于审计时间戳 |
| Q-033 | 根仓忽略两个独立仓，干净克隆无法重建组合工作区 | `src` 与 `work/trading-core` 登记为 Git 子模块并固定已推送 commit；根 CI 新增递归克隆后的跨仓门禁 |
| Q-034 | npm 锁文件可能被本机镜像和 npm 主版本漂移污染 | 固定 Node `>=22.15.0`、npm `10.9.2` 和官方 registry；安装前审计 HTTPS、主机、凭据及 SHA-512 完整性 |
| Q-035 | 历史组合页面可能用成交价伪装报告日市价 | 历史重建优先使用报告日之前的估值价格快照和记录权益，禁止未来持仓与未来价格倒灌 |
| Q-036 | 禁止键可用 camelCase、连字符或下划线变体绕过；坏字符串数值会被静默当成零 | 安全键先做统一规范化再递归审计；费用、税额、名义金额使用严格有限数解析，负值与错误字符串全部阻断 |
| Q-037 | cycle 幂等只绑定输入事件，代码或子仓 commit 改变后仍可能复用旧结论 | 幂等身份加入稳定 workspace-lock 哈希和三仓 commit；任一仓版本变化都必须重新完成重复运行证据 |
| Q-038 | URL 大小写、IDNA、默认端口与镜像子域可虚增独立研究来源 | 用解析后的 hostname 规范化 IDNA/大小写和默认端口，拒绝凭据及非默认端口，并按可注册域/来源家族计算独立性 |
| Q-039 | 行情、订单与持仓币种可不一致，日期倒退和 legacy replay 可污染账户 | instrument currency 全链一致；订单/估值日期单调；重复订单、重复 fingerprint、缺 fingerprint 回放和非法 turnover 均 fail closed |
| Q-040 | 未冻结候选会直接改写可部署站点，生产验证把本地哈希当线上证据 | 未冻结候选仅写非部署 staging；冻结 manifest 绑定 payload、snapshot、candidate、build/deployment 与三仓版本；线上验收只接受页面实际观测标记 |
| Q-041 | CI 使用私有子模块但未声明凭据契约，干净克隆依赖被忽略运行产物 | 缺少 `ATLAS_SUBMODULE_TOKEN` 时组合任务明确失败；干净克隆先验证 gitlink/HTTPS 子模块结构，再执行跨仓门禁 |
| Q-042 | Python 质量工具仅固定直接版本，传递依赖仍随时间漂移 | 新增由 Python 3.12 生成的 `requirements-dev.lock`，包含完整传递依赖 SHA-256；CI 安装强制 `--require-hashes` |

## 验证证据

- 根仓完整测试：227 passed、9 subtests passed；覆盖率 70.19%，高于 CI 的 60% 门槛。
- 虚拟交易专项：37 passed、6 subtests passed；覆盖批次崩溃恢复、锁顺序、币种一致性、日期单调和回放拒绝。
- 发布、冻结快照、研究来源与生产验证专项：83 passed。
- 灾备专项：16 passed；灾备、告警与改进验收联测：28 passed；错误密钥、篡改、额外 ZIP 成员和伪 Git bundle 均失败。
- 跨仓总门禁：根测试 26、global-briefing unittest 201、trading-core 集成 33、站点 Node 测试 12，全部通过。
- 站点质量：lint、TypeScript、生产构建、依赖源策略通过；`npm audit --omit=dev` 为 0 漏洞。
- 根安全门禁：Ruff、compileall、Bandit、detect-secrets、diff-check 通过；直接清单与哈希锁经 `pip-audit --strict` 为 0 已知漏洞。
- trading-core 全量发布回归：2156 passed、1 skipped。

## 仍然开放的边界

- `atlas.py`、`paper_trading.py` 与 `sync_briefing_site.py` 仍然偏大。拆包会扩大变更半径，建议在本轮稳定提交后单独迁移，并保持 CLI 兼容测试。
- 当前灾备密钥未写入仓库是刻意设计；旧 schema 2 `latest.json` 也不会被静默接链。运维必须显式归档旧指针，并用外部密钥创建 schema 4 genesis。
- 私有子模块的 CI 读取权限必须通过仓库 secret `ATLAS_SUBMODULE_TOKEN` 提供；仓库不会保存该凭据。
- 质量工具已有哈希锁；global-briefing 的大型运行时依赖仍只固定直接版本，尚未形成跨平台完整哈希锁。
- 行情陈旧度目前按工作日上限控制；精确交易所节假日仍需接入官方日历。
- 生产网络、DNS、TLS、边缘响应头及真实通知渠道属于仓库外状态，本地测试不能代替生产验收。
- 研究晋升只接受到期、独立且可重算的样本。样本量、Brier、Log Loss 或 ECE 未达阈值时继续保持 shadow，不以工程测试绿色替代研究有效性。
