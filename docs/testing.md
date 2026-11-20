# 测试与隔离环境

命令从仓库根目录执行。Python 使用 3.12 与 `uv.lock`，Node.js 使用 22 与 `frontend/package-lock.json`；Windows 下将 `npm` 写作 `npm.cmd`。测试记录必须附提交 SHA、命令、退出码、通过/失败/跳过数量及环境，不能把“可执行命令”当作本次已执行。

## 选择测试范围

| 路径 | 覆盖 | 不覆盖 |
| --- | --- | --- |
| 无数据库测试 | 计算、校验、文件测试替身、模拟 API 与任务流程 | PostgreSQL 实际读写、容器启动、真实浏览器。 |
| 原生 PostgreSQL 集成 | 真库的迁移、任务/结果持久化、各研究入口；备份用例还需 PostgreSQL 客户端 | Linux 容器权限和浏览器交互。 |
| `docker-compose.test.yaml` | 测试镜像中的备份恢复与统一数据更新，隔离真实 PostgreSQL | 它的固定 command 不包含整个 `tests/integration/`。 |
| `check_compose_startup.py` | 首次启动、重复启动、目录权限、迁移与失败阻断 | 网页提交回测与比较。 |
| 前端 Node/jsdom | 组件、表单和模拟接口交互 | 实际 worker、数据库、浏览器布局和线上页面。 |
| 隔离 UI 验收 | 浏览器提交临时研究/比较、任务执行、结果与接口一致 | 正式环境冒烟仍需部署后单独做。 |

## 无数据库检查

在没有启用 `FINANCE_QUANT_TEST_POSTGRES` 的独立终端中执行：

```sh
uv sync --locked --extra dev
npm --prefix frontend ci --no-audit --no-fund
uv run ruff check src alembic tests scripts
uv run ruff format --check src alembic tests scripts
uv run mypy
uv run pytest -q --ignore=tests/integration
npm --prefix frontend run format:check
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

`build` 自身也执行 `tsc --noEmit`；单列 `typecheck` 便于定位失败。默认 `uv run pytest -q` 会收集集成文件，但数据库开关未启用时会跳过；跳过不能算集成通过。链接权限等平台跳过也必须记录，涉及该修复的路径需在支持的平台补验。

## 真实 PostgreSQL 集成

使用全新测试服务器或独立测试数据库，不能把开发/生产连接改名后复用。库名须以 `fq_` 开头或以 `_test` 结尾，夹具还会核验实际连接；它们会创建/删除随机 schema，备份恢复夹具会创建/删除一次性数据库。运行账号应具备相应权限，并安装与服务端主版本相同或更高的 `pg_dump`、`pg_restore`，加入 PATH 或设置 `POSTGRES_BIN`。

由管理员在测试服务器预建 `finance_quant_test` 与专用测试账号。以下 PowerShell 示例只展示非敏感配置；密码从本机已配置的秘密环境提供，不写入命令、日志或仓库：

```powershell
$env:POSTGRES_HOST = "127.0.0.1"
$env:POSTGRES_PORT = "5432"
$env:POSTGRES_DB = "finance_quant_test"
$env:POSTGRES_USER = "quant_test"
# 在本机秘密环境设置 POSTGRES_PASSWORD；不要沿用生产密码。
$env:FINANCE_QUANT_TEST_POSTGRES = "1"
uv run pytest -q tests/integration
Remove-Item Env:\FINANCE_QUANT_TEST_POSTGRES
```

此命令包含备份恢复集成；若缺客户端或 CREATEDB 权限，应报告缺口。不能只挑两个数据库文件通过后声称整个集成目录已验收。普通研究集成与完整数据库备份的分组以当前 workflow 和测试夹具为准。

## Compose 备份恢复与启动

在有 Docker Engine、Compose v2 和基础镜像访问能力的 Linux 测试机上，从仓库根目录单独运行此段。每次创建随机项目，全部调用显式传入同一个 `-p`；退出时保存日志并清理，原测试失败码优先保留，测试成功而日志/清理失败也返回非零：

```sh
(
  set +e
  set -u
  test_root="$(mktemp -d "${TMPDIR:-/tmp}/fq-tests.XXXXXX")" || exit 1
  test_project="$(basename "$test_root" | tr '[:upper:]' '[:lower:]' | tr '.' '-')"
  test_compose() { docker compose -p "$test_project" -f docker-compose.test.yaml "$@"; }
  finish_test() {
    result=$?
    trap - EXIT
    test_compose logs --no-color > "$test_root/compose.log" 2>&1
    logs_result=$?
    test_compose down --volumes
    cleanup_result=$?
    printf '测试项目=%s；日志=%s/compose.log；清理退出码=%s\n' "$test_project" "$test_root" "$cleanup_result"
    if [ "$result" -eq 0 ] && [ "$logs_result" -ne 0 ]; then result=$logs_result; fi
    if [ "$result" -eq 0 ] && [ "$cleanup_result" -ne 0 ]; then result=$cleanup_result; fi
    exit "$result"
  }
  trap finish_test EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  test_compose config --quiet || exit $?
  test_compose up --build --abort-on-container-exit --exit-code-from test
  exit $?
)
```

处理该段非零结果及残留后，再独立执行启动检查：

```sh
set -eu
docker build --target application -t finance-quant:startup-check .
python3 scripts/check_compose_startup.py --image finance-quant:startup-check
```

显式 `-p` 覆盖文件默认 name 及外部 COMPOSE_PROJECT_NAME；测试无生产主机目录挂载或数据库端口映射。只清理打印的随机项目，保留日志；清理失败时核该项目的残留，不在正式 Compose 上照搬 `down --volumes`，也不执行全机 `docker system prune`。启动检查脚本另建随机项目与临时目录，并清理其资源。

目标生产机若不能访问 Docker Hub，不在上面执行 `--build`。可在具备构建网络的隔离环境运行这些检查，再按[产物发布流程](operations.md#镜像来源与发布约定)交付 GHCR 镜像；有源码挂载的测试环境须记录基底镜像 digest、挂载清单和源码提交，不能等同已验证最终发布镜像。

## UI 临时回测与比较

以下是每次涉及研究或比较流程时的验收步骤，执行结果另附，不表示本文已经完成浏览器验收。

1. 固定候选提交或镜像 digest，启动独立 API、一个 worker 和测试数据库。项目名、端口、数据库及全部存储路径与生产分离；原生 API 采用构建后的前端同源托管，具体启动见下方“隔离 UI 地址”。不能沿用默认 Vite 代理后只修改测试 API 端口。
2. 使用可重新生成的小型数据夹具或已授权的测试数据副本；记录数据版本、日期、预热、股票池、因子、财务/行业/基准绑定。合成数据只验流程，不宣称投资有效性。
3. 从 `/research/new` 经真实 UI 提交研究，确认任务由排队走到完成，打开对应回测、因子评分和评估。核回测 KPI 与 API `metrics`、日期筛选、持仓、费用和图表；保存路由、运行 ID、请求/响应及截图。
4. 从 `/experiments/new` 分别验证受影响的分段比较或收益来源对照。至少两种合法候选，检查分段边界、选优依据、详情跳转与覆盖；相关改动须覆盖固定池/动态池。不能只手工请求 API 就宣称 UI 提交流程通过。
5. 财务说明改动覆盖无需财务、需要但未绑定、已绑定未认证、历史证据不足；区分真实 0 与 null。旧结果详情/JSON 下载可更正说明，报告与冻结文件不改；按测试范围记录读取前后哈希。
6. 量化测试验 UI 逻辑，量化美工验信息层级、图表和桌面/手机效果；建议共用 1440×900 与 390×844 证据，浏览器、缩放及输入方式一并记录。浏览器不可用时明确未验部分，不用组件测试冒充实际页面。
7. 先保存日志与证据，再停止专用 API/worker/Compose 项目。仅清理本次登记的测试目录、数据库及容器；测试数据不能在服务仍连接时删除。清理失败由执行人列出残留与下一处理人，保留正式备份和其他项目资源。

### 隔离 UI 地址

原生测试先从空白配置准备 `.env.ui-test`，使用独立测试库（如 `finance_quant_ui_test`）、专用账号和独立存储根；所有 `FINANCE_QUANT_*` 路径覆盖也须位于该根内，不复制生产路径。设置 `ALLOWED_ORIGINS=http://127.0.0.1:18122`，端口改动时同步修改完整来源。安装依赖后：

```sh
npm --prefix frontend run build
uv run python -m finance_quant.server migrate --env-file .env.ui-test
uv run python -m finance_quant.server check --env-file .env.ui-test
uv run python -m finance_quant.server api --env-file .env.ui-test --host 127.0.0.1 --port 18122
```

在另一个终端用同一 `.env.ui-test` 启动一个 worker：`uv run python -m finance_quant.server worker --env-file .env.ui-test`。浏览器只打开该 API 托管的 <http://127.0.0.1:18122>。首次提交前，从这个地址核 `/api/v1/health`、已登记测试数据及浏览器 Network 的实际请求地址，确认全部指向隔离实例。

README 的默认 Vite 将 `/api` 代理到 `127.0.0.1:8000`，可能连接现有实例或 SSH 隧道；本步骤不使用这个入口。独立 Compose 同样绑定回环高位端口、配完整 ALLOWED_ORIGINS，并打开该端口同源页面。UI 提交成功、API 结果复核与完整比较流程分别记录，不把只打开比较入口写成比较通过。

## 固定 PR 与发布检查

测试负责人记录 PR head、base、实际检出 SHA、各必要 job 的 run URL 和结论。GitHub PR workflow 默认可能检出合并测试提交，应说明它与 head/base 的关系，不能只抄 PR head 当作实际运行版本。初次 PR 和后续提交都需核验；无结果、跳过必要路径、取消、超时或旧提交通过均不能算当前通过。

基线 `full-tests.yml` 的 job ID 为 `test`、`backup-restore`；后者还运行 Compose 启动检查。本轮 workflow 适配沿用这两个 job，目标是补主干 `master` push 触发及全部非备份数据库集成，真实备份集成继续走 Compose；是否生效以合入的 YAML 和实际 run 为准。workflow 的新增触发、分组和覆盖调整需随对应 PR 同步此处，并核实真实运行。发布另核合并提交的检查、镜像来源和目标机 digest 拉取；本地回归、PR CI、发布产物与线上冒烟是不同证据，职责见[协作指南](contributing.md)。
