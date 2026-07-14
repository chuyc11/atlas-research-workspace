# ATLAS 项目安全审查、修复与报告质量复核

审查日期：2026-07-14
审查范围：根目录 Python 编排、`work/global-briefing`、`work/trading-core`、`src` React/Next.js（Vinext/Cloudflare）前端，以及配置、测试、CI 和既有审计报告。
证据口径：仅将代码、配置、扫描器原始输出或可重复测试支持的事项列为已确认；生产边缘、网络出口和数据分级审批等仓库外事实均标为待验证。

## 1. 执行摘要

### 项目整体安全状况

本轮已修复原审查中的主要代码风险，并补齐 V36 真实安全证据链。当前没有确认仍开放的严重或高危代码漏洞。公开只读前端的本地构建、响应头、数据投影和浏览器测试已通过；`trading-core` 的密钥、依赖、静态代码、文件删除和网络边界五类扫描也已通过。

项目仍不应直接签署“生产正式上线通过”。原因不是已知高危漏洞仍未修复，也不是本地测试失败：本轮 `trading-core` 完整回归共 2,149 项，0 failure、0 error、1 skipped，已经 100% 结束并通过。剩余限制是当前工作树尚未形成干净提交，且真实生产边缘的源站隔离、身份头剥离、响应头、缓存和网络出口策略没有仓库外运行证据。

### 最严重的三个已确认问题

1. **SEC-001（高，已修复）**：可变日期曾进入 `shell=True` 命令字符串，具备条件性命令注入风险。
2. **SEC-002（高，已修复）**：V36 曾用默认布尔值自证安全通过，可能造成错误上线决策。
3. **SEC-011（高，已修复）**：历史回放日期进入待递归删除目录名，可能形成路径穿越和越界删除。

### 当前最需要处理的事项

1. 将修复形成干净、可审查的提交后重新执行完整回归和 V36，使证据与发布提交一一对应。
2. 在真实生产域名和运行环境验证边缘鉴权、源站隔离、安全响应头、缓存及 egress allowlist。
3. 归档本轮 JUnit、V36 原始扫描和风险接受记录，避免报告与证据再次脱节。

### 现有报告是否值得信任

旧报告中的具体代码证据部分有价值，但整体版本不值得直接用于验收：其顶部写“已修复”，正文仍把同一问题写成“未修复/阻止上线”，并混用了修复前和修复后的测试数据。本文件已按当前状态重写，明确区分“历史已修复”“当前残余风险”和“仓库外待验证”。

### 是否足以支持上线、验收或管理决策

- 可支持：确认修复已进入代码、确定剩余技术风险、安排复测和生产验证。
- 不足以单独支持：生产正式上线签署。还需要干净提交上的重跑证据和生产环境验证记录。

## 2. 漏洞与风险清单

| 编号 | 问题 | 位置 | 严重程度 | 当前影响 | 证据 | 修复优先级 | 置信度 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-001 | 日期参数进入 shell 命令 | `equity_current_day_builds/execution_record.py:77-86`；`equity_build_repeatability/execution_record.py:59-67` | 高 | 已修复；当前使用固定 argv 和 `shell=False` | 注入负向测试、安全回归、源码扫描 | P1→已完成 | 高 |
| SEC-002 | V36 默认值自证安全通过 | `equity_release_chain/generic.py:363-463`；`security/v36_evidence.py:54-112` | 高 | 已修复；缺证据、证据变更或工具失败均阻断 | 五类原始 JSON、SHA-256、范围清单、V36 构建/审计通过 | P1→已完成 | 高 |
| SEC-003 | RSS 单源异常中断整批 | `rss_collect.py:411-444` | 中 | 已修复；安全解析异常按单源隔离 | 恶意 XML/异常源后续正常源继续执行测试 | P1→已完成 | 高 |
| SEC-004 | RSS 重定向/私网 SSRF | `rss_collect.py:67-151` | 中 | 应用层已修复；DNS 校验到连接仍需 egress 控制 | HTTPS/主机/端口/IP/逐跳重定向负向测试 | P1→代码完成，部署待验 | 中高 |
| SEC-005 | 公开网页暴露内部组合与状态 | `sync_briefing_site.py:752-771,1061-1075,1419-1443` | 中 | 已采用公开 DTO；账户、仓位、现金、权益和内部状态不进入浏览器 | schema 断言、bundle 扫描、浏览器测试 | P1→已完成 | 高 |
| SEC-006 | Python/外部仓库供应链不可复现 | `pyproject.toml:12-18`；`download_external_research_repos.py:9-20` | 中 | 直接依赖和外部 commit 已固定；跨平台传递依赖哈希锁仍待强化 | pip-audit、固定 SHA、CI SBOM | P2 | 高 |
| SEC-007 | 客户端 bundle 泄露本机绝对路径 | `sync_briefing_site.py:1326-1335,1442-1443` | 低 | 已修复 | 生成 JSON、HTML/RSC、生产 bundle 路径扫描 | P2→已完成 | 高 |
| SEC-008 | CI/浏览器门禁覆盖不足 | `.github/workflows/quality.yml`；`src/tests/interactive-ui.py:43-129` | 中 | 已加入安全门、夜间全量门和动态 UI 断言；本轮 2,149 项完整回归通过 | CI 配置、UI 测试、JUnit | P2→已完成 | 高 |
| SEC-009 | npm Moderate 公告残余 | `src/package-lock.json`；`src/security/npm-audit-exceptions.json` | 低 | 6 个 Moderate（生产 2），当前无已确认利用链 | npm audit 和到期例外 | P2 | 高 |
| SEC-010 | 未使用鉴权 helper 信任身份头 | 原 `src/app/chatgpt-auth.ts` | 信息 | helper 已删除；生产边缘身份头契约仍待运行验证 | 全仓无 helper/调用点 | P3 | 中 |
| SEC-011 | 回放目录路径穿越和越界递归删除 | `historical_dry_run_replay.py:74-102,236-263` | 高 | 已修复；日期严格 ISO，删除前解析并约束到 replay 根目录 | 14 项回放/Universe 测试、Bandit、文件策略扫描 | P0/P1→已完成 | 高 |
| RISK-001 | V36 当前绑定的是脏工作树 | `data/security_evidence/v36_security_assessment.json` | 中（发布治理） | 扫描结果与当前 2,363 个文件哈希一致，但尚非可签署发布提交 | `scope_worktree_dirty=true`、范围清单 SHA-256 | P1 | 高 |
| RISK-002 | 生产边缘与 egress 缺运行证据 | 仓库外 Cloudflare/OpenAI/网络策略 | 中（待验证） | 不能确认真实域名与源站绕过路径的最终控制 | 本地代码不能证明外部平台状态 | P1 | 中 |

## 3. 详细问题分析

### SEC-001：条件性命令注入（已修复）

- 问题名称：未验证日期进入 shell 命令。
- 位置：`work/trading-core/src/trading_core/equity_current_day_builds/execution_record.py:77-86`；`equity_build_repeatability/execution_record.py:59-67`；统一实现位于 `system/safe_workflow_command.py`。
- 修复前证据：日期字符串被格式化到命令文本并通过 `shell=True` 执行。
- 原因：把展示命令和执行参数混为一个字符串，且 API 层未保证规范 ISO 日期。
- 攻击场景：能控制 CLI/自动化日期参数的调用者注入 shell 元字符，以运行账户权限执行额外命令。
- 影响范围：本地/CI 文件、凭证、网络权限和研究产物。
- 严重程度：高。
- 触发条件：攻击者能影响日期参数并触发对应执行记录流程。
- 修复：日期用 `date.fromisoformat` 规范化；执行只接受固定 argv；使用 `sys.executable` 和 `shell=False`；计划与执行 argv 不一致时 fail-closed。
- 修复示例：`subprocess.run(current_day_workflow_argv(date, executable=True), shell=False, check=False)`。
- 验证：命令分隔符、引号、路径片段和计划篡改负向测试；源码扫描确认无 `shell=True`。
- 置信度：高。
- 误报：不是误报；利用需要本地/自动化参数控制权，因此不是匿名互联网直接利用。

### SEC-002：安全报告自证通过（已修复）

- 问题名称：没有真实扫描仍产生 `overall_passed=true`。
- 位置：`work/trading-core/src/trading_core/equity_release_chain/generic.py:363-463`；证据生成器 `security/v36_evidence.py:54-112`。
- 修复前证据：V36 组件把规格里的 `required_true/false` 直接复制为扫描结论，漏洞库不可用仍显示通过。
- 原因：把期望状态误当成观测结果，没有外部证据合同。
- 攻击/失败场景：真实密钥或依赖漏洞未被扫描，但管理层看到“通过”并放行。
- 影响范围：上线、验收、供应链和管理决策。
- 严重程度：高。
- 触发条件：依赖 V36 报告作为安全结论。
- 修复：新增五类真实扫描；记录工具/版本、原始证据路径和哈希；schema v2 绑定提交、逐文件范围清单和当前文件哈希；缺失、篡改、过期、漏洞库不可用或任一扫描失败均阻断。
- 修复伪代码：`passed = all(scan.status == "passed") and manifest == collect_current_scope()`。
- 验证：当前五项扫描通过，V36 build/audit 均 `overall_passed=true`；删除或修改证据的负向测试保持失败。
- 置信度：高。
- 误报：原问题不是误报。当前通过仅覆盖证据声明的代码/配置范围，不代表生产基础设施自动通过。

### SEC-003：RSS 单源异常导致批次中断（已修复）

- 问题名称：安全解析异常未隔离导致整批采集中止。
- 位置：`work/global-briefing/scripts/rss_collect.py:411-444`。
- 证据：修复前循环未捕获 `ValueError` 和 defusedxml 安全异常；现在按单源记录错误并继续。
- 原因：异常分类只考虑普通网络/解析失败。
- 场景与影响：恶意或损坏源可中断整个情报采集批次，造成可用性和发布延迟。
- 严重程度：中；触发需影响一个已配置源。
- 修复：捕获网络、编码、策略和安全 XML 异常，隔离单源，禁止吞掉编程错误。
- 修复示例：`for source in sources: try: collect(source) except EXPECTED_SOURCE_ERRORS as exc: record_error(source, exc); continue`。
- 验证：恶意 XML 后的正常源仍被采集。
- 置信度：高；不是误报。

### SEC-004：条件性 SSRF（代码已修复，部署待验）

- 位置：`rss_collect.py:67-151`。
- 证据：逐跳调用 `validate_public_https_url`；只允许配置主机、HTTPS、443；DNS 地址必须为全局地址；自动重定向关闭，最多三跳。
- 原因：原实现默认信任远端重定向和 DNS。
- 场景与影响：受控源重定向到内网/云元数据，利用采集机网络权限访问内部服务。
- 严重程度：中。
- 前提：攻击者控制配置源、DNS 或远端响应，且运行环境可达敏感网段。
- 修复：应用层 URL/IP/重定向约束；生产网络层按源域/IP egress allowlist 阻断竞态和代理绕过。
- 修复示例：每一跳先执行 `validate_public_https_url(next_url, allowed_hosts)`，关闭自动重定向，再由网络策略拒绝私网、链路本地和云元数据地址。
- 验证：覆盖 HTTP、非 443、localhost、RFC1918、链路本地、IPv6 回环、跨域和私网重定向。
- 置信度：中高。
- 误报：若生产进程已完全无内网访问能力，影响降低；该部署事实尚未提供。

### SEC-005：公开数据边界不清（已修复）

- 位置：`sync_briefing_site.py:752-771,1061-1075,1419-1443`；前端断言 `src/tests/rendered-html.test.mjs:98-104`。
- 证据：发布前通过 `public_portfolio` 和 `public_system_status` 投影；校验明确拒绝 `accountId/positions/cash/equity/realizedPnl/initialCash`。
- 原因：原发布模型直接复用内部纸面账户对象。
- 场景与影响：匿名访问者获得内部持仓、资金、账户名和运行状态。
- 严重程度：中；若数据被业务定义为受限可升为高。
- 触发条件：内部对象被同步到公开站点，且匿名用户可读取生成的 JSON、SSR/RSC 或 JS 资源。
- 修复：公开 DTO allowlist、匿名化资产标签、删除内部状态和路径；不依赖前端隐藏。
- 修复示例：`public_payload = {"asOf": source["asOf"], "summary": project_public_summary(source)}`，任何账户、持仓和资金字段均不在 allowlist 中。
- 验证：payload schema、SSR、生产 bundle 和真实浏览器均检查受限字段不存在。
- 置信度：高。
- 误报：若业务明确批准全部数据公开，原授权问题影响下降；绝对路径和最小披露仍应修复。

### SEC-006：供应链不可复现（主要部分已修复）

- 位置：根/子项目 requirements、`work/trading-core/pyproject.toml:12-18`、`scripts/download_external_research_repos.py:9-20`。
- 证据：直接 Python 依赖固定版本；11 个外部仓库固定完整 SHA 并验证 HEAD；CI 生成 CycloneDX SBOM；pip-audit 当前审计 7 个解析依赖且无已知漏洞。
- 原因：原依赖和 Git 默认分支可漂移。
- 攻击场景：上游发布或仓库被接管后，重装/重克隆获得不同代码。
- 影响范围：开发机、CI、研究结果和供应链。
- 严重程度：中。
- 前提：重新安装、克隆或缓存失效。
- 修复建议：继续增加跨平台传递依赖哈希锁；外部镜像保持“不导入、不自动执行”，升级 commit 必须审查。
- 修复示例：依赖安装使用带哈希锁文件；下载器执行 `git checkout <full_sha>` 后验证 `git rev-parse HEAD == expected_sha`，不执行镜像内代码。
- 验证：干净环境可重复安装；commit 不符立即失败；SBOM/pip-audit 原始结果归档。
- 置信度：高。
- 误报：不是误报；当前残余是治理强化而非已确认可利用漏洞。

### SEC-007：绝对路径泄露（已修复）

- 位置：`sync_briefing_site.py:1326-1335,1442-1443`。
- 证据：发布校验递归拒绝 Windows/Linux 用户目录绝对路径；阶段 detail 不再进入公开 payload。
- 原因：内部产物对象被直接序列化到公开输出，缺少发布边界投影。
- 场景与影响：客户端 bundle 暴露用户名、目录结构和内部产物名。
- 严重程度：低；无需特殊利用前提，只需下载公开资源。
- 修复：公开字段 allowlist，路径改为稳定逻辑 ID/仓库内相对标识。
- 修复示例：`public_ref = artifact.logical_id`；发布校验递归拒绝匹配盘符根、UNC 根或 `/home|/Users` 的值。
- 验证：构建后扫描 JSON、HTML/RSC 和 JS chunk。
- 置信度：高；不是误报。

### SEC-008：测试门禁不足（已修复并持续验证）

- 位置：`work/trading-core/.github/workflows/quality.yml`；`src/tests/interactive-ui.py:43-129`。
- 证据：PR 运行固定安全负向测试；安全 job 生成真实 V36 证据和 SBOM；计划/手动 job 执行完整 pytest 并上传 JUnit；UI 断言从当前 payload 派生。
- 原因：旧门禁只覆盖部分模块，UI 检查又绑定固定日报内容，不能稳定代表当前数据契约。
- 影响：未覆盖模块回归可能被合并，硬编码日报内容会制造假失败并导致门禁被忽略。
- 严重程度：中（治理风险）。
- 触发条件：合并触及未覆盖路径，或日报日期/内容变化。
- 修复：快速安全门 + 完整夜间/发布门；动态 UI 契约。
- 修复示例：PR 先跑安全负向用例和 V36；发布门必须消费完整 JUnit 且要求 `failures == errors == 0`。
- 验证：本轮 JUnit 记录 2,149 tests、0 failure、0 error、1 skipped；故意注入 fixture 风险应阻断。
- 置信度：高。
- 误报：若有仓库外 CI 可降低原影响；此前未提供相关证据。

### SEC-009：npm Moderate 公告（风险接受）

- 位置：`src/package-lock.json`、`src/security/npm-audit-exceptions.json`。
- 证据：2026-07-14 全范围 6 Moderate、0 High、0 Critical；生产范围 2 Moderate。
- 原因：当前 Vinext/Vite 工具链仍传递引入相关版本，直接强制升级可能破坏框架兼容性。
- 场景：旧 esbuild 开发服务器对不可信网络开放，或未来新增用户可控 CSS 进入受影响 PostCSS 链。
- 影响：当前生产利用链未确认；开发工具链和未来功能存在条件性风险。
- 严重程度：低。
- 触发条件：开发服务暴露给不可信网络，或应用开始处理攻击者可控 CSS；若依赖图或公告严重度变化也需重评。
- 修复：High/Critical 持续阻断；Moderate 例外记录依赖路径、利用前提、责任人和 2026-08-31 到期日；不使用破坏性 `audit fix --force`。
- 修复示例：CI 对未列入例外或已过期的公告返回失败；升级后删除对应 exception 并重跑构建、SSR 和浏览器测试。
- 验证：每次构建保存 `npm audit --json`，到期或触发条件变化必须重评。
- 置信度：高。
- 误报：公告真实，但当前可利用性不足，降级合理。

### SEC-010：身份头 helper 信任边界（已移除）

- 位置：原 `src/app/chatgpt-auth.ts`。
- 证据：原 helper 仅凭请求头构造用户且无调用点；现已删除。
- 原因：helper 把边缘注入头当作可信身份，但自身不验证签名、会话或请求是否确实来自可信边缘。
- 攻击场景：若该 helper 被接入授权路径且源站允许公网直连，客户端可能伪造同名头获得他人身份。
- 影响：若未来直接用于授权且边缘允许客户端伪造同名头，会造成认证绕过。
- 严重程度：信息；启用类似逻辑前为 P1。
- 触发条件：恢复或重写同类 helper、将其用于授权、且边缘/源站未建立可信请求边界。
- 修复：不要恢复该 helper；需要鉴权时使用平台可验证签名/会话、边缘剥离客户端同名头、源站网络隔离和资源级授权。
- 修复伪代码：`identity = verify_signed_session(request)`；验证失败保持匿名，授权时再校验 `can_access(identity, resource)`。
- 验证：公网伪造身份头仍匿名；绕过边缘无法访问源站。
- 置信度：中。
- 误报：当前无调用点，因此不是当前可利用漏洞；属于已消除的待启用风险。

### SEC-011：历史回放路径穿越导致递归删除（已修复）

- 问题名称：未校验日期形成删除目录。
- 位置：`work/trading-core/src/trading_core/evaluation/historical_dry_run_replay.py:74-102,236-263`。
- 修复前证据：`replay_id = f"replay-{start_date}-{end_date}"` 后直接对派生路径执行 `shutil.rmtree`，日期来自 CLI 且无格式约束。
- 原因：业务日期同时被用作文件路径标识，删除前没有 canonicalize-and-confine。
- 攻击场景：能控制本地 CLI/自动化参数的调用者使用路径分隔符和 `..`，令回放重置逻辑删除预期 replay 根以外的可写目录。
- 影响范围：运行账户可写的研究数据、输出或相邻目录。
- 严重程度：高。
- 触发条件：调用回放命令、价格包可读取、攻击者可控制日期参数。
- 修复：开始任何读写前严格解析规范 `YYYY-MM-DD`，校验起止顺序和正数窗口；删除前 `resolve()`，要求 `relative_to(allowed_root)` 成功且目标不等于根目录。
- 修复示例：`resolved.relative_to(replay_root.resolve())` 失败即抛错；仅对验证后的 `resolved` 执行删除。
- 验证：路径分隔符、非规范日期、逆序日期和零窗口均抛错，外部 sentinel 文件保持不变；文件策略扫描确认唯一递归删除 sink 具备全部保护条件。
- 置信度：高。
- 误报：不是误报；但需要本地/自动化调用权限，不是匿名远程漏洞。

### RISK-001：安全证据绑定脏工作树（当前发布阻断）

- 问题名称：通过的安全证据尚未绑定可审查的干净发布提交。
- 位置：`work/trading-core/data/security_evidence/v36_security_assessment.json` 和 `v36_scope_manifest.json`。
- 证据：schema v2 记录 `scope_worktree_dirty=true`；2,363 个范围内文件哈希与当前工作树一致，但 Git 提交本身不包含这些未提交变更。
- 原因：修复、扫描和验收在工作树中完成，尚未进入最终发布提交。
- 失败场景：后续提交遗漏、额外修改或选错构建来源，管理者仍误用当前扫描结果放行不同代码。
- 影响范围：发布可追溯性、复测可信度和审计问责；不是新的运行时代码漏洞。
- 严重程度：中（发布治理）；P1。
- 触发条件：直接用当前证据签署发布，或实际部署源码与范围清单不一致。
- 修复建议：审查并形成干净提交，在该提交上重新执行完整 pytest、V36 和构建；发布包保存 commit、JUnit、范围清单及哈希。
- 修复伪代码：`require git_is_clean() and evidence.scope_commit == deploy_commit and evidence.manifest == current_scope`。
- 验证：`git status --porcelain` 为空；重新生成证据后 `scope_worktree_dirty=false`；V36 validator、完整回归和发布构建全部通过。
- 置信度：高。
- 误报：不是误报；当前证据确实覆盖工作树，因此不否定本轮修复效果，只阻止把它当作最终发布制品证明。

### RISK-002：生产边缘与网络出口缺少运行证据（需要进一步验证）

- 问题名称：仓库内控制不能证明真实生产边缘、源站和 egress 策略已按设计生效。
- 位置：仓库外 Cloudflare/OpenAI 边缘配置、DNS/源站访问控制、运行网络策略和访问日志。
- 证据：本地测试可证明应用代码与构建产物；未提供真实域名响应、源站直连测试、身份头处理日志或网络策略导出。
- 原因：这些控制属于部署平台状态，不能从源码静态推导。
- 攻击/失败场景：源站可绕过边缘直连、客户端身份头未剥离、安全头只在部分路由存在，或采集进程仍可访问私网/元数据服务。
- 影响范围：认证边界、SSRF 防护、浏览器安全策略和缓存数据泄露。
- 严重程度：中（待验证）；若发现源站绕过或身份伪造可升级为高。
- 触发条件：生产部署配置偏离代码假设，或攻击者能直接访问源站/控制出站目标。
- 修复建议：限制源站仅接受可信边缘；剥离并重建身份头；对公开/错误/缓存路由统一安全头；网络层启用最小 egress allowlist。
- 修复伪代码：`edge verifies session -> strips external identity headers -> adds signed identity -> origin accepts edge network only`。
- 验证：对真实域名和源站地址分别测试匿名、伪造头、错误路由、缓存命中；从运行容器验证私网和元数据地址不可达并归档策略/日志。
- 置信度：中。
- 误报：可能是部署侧已实现但未提供证据；在获得配置导出和运行测试前不能判定为已确认漏洞，也不能宣称已通过。

## 4. 报告内容质量评估

| 报告内容 | 是否有用 | 证据充分度 | 原有问题 | 本次修改 |
| --- | --- | --- | --- | --- |
| 旧 Web 依赖升级与 CSP 修复 | 有明确证据且有实际价值 | 高 | 历史状态和当前状态混在一起 | 改为历史已修复，并保留当前 npm 审计状态 |
| “未发现 XSS 高危 sink” | 有用 | 中高 | 未明确生成 payload 和 URL 也是不可信边界 | 加入公开 DTO、URL schema、bundle 扫描证据 |
| “浏览器门禁已生效” | 当前可用 | 高 | 旧版曾硬编码日报日期/URL，数据更新后失效 | 改为从当前 payload 派生并记录桌面/移动结果 |
| V36 `overall_passed` | 当前可用 | 高 | 原版由默认值生成，严重误导 | 接入五类真实扫描、原始证据、哈希和范围清单 |
| V36 供应链结论 | 当前有用 | 中高 | 原版没有包、版本、工具或漏洞库状态 | 记录 pip-audit 版本、7 个解析依赖、漏洞服务和原始结果 |
| 修复状态表 | 有用 | 高 | 顶部称已修复，正文仍称未修复 | 全文统一为当前状态，并把历史证据放在详细分析 |
| 上线结论 | 有用但必须有边界 | 中高 | 旧版把前端局部结论外推到全项目 | 分开本地代码、公开前端、正式发布和生产环境 |
| 测试数量/结果 | 有用 | 高 | 旧数据过期且一次只跑到 47% | 更新为本轮 2,149 项完整 JUnit：0 failure、0 error、1 skipped |

报告内容分类：

- **有明确证据且有实际价值**：SEC-001/002/003/005/007/008/011 的修复；V36 五类扫描；前端安全头和公开 DTO；npm 当前风险分布。
- **方向正确但证据不足**：生产边缘身份头剥离、源站隔离、真实 egress allowlist；需要运行环境证据。
- **内容过于泛化、无法指导行动**：旧 V36 重复的通用布尔字段，已从当前结论中废弃。
- **判断错误或可能误导**：旧 V36 无扫描却通过；旧报告同时声称问题已修复和仍阻止上线；均已纠正。
- **重要但被遗漏的内容**：回放目录路径穿越、证据与工作树文件哈希绑定；已补充。

## 5. 遗漏项和需要进一步验证的事项

1. 真实生产域名的 CSP、`X-Content-Type-Options`、点击劫持、Referrer/Permissions Policy、缓存和错误路由响应。
2. Cloudflare/OpenAI 边缘是否剥离外部同名身份头，以及源站是否只能由可信边缘访问。
3. 生产网络层 egress allowlist 是否阻断 DNS rebinding、代理和应用层校验竞态。
4. 公开数据字段的业务所有者审批记录和复审周期。
5. Git 历史级密钥扫描；本轮 detect-secrets 覆盖当前一方文件，未声称完整历史扫描。
6. Python 跨平台传递依赖的哈希锁；当前为直接依赖精确版本、pip-audit 和 SBOM。
7. 备份加密、访问权限、保留/销毁策略以及外部目标权限。
8. 日志集中化、脱敏、告警责任人、响应 SLA 和安全事件演练证据。
9. `external_research` 镜像不导入、不自动执行的持续门禁；当前以固定 commit 和流程约束管理。

## 6. 修复优先级

### P0

当前没有仍开放、已确认可造成系统接管、大规模数据泄露或核心业务损失的 P0。SEC-011 的越界删除已在本轮立即修复。

### P1：正式发布前必须完成

1. 形成干净提交后重新运行完整回归和 V36，确认 JUnit、范围清单和发布 commit 一致。
2. 完成真实生产边缘、源站隔离和 egress 验证。
3. 保存并审阅风险接受记录，确保 npm Moderate 在到期日前处理。

### P2：近期版本

1. Python 传递依赖哈希锁和跨平台可重复安装。
2. npm Moderate 例外在 2026-08-31 前升级或重新审批。
3. Git 历史密钥扫描与统一 SBOM/漏洞证据留存。

### P3：长期治理

1. 数据分级、风险接受、责任人、SLA 和验收证据统一登记。
2. 备份、日志、外部数据源和云绑定周期性威胁建模。
3. 生产安全控制定期自动复测。

## 7. 结论

### 当前项目是否适合上线

- 本地公开只读前端：代码与本地运行验收通过，可进入生产环境复核。
- 整个 ATLAS 工作台：当前完整回归已通过；在干净提交证据和生产边缘验证完成前，仍不应签署正式生产上线通过。

### 哪些问题会阻止上线

当前没有已知未修复的高危代码漏洞或本地回归失败阻止上线；剩余阻断项是发布提交可追溯性和生产环境控制验证。

### 报告是否可信

本报告对代码和本地扫描范围可信；对生产边缘/网络状态明确不作无证据推断。旧 V36 和旧报告中的冲突结论不得继续用于决策。

### 可以直接采用的结论

- SEC-001、SEC-003、SEC-005、SEC-007、SEC-011 已修复并有负向测试。
- V36 当前五类真实扫描通过，原始证据及哈希存在。
- Python 当前解析的 7 个依赖未发现已知漏洞。
- npm 当前无 High/Critical，6 个 Moderate 已按条件接受。
- 公开前端不再携带账户、仓位、现金、权益、盈亏和本机绝对路径。

### 必须重新验证的结论

- 干净发布提交上的 V36 和完整 pytest（当前工作树版本已经通过）。
- 真实生产域名安全头、身份头、源站隔离和缓存。
- 网络层 egress 策略。
- 业务数据所有者对公开字段的正式批准。

### 下一步最优先的三个行动

1. 审查改动后形成干净提交，重跑完整回归并重新生成 V36 证据。
2. 在真实生产环境执行边缘/源站/egress 验收并归档响应头和访问日志证据。
3. 将 JUnit、V36 原始 JSON/SHA-256 和 npm 风险接受记录纳入发布包。

## 验收证据（2026-07-14）

| 验证项 | 结果 |
| --- | --- |
| V31 post-v3 模块 | 21 项通过 |
| 回放目录与 Universe 定向测试 | 14 项通过 |
| V36 定向测试 | 10 项通过 |
| V36 五类真实扫描 | 通过 |
| V36 build/audit | `overall_passed=true` |
| Bandit High severity + High confidence | 0 项 |
| pip-audit | 7 个解析依赖，0 个已知漏洞 |
| V36 范围合同 | 2,363 个源码/脚本/测试/配置文件逐项 SHA-256；当前工作树为 dirty |
| trading-core 完整 pytest | 2,149 项；0 failure、0 error、1 skipped；退出码 0；JUnit `work/trading-core/full-regression.xml` |
