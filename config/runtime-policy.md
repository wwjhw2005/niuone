# 运行数据和敏感信息处理策略

简体中文 | [English](runtime-policy_EN.md)

本文档定义 NiuOne 的运行数据、模型密钥和本地私有文件处理规则。目标是让真实数据可以留在工程目录内，同时确保上传到公开仓库的内容不包含用户数据或敏感信息。

## 目录边界

源码目录：

```text
/path/to/NiuOne
```

私有运行目录：

```text
.local-data/
├── dashboard.env
├── .venv/
├── runtime/
└── backups/
```

`.local-data/`、`dashboard.env`、数据库、本地凭据、日志和备份文件都已在 `.gitignore` 中忽略。

## 不应提交或外传的内容

| 路径 | 说明 |
|---|---|
| `.local-data/dashboard.env` | 本机环境变量、路径和可能存在的模型密钥或管理员密码 |
| `.local-data/.venv/` | 本机 Python 虚拟环境 |
| `.local-data/runtime/dashboard_admin_token.txt` | 未配置 `DASHBOARD_ADMIN_PASSWORD` 时使用的 bootstrap 管理密钥 |
| `.local-data/runtime/dashboard_users.db` | 本地访问用户和认证数据 |
| `.local-data/runtime/push_history.db` | 消息历史 |
| `.local-data/runtime/news/realtime_news_latest.json` | NewsNow 有界滚动快讯历史及非敏感来源状态；成功刷新按 ID 合并并受总量/重要条数上限约束，上游失败时只读回退，不覆盖真实交易记录 |
| `.local-data/runtime/niuniu.db` | 实战页面交易、账户、完整候选机会集、持仓五阶段路径/退出阶段、决策耐久证据，以及只追加的成交/决策/权益历史版本 |
| `.local-data/runtime/cron/output/niuone_forward_evaluation.json` | 牛牛严格前向聚合、五阶段机会/定仓漏斗、个股资金活跃度与全市场/题材内成交额分位、持仓路径/阶段转移/退出阶段、拒单分类、交易级与日期×行业簇稳健胜率区间、每日组合收益/回撤、成效门、覆盖诊断和影子分组结果 |
| `.local-data/runtime/cron/state/niuone_forward_protocol.json` | 牛牛严格前向队列冻结的代码/非密钥运行配置指纹、不含股票代码的起始账户边界，以及每日牛牛新仓上限与跨决策轮次计数口径 |
| `.local-data/runtime/cron/state/niuone_cron_scheduler.json` | 有界保留的 Cron 运行键与严格前向每日任务结果 |
| `.local-data/runtime/cron/state/b1_schedule_state.json` | 有界保留的 Practice 配置时点扫描/决策终态 |
| `.local-data/runtime/market_data/tencent_daily_klines.sqlite3` | 盘前预热并由盘中扫描增量补齐的全市场日 K 缓存 |
| `.local-data/runtime/backtesting/` | 各策略当前一次回测的服务端进度与结果、短生命周期子进程交换文件，以及按协议/数据/分类内容寻址的压缩选股回放带；不保存可供其他模块读取的通用历史日 K 缓存 |
| `.local-data/runtime/config.yaml` | 模型服务商、模型和模型密钥配置 |
| `.local-data/runtime/cron/state/` | 定时任务和补跑状态 |
| `.local-data/runtime/cron/output/` | 实战选股缓存、模拟账户状态和其他非消息类运行缓存 |
| `.local-data/runtime/cron/output/multi_strategy_history/` | 完整选股扫描的有界复盘快照；只保留最近一个归档日期且每个日期最多 12 轮 |
| `.local-data/runtime/cron/output/b1_history/` | 已停写的旧 B1 重复归档；下一轮成功扫描只清理其中符合标准日期/时间戳格式的旧 JSON，未知文件保持不动 |
| `.local-data/runtime/logs/` | 服务和任务日志 |
| `.local-data/backups/` | 部署备份，可能包含旧配置 |

Dashboard 增量接口只允许返回 `.local-data/runtime/public-data/` 中由 `public_projection.py` 字段白名单生成的内容。不要把其父目录、数据库或 `cron/output/` 配置为静态站点根目录。若同步到 CDN，必须以 `objects/`、`manifests/`、`latest.json` 为精确边界，并在每次 schema 变更后重新检查脱敏测试。

Compose 内置 NewsNow 的数据库和缓存位于独立 Docker volume `newsnow-data`，不位于仓库或 `niuone-data`。它同样属于私有运行数据；备份容器部署时应单独备份该卷，不要上传其内容。`docker compose down` 会保留它，而 `docker compose down -v` 会删除它及牛牛1号主数据卷，执行前必须确认备份。

不要把上述内容复制到 issue、PR、README、文档示例或聊天上下文。排查问题时只提供脱敏后的错误类型、时间点和必要字段。

## 模型密钥

推荐用途：

| 用途 | 推荐模型 | 配置项 |
|---|---|---|
| 美股机构评级日报 | Financial Modeling Prep 结构化数据与本地确定性规则 | `FMP_API_BASE_URL`、`FMP_API_KEY`、`FMP_RATING_MAX_RESULTS` |
| 买卖决策、文字策略细化、消息判断及 A 股/隔夜美股盘面总结 | 共享的 OpenAI 兼容模型 | `DASHBOARD_DECISION_BASE_URL`、`DASHBOARD_DECISION_API_KEY`、`DASHBOARD_DECISION_MODEL`、`DASHBOARD_DECISION_STREAM_MODE`、`DASHBOARD_DECISION_REASONING_EFFORT` |
| A 股候选股及龙虎榜连板/连榜股票消息面预检 | 同花顺问财 OpenAPI | `IWENCAI_NEWS_PRECHECK_ENABLED` 及 `IWENCAI_*` |
| 综合决策参考 | 本地聚合，不需要额外模型 | `DASHBOARD_DECISION_INTELLIGENCE_ENABLED`、`DASHBOARD_DECISION_INTELLIGENCE_TTL_SECONDS`、`DASHBOARD_DECISION_INTELLIGENCE_MAX_ITEMS` |

美股机构评级日报由 `DASHBOARD_US_FEATURES_ENABLED` 总开关控制。关闭时设置页隐藏相关配置，并跳过美股评级定时任务。

综合决策参考会读取本地行情缓存、盘面消息历史和模拟账户状态，并把压缩后的摘要写入决策日志；它不新增模型密钥，但日志中可能包含候选消息面摘要，公开排障前仍需按运行数据策略检查。

模型及 FMP 密钥只允许保存在 `.local-data/dashboard.env`、`.local-data/runtime/config.yaml` 或受控的系统环境变量中。提交前必须确认没有新增 `.env`、`*.key`、`*.token`、`*.secret`、数据库或备份文件。

问财数据源使用 `IWENCAI_API_KEY`，同样只允许保存到 `.local-data/dashboard.env` 或受控系统环境变量。
海外部署可通过 `DASHBOARD_CN_DATA_PROXY_URL=socks5h://host:port` 仅代理腾讯、东方财富、新浪和问财等国内数据源；该地址不得包含用户名、密码、查询参数或路径。配置代理后连接失败必须沿用有界超时、重试与缓存降级，不得静默改为直连。Docker Compose 会把回环代理主机映射为宿主机 `host.docker.internal`。模型、通知、FMP 和 NewsNow 不使用此配置。
`IWENCAI_ENABLED` 默认关闭；问财数据仅作为研究快照和现有行情的补充，不得用不完整或缓存响应覆盖账户、成交和真实交易记录。
`IWENCAI_NEWS_PRECHECK_ENABLED=1` 启用消息面预检：系统组合调用问财官方 `announcement-search`、`news-search` 和 `hithink-event-query` 检索证据，不包含雪球/X 舆情。公告和新闻走 `/v1/comprehensive/search`，结构化事件走 `/v1/query2data`；结果按股票身份及最近 3 天过滤并跨来源去重。存在有效证据时，必须复用 `DASHBOARD_DECISION_*` 买卖决策模型判断利好、利空或中性；无有效证据时直接记为中性，不调用模型。模型未配置、超时或输出不可解析时标记判断不可用，不得回退关键词匹配。预检失败、超时、未检查、待判断或不可用的记录不进入买卖决策消息证据，在候选摘要中统一映射为中性、权重 0，不得因缺失辅助信息降分、降优先级、缩仓或单独阻止买卖。每个问财技能独立保留成功、证据数和非敏感错误码；不得把价格或资金流当作消息证据。旧 `DASHBOARD_NEWS_*` 配置不再读取。
龙虎榜任务默认在 A 股交易日北京时间 18:00 更新；只保留最近一次非空成功响应，并在下一次成功查询后原子替换。失败或空结果必须继续保留上一份有效数据。升级前生成的交易日归档会在下一次成功更新后清理；买卖前五席位明细单独失败时，仅在查询日期相同时保留当前快照中已有的机构、营业部及其他有效席位记录。
连续上榜只能由相邻 A 股交易日的成功滚动快照确认；缺失中间快照时必须重置，不能跨数据缺口推测。消息面预检批次以 `IWENCAI_DRAGON_TIGER_CRON` 配置的本次龙虎榜计划查询时间为起点，不使用上游响应的 `generated_at`；开启后，龙虎榜成功返回会查询尚未预检的连板或连续上榜股票。快照按股票保存已检索和待检索状态，全部完成后同日不得重复查询问财。开关关闭、配置缺失、限流、超时或解析失败不得阻断或清空龙虎榜主数据。快照和公开 API 只保存结构化摘要、情绪标签、查询时间、检索状态与非敏感错误码，不保存密钥或完整上游响应。
龙虎榜当日数据以及下一次成功查询前仍在滚动快照中的最近数据保持公开；查询更早日期时必须先建立有效管理员会话。当日实时回源为空时，接口必须回退到最近成功快照，不得在新数据生成前把页面替换为空状态。所有非当日响应均不得使用公共或 CDN 缓存，确保滚动快照更新后旧日期立即恢复保护。

## 本地副本和测试

不要直接拿真实 `.local-data/runtime/` 做实验。测试时使用临时运行目录：

```bash
DASHBOARD_HOME=/tmp/niuone-smoke DASHBOARD_PORT=8877 ./scripts/run_standalone.sh
```

提交前运行：

```bash
./scripts/validate.sh
git status --ignored --short
```

`.local-data/` 应显示为 ignored，不应出现在 staged files 中。

## 发布和备份

本机部署脚本会把当前 `app/`、环境文件和启动脚本备份到：

```text
.local-data/backups/
```

备份目录同样属于私有数据区域，不应提交或外传。回滚时优先从备份恢复 `app/`，或使用 `git revert` 做非破坏性提交回滚。

## 处理疑似泄露

如果模型密钥、本地凭据或数据库误入公开位置：

1. 立即撤销或轮换对应密钥或凭据。
2. 从代码和文档中删除泄露内容。
3. 检查 `git status --ignored --short` 和最近提交。
4. 未配置管理员密码时，必要时重建 `.local-data/runtime/dashboard_admin_token.txt`；按需重建相关数据库。
5. 对已经推送到远端的敏感内容，按远端平台的泄露处理流程清理历史。
