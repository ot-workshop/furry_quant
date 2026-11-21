# Linux 运维

生产环境由 Docker Compose 运行 PostgreSQL、FastAPI 和一个串行 worker。应用镜像由 GitHub Actions 构建并保存在私有 GHCR；服务器不编译源码，也不接受 GitHub 的自动部署连接。

## 镜像来源与发布约定

| 用途 | 约定 |
| --- | --- |
| 新发布目标 | `ghcr.io/ot-workshop/furry_quant:sha-<完整40位提交>`，运行时固定 `ghcr.io/ot-workshop/furry_quant@sha256:<digest>`。 |
| 旧环境 | 既有部署可能使用其他镜像命名空间或短 SHA；当前 workflow 的命名约定不能证明旧部署已更新，也不能证明新镜像已发布。 |
| 回滚来源 | 保留部署前实际验证过的旧镜像 digest，包括旧命名空间产物，不因更名删除。 |

新命名空间的私有包推送、目标机授权拉取和对应源码必须用首个真实产物验证。`manifest unknown`、匿名拒绝、workflow 配置为 active 或没有 Actions 记录，都不能证明目标镜像可部署；记录实际成功 pull 的 digest。`:master` 是移动标签，短 SHA 可用于查找，均不替代完整提交与 digest 的核对。

发布 workflow 是 `docker.yml`，触发为 `master` push、`v*` tag 或手动派发；测试 workflow 与 PR 检查见[测试指南](testing.md)。手动派发沿仓库支持的入口和当前授权执行，不能用 API 绕过权限。发布记录给出完整提交和 run 链接，不自动部署最新分支。

发布说明（Release 正文/公开公告）的正文先落成纯文本文件再提交，推送前按字节自检：完整提交与镜像 `digest` 存在、无 C0 控制字符（`[\x00-\x08\x0B\x0C\x0E-\x1F]`）、代码片段用反引号。PowerShell 的双引号字符串会把反引号加字母当成转义（反引号加 a 是 BEL），会把以反引号开头的摘要静默改成控制字符加残缺文本；写正文用单引号 here-string，并在写盘后复查。

## 发布准入与版本核对

由发布维护者固定提交，技术评审者审核条件，运维执行部署，测试做部署后冒烟。开始前必须具备：

- 该版本必要检查通过、独立技术审批与最终合并结论；合并提交或组合版本与被验版本对应。新提交不能沿用旧头结果。
- 对应发布 run 成功，应用镜像存在且目标机能拉取；记录 digest、完整源提交、部署前镜像和数据库迁移版本。
- 正确版本的 Compose 文件、配置与目录；部署前一致备份 create/verify 成功、回滚目标及恢复方法可用。
- 测试与部署窗口、失败责任人和证据目录明确，未完成任务已处理。目标机不能访问 Docker Hub 时不在其上构建，先确认所需数据库镜像已存在或有可用授权来源。

### 固定 Docker 与 Compose 目标

以下发布、回滚与恢复示例使用 Bash。先在专用终端初始化函数：固定已核 Docker context/配置目录、项目、Compose 文件和 env 文件，不继承 COMPOSE_FILE、DOCKER_HOST、APP_IMAGE 或数据路径等外部覆盖。`/srv/example/`、`SERVER_ADDRESS` 和尖括号内容均为占位示例，按实际部署登记替换，先确认相应父目录存在；初始化后若改变任一目标，重新核验，不能接续上次窗口。

```sh
set -euo pipefail
docker_context=default
docker_config="$HOME/.docker"
prod_dir=/srv/example/finance-quant
prod_project=finance-quant
prod_file="$prod_dir/docker-compose.yaml"
prod_env="$prod_dir/.env"
prod_storage="$prod_dir/storage"
prod_backups="$prod_dir/backups"
prod_restores="$prod_dir/restores"
prod_postgres="$prod_dir/postgres"
expected_db=finance_quant
expected_user=quant
dkr() {
  env -i PATH="$PATH" HOME="$HOME" docker --config "$docker_config" --context "$docker_context" "$@"
}
prod_compose() {
  dkr compose -p "$prod_project" --project-directory "$prod_dir" --env-file "$prod_env" -f "$prod_file" "$@"
}
```

先核**当前服务**，不停止或启动业务；`current_image` 填当前线上已核 digest。逐项核旧镜像、四个实际挂载、Compose 解析目标和数据库身份，全部成功后才进入候选产物/发布步骤。临时核配置的容器无网络和业务挂载，只接收配置并输出核验结论，不回显密码：

```sh
set -euo pipefail
current_image='ghcr.io/旧命名空间/应用@sha256:<当前已核digest>'
prod_compose config --quiet
api_id="$(prod_compose ps -q api)"
worker_id="$(prod_compose ps -q worker)"
db_id="$(prod_compose ps -q db)"
test -n "$api_id"
test -n "$worker_id"
test -n "$db_id"
current_id="$(dkr image inspect "$current_image" --format '{{.Id}}')"
for app_id in "$api_id" "$worker_id"; do
  test "$(dkr inspect "$app_id" --format '{{.Image}}')" = "$current_id"
  test "$(dkr inspect "$app_id" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')" = "$prod_file"
done
mount_source() {
  dkr inspect "$1" --format "{{range .Mounts}}{{if eq .Destination \"$2\"}}{{.Source}}{{end}}{{end}}"
}
for app_id in "$api_id" "$worker_id"; do
  test "$(mount_source "$app_id" /app/storage)" = "$prod_storage"
  test "$(mount_source "$app_id" /backups)" = "$prod_backups"
  test "$(mount_source "$app_id" /restores)" = "$prod_restores"
done
test "$(mount_source "$db_id" /var/lib/postgresql)" = "$prod_postgres"
compose_image() {
  prod_compose --profile tools config --format json | dkr run --rm -i --network none --entrypoint python "$current_image" -c 'import json, sys; print(json.load(sys.stdin)["services"][sys.argv[1]]["image"])' "$1"
}
configured_app="$(compose_image api)"
configured_db="$(compose_image db)"
test "$(dkr image inspect "$configured_app" --format '{{.Id}}')" = "$current_id"
test "$(dkr image inspect "$configured_db" --format '{{.Id}}')" = "$(dkr inspect "$db_id" --format '{{.Image}}')"
prod_compose --profile tools config --format json | dkr run --rm -i --network none --entrypoint python "$current_image" -c '
import json, sys
s = json.load(sys.stdin)["services"]
storage, backups, restores, postgres, db, user, image = sys.argv[1:]
expected = {"/app/storage": storage, "/backups": backups, "/restores": restores}
for name in ("api", "worker", "migrate", "maintenance"):
    service = s[name]
    assert service["image"] == image, "应用镜像配置不一致"
    assert {v["target"]: v["source"] for v in service["volumes"]} == expected, "应用挂载配置不符"
    env = service["environment"]
    assert (env["POSTGRES_HOST"], str(env["POSTGRES_PORT"]), env["POSTGRES_DB"], env["POSTGRES_USER"]) == ("db", "5432", db, user), "应用数据库目标不符"
assert {v["target"]: v["source"] for v in s["db"]["volumes"]} == {"/var/lib/postgresql": postgres}, "数据库挂载配置不符"
print("Compose 镜像、挂载和数据库目标已核")
' "$prod_storage" "$prod_backups" "$prod_restores" "$prod_postgres" "$expected_db" "$expected_user" "$configured_app"
actual_identity="$(prod_compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -X -w -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT current_database(), current_user"')"
test "$actual_identity" = "$expected_db|$expected_user"
printf '已核当前项目=%s；数据库=%s；镜像ID=%s\n' "$prod_project" "$expected_db" "$current_id"
```

`config --images api` 可能包含依赖链镜像并返回多行，不能当作 API 镜像单值；上面的函数从已解析 JSON 精确读取对应服务。`--profile tools` 保证 maintenance 也参与配置核验。

候选产物检查同样沿已固定的 `dkr`，不启动业务应用：

```sh
set -euo pipefail
release_image='ghcr.io/ot-workshop/furry_quant@sha256:<已核digest>'
dkr pull "$release_image"
dkr image inspect "$release_image" --format '{{.Id}} {{json .RepoDigests}}'
dkr image inspect "$release_image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
dkr run --rm --entrypoint python "$release_image" -c 'from finance_quant.provenance import source_provenance; import json; print(json.dumps(source_provenance()))'
```

镜像标签的 revision 应对应发布完整提交；缺少或冲突时核对构建证据，不猜测。可在同一提交的干净源码目录执行 `uv run python -c "from finance_quant.provenance import source_provenance; print(source_provenance())"`，对照 `code_sha256`、依赖锁哈希和 Python 版本。`code_sha256` 只覆盖计算层，排除了网页、持久化和部分进程入口，不能单独证明完整应用或前端相同；完整身份还需提交、构建 run 与镜像 digest。

记录当前运行 api/worker 的 image ID 与 digest，确认二者使用同一目标产物。只输出所需字段，不把完整环境或 Docker 配置打印到日志。拉取操作会写本机镜像缓存。

## 目录与配置

部署目录只需要：

```text
finance-quant/
├── docker-compose.yaml
├── .env
├── postgres/         # PostgreSQL 数据目录，由官方镜像管理
├── storage/          # 自动创建；支持相对路径或绝对路径
├── backups/          # 自动创建；支持相对路径或绝对路径
└── restores/         # 自动创建；独立恢复环境
```

`.env` 至少配置：

```dotenv
APP_IMAGE=ghcr.io/ot-workshop/furry_quant@sha256:实际已验digest
# 可选：只给 api 单独指定镜像；未设置时回落到 APP_IMAGE
# API_IMAGE=ghcr.io/ot-workshop/furry_quant@sha256:实际已验digest
TUSHARE_TOKEN=官方TushareToken
POSTGRES_DB=finance_quant
POSTGRES_USER=quant
POSTGRES_PASSWORD='独立强密码'
POSTGRES_DATA_ROOT=./postgres
STORAGE_ROOT=./storage
BACKUP_ROOT=./backups
RESTORE_ROOT=./restores
HTTP_PORT=8000
```

`.env` 权限设为 `600`。GHCR Token 不写入 `.env`，只用于 `docker login`。

PostgreSQL 使用 `POSTGRES_DATA_ROOT` 主机目录，默认 `./postgres`，目录权限由官方镜像管理。其余三个目录继续使用 `./storage`、`./backups`、`./restores`。Compose 默认以部署文件夹名作为项目名；同一主机运行多套实例时，使用不同文件夹名、端口和数据库目录。文件夹同名或需要自定义时，可设置 `COMPOSE_PROJECT_NAME`；不同项目名不会隔离指向同一路径的挂载。

## 私有镜像认证

创建 classic personal access token，只授予 `read:packages`。对应 GitHub 账户需要具备目标镜像包的读取权限；源码仓库公开不代表镜像包也公开。

```sh
read -s GHCR_TOKEN
echo "$GHCR_TOKEN" | docker login ghcr.io -u GitHub用户名 --password-stdin
unset GHCR_TOKEN
```

登录状态保存在部署用户的 Docker 配置中。不要用 root 登录后再以普通用户运行 Compose，否则两者使用不同的凭据文件。

## 首次启动

在部署目录检查配置并启动服务：

```sh
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps -a
```

首次启动由 PostgreSQL 官方镜像创建用户和数据库；账号、库名和密码来自 `.env`。数据库健康检查使用与应用相同的 `db` 网络地址进行密码认证并执行查询，只有最终数据库进程可以通过检查。不能使用 `127.0.0.1` 代替：官方镜像的回环认证规则可能为 `trust`，这会跳过密码校验。

`storage-init` 准备三个主机挂载根目录的属主，不递归修改业务文件。`migrate` 等待数据库健康与目录准备成功后执行迁移；API、worker 在迁移成功后启动。API、worker 和迁移均以非 root 用户运行。

正常状态为：`db`、`api` 健康，`worker` 运行，`storage-init`、`migrate` 以状态码0退出。一次性服务正常退出后无需保持运行。

验证应用及查看启动日志：

```sh
docker compose exec api python -m finance_quant.server check
docker compose logs --tail=100 db storage-init migrate api worker
```

准备或迁移失败会阻止依赖它们的应用服务启动。处理失败原因后，重新执行 `docker compose up -d`。重复启动会复用现有数据库和主机目录，迁移只应用尚未执行的版本。

## 更新镜像

首版流程仅适用于**现有 Compose 结构、数据库镜像、迁移定义、数据布局及运行身份兼容的应用镜像更新**。对照线上版本与最终完整候选提交的 Compose、Dockerfile、迁移及路径/检查实现，确认既有数据库已满足候选所需 schema，目录已按候选实际非 root 身份就绪，才可跳过执行 migrate/storage-init。首次部署或任一条件不满足时走单独验证的准备/迁移流程；不能先覆盖线上 Compose 再记录旧配置。

只换 api 之前先证明 worker 路径没有差异：

```sh
git diff --stat <旧提交> <新提交> -- src/finance_quant/research src/finance_quant/application src/finance_quant/server.py
```

输出为空（无改动）才可只换 api；否则必须连同 worker 一起更新。只换 api 时用 `API_IMAGE` 钉住 api 容器，`APP_IMAGE` 仍供 worker 与 migrate 使用；两个服务实际运行的 digest 要分别核对、分别记录。

`API_IMAGE` 的用法（Compose 里 api 行是 `image: ${API_IMAGE:-${APP_IMAGE:?Set APP_IMAGE in .env}}`）：

- **什么时候用**：只更新界面与接口（不改 worker/migrate）时在 `.env` 里设 `API_IMAGE=<已验 digest 或 sha-* 标签>`，worker 保持不动；
- **默认行为**：不设 `API_IMAGE` 时 api 与 worker 用同一个 `APP_IMAGE`；两者都为空时仍报 `Set APP_IMAGE in .env`，不会静默启动一个空镜像；
- **与迁移的关系**：含迁移的版本仍必须“先 `alembic upgrade head` 再换镜像”，不能用 `API_IMAGE` 绕过迁移；详见[含新迁移的版本](#含新迁移的版本)。

两条容易踩的坑：

- **显式 `-f docker-compose.yaml` 时，Compose 不会自动叠加 `docker-compose.override.yml`**。想让 override 生效，要么不加 `-f` 让 Compose 自己发现它，要么在命令行显式写成 `-f docker-compose.yaml -f docker-compose.override.yml`。不要假定“目录里有 override 就生效”，否则会以为改了 api 镜像、实际没改。
- 仓库里的 `docker-compose.yaml`/`.env` 与目标机上的实际文件要保持一致，差异只能是已登记的占位值。仓库改了而机器没改（或反之），下一次 `up -d` 就会默默回到旧行为。

### 含新迁移的版本

应用启动时会比较库的迁移版本与应用期望值（当前为 `0002_forward_registration`），**必须完全相等**，否则 api 拒绝启动并提示先迁移。因此包含新迁移的版本（例如引入前瞻登记两表的 `0002`）不能沿用只换 API 的兼容更新。

**推荐主路径：停 api/worker → 迁移 → 起新版**，即先 `docker compose stop -t 90 api worker`，再 `docker compose run --rm migrate`（或 `alembic upgrade head`），读 `alembic_version` 确认为目标头，最后起来候选镜像的服务并做只读冒烟 200。这样在迁移完成的瞬间不会留下一个“一重启就拒启”的旧 worker，也避免了“503 是不是故障”的误判。

如果必须缩短停机、选择“边跑边迁移再快速换 api”的方案，至少要写明并遵守：迁移完成到换完 api 的这段时间，旧容器**不得重启**（旧 worker 重启会在新库上被 `SchemaNotReady` 拒启，运行中的任务随之中断），并且依赖 `/health` 的监控对应时段按下线处理。

迁移完成后，只换 API 的路径仍然可用，但必须显式把更新工具的期望 schema 设为实际新头（不能再用默认的旧头）；更新工具本身不执行迁移，所以“迁移”与“换镜像”始终是两个独立步骤。

使用内部只换 API 的更新脚本（不在本仓库）时，期望 schema 用环境变量传入，例如 `EXPECT_SCHEMA=0002_forward_registration`；它与库里的 `alembic_version` 不一致时，脚本会在改动任何服务之前停住并返回非零（安全，不是故障），这时先补迁移或核对目标版本。

迁移后、换 api 前的窗口里，仍在运行的旧 api 的 `/api/v1/health` 会返回 **503**：这是旧的 schema 断言不再成立，不是故障，业务请求仍可处理、容器也不需要重启。不要据此回滚；依赖 `/health` 的监控或界面对应时段按下线处理，并尽量缩短迁移与替换的间隔。冒烟里把 503 记为“迁移版本与应用声明不一致”，不要当网络或进程故障排查。

换镜像本身不执行迁移：只换 API 的更新只有在候选镜像声明的迁移版本与库当前版本一致时才可用；两者不一致时先迁移再换。换镜像前后各记一次 `alembic_version` 与镜像声明的 `SCHEMA_REVISION` 入证据。

回滚：库已升到新头后，不都能“改回旧镜像”，三条路径的代价不同，需在窗口前选定一条并写进清单：

1. **降级** `alembic downgrade 0001_postgres`：删掉 `forward_registration` 与 `forward_attempt` 两张表。`0002` 只新增表与索引、未改既有表结构，因此仅在两张新表**无任何写入**时可接受；一旦已有登记或尝试记录，降级会丢用户数据，不得使用。
2. **可接受新头的补丁镜像**：保留旧业务代码、`SCHEMA_REVISION` 改为新头。需提前构建、审批与验证，不能临场拼。
3. **恢复到迁移前备份**（`0001`）后换回旧镜像：可行，但迁移后产生的数据全部丢失。

无论选哪条，迁移前先做一次一致备份并记录；将来若有迁移修改既有表结构，本节口径需重写，不能沿用。

**不要用整栈 `docker compose up -d` 回滚跳迁移的版本**：compose 的 `migrate` 服务会随 `up` 重建并执行旧镜像里的 `alembic upgrade head`，而库已停在更新的版本，旧代码里没有那个 revision，会报 `Can't locate revision identified by ...` 并非零退出；而 api/worker 依赖 `migrate` 成功，于是整栈起不来（比 503 更早、更容易误判成环境故障）。正确顺序是：先把库处理到目标镜像声明的版本（降级或恢复备份）→ 只替换目标服务（如 `docker compose ... up -d --no-deps --pull never api`）→ 冒烟 200。

失败处理：看到 `migrate` 容器非零退出且日志里有 `Can't locate revision`，含义是“镜像与库的迁移版本不匹配”；处置是回滚库版本或换回匹配镜像，不是重建数据库。

发布记录除原有字段外，一并写明本次的迁移头（或“未变”），不要靠事后推断。

先完成[发布准入与版本核对](#发布准入与版本核对)，确认没有运行中的任务；在同一专用 Bash 终端沿已核函数继续。以下是发布专用停写步骤，不用例行备份替代。任一步失败立即退出，保留现场并由负责人处理；新开终端重新初始化及核验目标。

```sh
set -euo pipefail
test "$(grep -c '^APP_IMAGE=' "$prod_env")" -eq 1
umask 077
release_state_base="$HOME/.local/state/finance-quant/releases"
mkdir -p -- "$release_state_base"
chmod 700 "$release_state_base"
release_state="$(mktemp -d "$release_state_base/release.XXXXXX")"
cp -- "$prod_file" "$release_state/compose.before.yaml"
cp -- "$prod_env" "$release_state/env.original"
cp -- "$release_state/env.original" "$release_state/env.before"
sed -i "s|^APP_IMAGE=.*|APP_IMAGE=$current_image|" "$release_state/env.before"
chmod 600 "$release_state/env.before"
printf '%s\n' "$docker_context" "$docker_config" "$prod_dir" "$prod_project" "$prod_file" "$prod_env" \
  "$prod_storage" "$prod_backups" "$prod_restores" "$prod_postgres" "$expected_db" "$expected_user" \
  "$current_image" "$current_id" > "$release_state/targets.txt"
prod_compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -X -w -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT version_num FROM alembic_version"' > "$release_state/schema.before"
test -s "$release_state/schema.before"
db_image_before="$(dkr inspect "$db_id" --format '{{.Image}}')"
test -n "$db_image_before"
printf '%s\n' "$db_image_before" > "$release_state/database-image.before"
(cd "$release_state" && sha256sum compose.before.yaml env.original env.before targets.txt schema.before database-image.before > checksums.sha256)
printf '发布前恢复记录=%s\n' "$release_state"
dkr pull "$release_image"  # 拉取失败时尚未停服务。
cmp -s "$prod_file" "$release_state/compose.before.yaml"
cmp -s "$prod_env" "$release_state/env.original"
active_migrations="$(dkr ps -q --filter "label=com.docker.compose.project=$prod_project" --filter label=com.docker.compose.service=migrate)"
test -z "$active_migrations"
before_compose() {
  dkr compose -p "$prod_project" --project-directory "$prod_dir" --env-file "$release_state/env.before" -f "$release_state/compose.before.yaml" "$@"
}
# 停写前确认保存配置能精确回到旧 digest，不以标签的当前镜像 ID 相同代替。
for service in api worker migrate maintenance; do
  saved_image="$(before_compose --profile tools config --format json | dkr run --rm -i --network none --entrypoint python "$current_image" -c 'import json, sys; print(json.load(sys.stdin)["services"][sys.argv[1]]["image"])' "$service")"
  test "$saved_image" = "$current_image"
done
backup_dir="/backups/predeploy-$(date -u +%Y%m%dT%H%M%SZ)"
before_compose stop -t 90 api worker
before_compose --profile tools run --rm --no-deps maintenance python -m finance_quant.backup create "$backup_dir"
before_compose --profile tools run --rm --no-deps maintenance python -m finance_quant.backup verify "$backup_dir"
printf '%s\n' "$backup_dir" > "$release_state/backup.txt"
# 校验成功后仍保持停写；此间不能重新启动旧 API/worker。
cmp -s "$prod_file" "$release_state/compose.before.yaml"
cmp -s "$prod_env" "$release_state/env.original"
sed -i "s|^APP_IMAGE=.*|APP_IMAGE=$release_image|" "$prod_env"
prod_compose config --quiet
for service in api worker maintenance; do
  parsed_image="$(compose_image "$service")"
  test "$parsed_image" = "$release_image"
done
parsed_db="$(compose_image db)"
parsed_db_id="$(dkr image inspect "$parsed_db" --format '{{.Id}}')"
online_db_id="$(dkr inspect "$db_id" --format '{{.Image}}')"
test "$parsed_db_id" = "$db_image_before"
test "$online_db_id" = "$db_image_before"
prod_compose --profile tools run --rm --no-deps maintenance python -m finance_quant.server check
# 沿相同候选镜像、默认用户及实际挂载验证，不加 --user 或启动依赖。
prod_compose --profile tools run --rm --no-deps -T maintenance python - <<'PY_PATH_PROBE'
import os
import tempfile
from pathlib import Path
from finance_quant import paths

assert os.geteuid() != 0, "候选必须以实际非 root 身份检查"
root, runs, data = paths.storage_root(), paths.runs_root(), paths.data_root()
quota = paths.quota_database()
required = (root, runs, runs / "tasks", data, quota.parent, root / "logs",
            Path(os.environ["BACKUP_ROOT"]), Path(os.environ["RESTORE_ROOT"]))
for directory in dict.fromkeys(required):
    assert directory.is_dir(), f"所需目录未就绪: {directory}"
    # 创建唯一临时文件、写入并同步，退出时删除；既有目录不等于可写。
    with tempfile.TemporaryFile(prefix=".release-write-probe-", dir=directory) as handle:
        handle.write(b"candidate-write-probe")
        handle.flush()
        os.fsync(handle.fileno())
if quota.exists():
    descriptor = os.open(quota, os.O_RDWR)  # 只核既有文件可读写，不改内容。
    os.close(descriptor)
print(f"候选路径写入检查通过: uid={os.geteuid()} gid={os.getegid()}")
PY_PATH_PROBE
active_migrations="$(dkr ps -q --filter "label=com.docker.compose.project=$prod_project" --filter label=com.docker.compose.service=migrate)"
test -z "$active_migrations"
actual_identity="$(prod_compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -X -w -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT current_database(), current_user"')"
test "$actual_identity" = "$expected_db|$expected_user"
actual_postgres_mount="$(mount_source "$db_id" /var/lib/postgresql)"
test "$actual_postgres_mount" = "$prod_postgres"
db_health="$(dkr inspect "$db_id" --format '{{.State.Health.Status}}')"
test "$db_health" = healthy
schema_ready="$(prod_compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -X -w -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT version_num FROM alembic_version"')"
test "$schema_ready" = "$(cat "$release_state/schema.before")"
: > "$release_state/switch-attempted"
prod_compose up -d --no-deps --pull never --wait --wait-timeout 180 api worker
prod_compose ps -a
```

数据库保持运行。停写前先核保存的旧配置：应用服务必须实际解析为 `current_image` 的精确 digest；可变标签即使当前 ID 相同也不能通过，包含后置 `export APP_IMAGE` 等覆盖时以实际解析结果为准。停写与备份使用这份配置；归档校验到新应用启动之间不恢复旧服务。候选检查 schema 与存储路径，不执行 migrate/storage-init。server check 可能创建缺失的运行目录，不能称为只读；其 mkdir(exist_ok=True) 也不能证明既有目录可写。探针以候选默认非 root 身份在所需目录创建、写入、同步并删除临时文件，会改变文件系统元数据；进程或主机异常时检查临时残留。先在独立环境验证这些副作用，生产仅在已批准停写窗口执行。候选 schema 检查失败（即使 revision 未变）或路径不可写均在应用启动前退出。

`--no-deps` 避免维护命令和应用启动协调或重建 DB 等依赖；仍须核解析数据库镜像与在线 DB 的实际 ID 一致。`--wait` 等待应用健康，`--pull never` 使用已核镜像。

恢复记录在停写前保存原 Compose、固定旧 digest 的配置、实际目标及 schema。目录位于本机用户私有 state，放在仓库和应用数据目录之外；`env.before` 含本机凭据，不能贴到消息、PR、日志或附件，也不进入应用数据备份。保留记录至本批部署与回滚窗口结束，故障时据打印的实际路径进入下节，不依赖损坏的新配置或在线应用。

备份、校验或切换失败时，本脚本不会自动重开旧服务；记录失败发生在修改 APP_IMAGE 或启动应用之前还是之后，由负责人核实兼容性后恢复服务或进入[回滚](#回滚)。

### 部署后核验

```sh
prod_compose ps -a
curl -fsS http://127.0.0.1:8000/api/v1/health
prod_compose --profile tools run --rm --no-deps maintenance python -m finance_quant.server check
prod_compose exec -T api python -c 'from finance_quant.provenance import source_provenance; print(source_provenance())'
prod_compose exec -T worker python -c 'from finance_quant.provenance import source_provenance; print(source_provenance())'
```

端口按实际配置替换。核对 api/worker 容器 image ID 与目标镜像、计算与依赖哈希，以及 health 的数据库 revision；健康检查的 revision 是数据库迁移版本，不是 Git 提交。

测试负责人在约定窗口执行线上冒烟：原有回测/评估/评分数量与部署前对照、`/api/v1/data-status` 的原数据版本未意外变化、既有回测 KPI 与 API 一致、研究/比较入口正常。财务兼容修复须打开 `/evaluations/<原运行ID>` 并对照详情 API 与 JSON 下载，不能仅检查评估列表；无原始观测的 IC/分组收益仍为空，冻结文件不变。新增临时研究在隔离环境完成，生产写入另按明确验收范围执行。页面无法访问时记录未验项，不以 HTTP 或组件测试冒充浏览器验收。

将部署提交、发布 run、镜像 digest、备份校验、迁移、版本核对与冒烟结论写入发布记录。镜像 digest 按服务分别记：api 与 worker 可能因 `API_IMAGE` 而不一致，要写清各自实际运行的那一份，并注明判断“能否只换 api”的依据。检查失败时由执行人保留日志，发布维护者与技术评审者判断继续修复或回滚，不能只重启后宣称通过。

### 更新后继续未完成下载

保留 `.env` 中的数据库配置和 `POSTGRES_DATA_ROOT`、`STORAGE_ROOT` 路径；不要清理数据库、`storage`、原始响应或下载计划。更新应用只需改 `APP_IMAGE`，使用 GitHub Actions 已成功发布且包含目标修复的标签或 digest。仅修改本地代码、重启旧镜像或拉取同一个固定旧标签不会获得修复。

容器健康后，打开“数据管理 → 统一更新”，在原任务的“统一更新记录”中点击“续传”。单独创建的行情任务在“行情下载 → 下载记录”中续传。程序复用原计划、完成断点和接口缓存，保留成功的数据与研究结果；校验规则更新后，未完成候选会用新规则重新核验，必要时补查缺失历史。

续传后展开记录，检查剩余待处理股票及自动处理明细。存在来源冲突或缺少历史证据时，任务仍可能显示部分完成；不要通过删除股票或清空数据来绕过复核。

## Compose 结构变化

上述兼容镜像更新不替换线上 Compose。服务、数据库镜像、volume、端口或环境变量结构变化须转到单独方案：先保存实际旧文件与配置，再在隔离环境核新旧启动、数据/迁移和故障恢复，形成对应切换步骤后执行。可以准备候选文件，但不能按独立 `mv` 步骤先覆盖线上文件，再进入本节的旧配置记录。

## 回滚

发布失败后，API/worker 可能已停止或只启动一部分，**不要重跑要求健康应用在线的发布预检**。使用本次发布前保存的恢复记录，允许应用容器缺席或镜像混合；必须核固定 Docker 目标、仍在线的数据库及业务挂载，且没有正在执行的 migrate。下列故障态入口不读取损坏的新 `.env` 或新 Compose：

```sh
set -euo pipefail
release_state="$HOME/.local/state/finance-quant/releases/release.<本次记录>"
(cd "$release_state" && sha256sum --check --status checksums.sha256)
mapfile -t saved < "$release_state/targets.txt"
test "${#saved[@]}" -eq 14
docker_context="${saved[0]}"; docker_config="${saved[1]}"
prod_dir="${saved[2]}"; prod_project="${saved[3]}"; prod_file="${saved[4]}"; prod_env="${saved[5]}"
prod_storage="${saved[6]}"; prod_backups="${saved[7]}"; prod_restores="${saved[8]}"; prod_postgres="${saved[9]}"
expected_db="${saved[10]}"; expected_user="${saved[11]}"; current_image="${saved[12]}"; current_id="${saved[13]}"
dkr() { env -i PATH="$PATH" HOME="$HOME" docker --config "$docker_config" --context "$docker_context" "$@"; }
prod_compose() {
  dkr compose -p "$prod_project" --project-directory "$prod_dir" --env-file "$prod_env" -f "$prod_file" "$@"
}
saved_compose() {
  dkr compose -p "$prod_project" --project-directory "$prod_dir" --env-file "$release_state/env.before" -f "$release_state/compose.before.yaml" "$@"
}
mount_source() { dkr inspect "$1" --format "{{range .Mounts}}{{if eq .Destination \"$2\"}}{{.Source}}{{end}}{{end}}"; }
assert_read_equals() {
  local expected_output actual_output
  expected_output="$1"; shift
  actual_output="$("$@")" || return $?
  test "$actual_output" = "$expected_output"
}
verify_fault_target() {
  local active_migrations app_ids app_id service expected_db_image
  active_migrations="$(dkr ps -q --filter "label=com.docker.compose.project=$prod_project" --filter label=com.docker.compose.service=migrate)" || return $?
  test -z "$active_migrations" || return 1
  db_id="$(saved_compose ps -q db)" || return $?
  test -n "$db_id" || return 1
  expected_db_image="$(cat "$release_state/database-image.before")" || return $?
  test -n "$expected_db_image" || return 1
  assert_read_equals healthy dkr inspect "$db_id" --format '{{.State.Health.Status}}' || return $?
  assert_read_equals "$expected_db_image" dkr inspect "$db_id" --format '{{.Image}}' || return $?
  assert_read_equals "$prod_postgres" mount_source "$db_id" /var/lib/postgresql || return $?
  for service in api worker; do
    app_ids="$(dkr ps -a -q --filter "label=com.docker.compose.project=$prod_project" --filter "label=com.docker.compose.service=$service")" || return $?
    for app_id in $app_ids; do
      assert_read_equals "$prod_storage" mount_source "$app_id" /app/storage || return $?
      assert_read_equals "$prod_backups" mount_source "$app_id" /backups || return $?
      assert_read_equals "$prod_restores" mount_source "$app_id" /restores || return $?
    done
  done
  actual_identity="$(saved_compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -X -w -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT current_database(), current_user"')" || return $?
  test "$actual_identity" = "$expected_db|$expected_user" || return 1
  schema_now="$(saved_compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -X -w -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT version_num FROM alembic_version"')" || return $?
  test -n "$schema_now"
}
saved_compose config --quiet
verify_fault_target
printf '发布前schema=%s；当前schema=%s；旧镜像=%s\n' "$(cat "$release_state/schema.before")" "$schema_now" "$current_image"
```

由技术评审者与发布负责人根据**实际失败阶段和迁移状态**记录回退结论。`schema` 相同不能证明部分迁移未改结构或数据；发生过迁移尝试时尤其不能仅据版本号相等切回。只有确认当前结构/数据可由旧镜像读取，才在恢复记录内保存 `image-rollback-reviewed.txt`，包含 `mode=image`、`schema=<当前实际版本>`、`image=<记录内旧digest>` 和非空 `decision=<本次兼容结论及依据>`。否则按下一段独立备份恢复，不能创建空文件绕过判断。

以下是**已确认可直接回镜像**的切换；继续沿刚核的故障态函数执行，应用已停不会阻断，部分已启动的应用会先停止。数据库版本必须回到发布前值，且恢复后再次确认 Compose 实际解析到旧 digest，才允许启动：

```sh
set -euo pipefail
decision="$release_state/image-rollback-reviewed.txt"
grep -Fx 'mode=image' "$decision" >/dev/null
grep -Fx "image=$current_image" "$decision" >/dev/null
grep -E '^decision=.+' "$decision" >/dev/null
if cached_id="$(dkr image inspect "$current_image" --format '{{.Id}}' 2> "$release_state/image-inspect.stderr")"; then
  test "$cached_id" = "$current_id"
else
  # 只有明确缺图时获取；引擎或权限错误不能当作缺缓存。
  grep -Fq 'No such image' "$release_state/image-inspect.stderr"
  dkr pull "$current_image"
  cached_id="$(dkr image inspect "$current_image" --format '{{.Id}}')"
  test "$cached_id" = "$current_id"
fi
saved_compose stop -t 90 worker api
# 人工判断之后、替换配置之前，重新读取实际状态并匹配本次判断。
verify_fault_target
test "$schema_now" = "$(cat "$release_state/schema.before")"
grep -Fx "schema=$schema_now" "$decision" >/dev/null
cp -- "$release_state/compose.before.yaml" "$prod_file"
cp -- "$release_state/env.before" "$prod_env"
chmod 600 "$prod_env"
cmp -s "$release_state/compose.before.yaml" "$prod_file"
cmp -s "$release_state/env.before" "$prod_env"
prod_compose config --quiet
compose_image() {
  prod_compose --profile tools config --format json | dkr run --rm -i --network none --entrypoint python "$current_image" -c 'import json, sys; print(json.load(sys.stdin)["services"][sys.argv[1]]["image"])' "$1"
}
for service in api worker migrate maintenance; do
  parsed_image="$(compose_image "$service")"
  test "$parsed_image" = "$current_image"
done
parsed_db="$(compose_image db)"
parsed_db_id="$(dkr image inspect "$parsed_db" --format '{{.Id}}')"
online_db_id="$(dkr inspect "$db_id" --format '{{.Image}}')"
test "$parsed_db_id" = "$(cat "$release_state/database-image.before")"
test "$online_db_id" = "$parsed_db_id"
assert_read_equals "$prod_postgres" mount_source "$db_id" /var/lib/postgresql
prod_compose up -d --no-deps --pull never --wait --wait-timeout 180 api worker
```

旧应用的精确 digest 与 ID 命中本机时不访问仓库；缺失才获取，其他缓存核验错误立即停止。审批期间的迁移、库身份、schema 或 DB 镜像变化会在替换前被拒绝。解析出的数据库标签若已指向别的本地镜像，也不能继续；`--no-deps` 额外保证本步骤不协调或重建数据库依赖。

镜像回滚不会自动回滚数据库。存在迁移而未证明向后兼容时，不只切旧镜像；使用部署前已校验备份，按[恢复](#恢复)在独立空数据库和空存储中恢复、验证，再由负责人切换访问。不要先 migrate 空目标，也不要在现有业务目录上覆盖恢复。迁移应采用向前兼容的扩展/收缩方式；删除列、修改字段类型等破坏性操作拆分到后续版本。

回滚后再次核版本、health、server check、既有结果与页面；记录原因、恢复点与未恢复的新数据范围。旧环境和失败现场先保留以供排查，不能把重新建库当作无损回滚。

## 日志与状态

```sh
docker compose --env-file .env -f docker-compose.yaml ps
docker compose --env-file .env -f docker-compose.yaml logs -f api worker
docker compose --env-file .env -f docker-compose.yaml logs --tail=100 db
```

任务子进程日志位于 `STORAGE_ROOT/logs/`。网页关闭或 SSH 隧道断开不会停止服务器任务。

只重启应用：

```sh
docker compose --env-file .env -f docker-compose.yaml restart api worker
```

`docker compose down` 停止并删除本项目容器和网络，主机上的数据库、业务、备份和恢复目录继续保留。重建试运行数据库见下文。

### 任务失败与内存判读

任务失败时先看任务中心行内的 `原因：…`（含阶段与退出码），再看 `STORAGE_ROOT/logs/<任务 id>.log` 里的子进程输出。退出码语义见[前端结构与接口](frontend-contract.md#任务失败原因与退出码)；**没有 cgroup 证据时不要写“内存不足”**。

可能吃内存的长任务（长区间回测、多因子、方案比较、完整备份）在开始前与结束后各读一次 worker 容器的 cgroup 计数：

```sh
c=$(docker inspect --format '{{.Id}}' finance-quant-worker-1 | tr -d '-')
d=/sys/fs/cgroup/system.slice/docker-$c.scope   # 具体路径以宿主为准
cat $d/memory.current $d/memory.peak $d/memory.max
cat $d/memory.events
cat $d/memory.stat | grep -E '^(anon|file|file_dirty|file_writeback) '
```

判读规则：

- `memory.current` 包含页缓存。数值高时要看 `memory.stat` 的 `anon` 与 `file`：`file` 是内核可回收的缓存，`anon` 才是进程真实占用；`file_dirty` / `file_writeback` 大量未落盘时也不宜同时跑重任务。
- `memory.peak` 是 cgroup 生命周期内的历史最大值，不是本次任务的峰值：跑完比基准大只能说“至少用到了这个值”，没变时要记“未超过此前峰值”，**不能记成 0**。需要本次真实峰值就低频（如每 5 秒）采样 `memory.current` 取最大。
- `memory.events` 的 `oom_kill` 增量为 0 时，不得把失败归因为内存不足；`docker stats` 读数或“容器仍在运行”也不能单独证明原因。
- 容器重建会重置这些计数，两次读数只能比较同一 cgroup 生命周期；同时记下容器 id、`StartedAt`、镜像 id 与两次读数时的 `queued/running`。

## Windows 访问

API 只发布到服务器回环地址。Windows PowerShell 建立隧道：

```powershell
ssh -N -L 8000:127.0.0.1:8000 用户名@服务器地址
```

浏览器打开 <http://127.0.0.1:8000>。隧道只影响浏览器访问，不影响后台任务。

直接通过局域网访问时，将 `HTTP_BIND_HOST` 设置为服务器地址，并在 `ALLOWED_ORIGINS` 中加入完整网页地址。例如 `HTTP_BIND_HOST=0.0.0.0`（监听所有网卡）、`ALLOWED_ORIGINS=http://SERVER_ADDRESS:8000`，执行 `docker compose up -d` 后即可从其他电脑访问。监听地址决定端口是否对外可达，来源配置决定网页能否提交研究和数据管理操作；两项应配套设置。

## 备份

网页“数据管理 → 备份与恢复”提供创建、校验和恢复入口，风格与其他数据页面一致。后台任务与行情更新共用串行 worker，备份执行期间阻止 API 写入；无需在网页操作时停掉 worker。下列命令行方式则需要先停止 API 与 worker。

创建备份**不做跨请求去重**：每次提交都会新建一个任务与一个归档，重复点击会在串行 worker 里排队执行多次完整备份，占用相应时间与磁盘。需要单份归档时，先确认前一条已完成再提交；窗口内不要把重复备份当成“同一条任务”。

下面是**例行备份**：完成后恢复原服务，不用于发布停写窗口。发布时使用上文[更新镜像](#更新镜像)的专用顺序。停止 API 与 worker 后执行；数据库保持运行。备份命令会检查 worker 互斥锁并阻止 API 写入，不会仅凭“当前没有运行中的任务”判断一致性：

```sh
backup_dir="/backups/backup-$(date -u +%Y%m%dT%H%M%SZ)"
docker compose stop -t 90 api worker || exit 1
if ! docker compose --profile tools run --rm maintenance python -m finance_quant.backup create "$backup_dir" \
   || ! docker compose --profile tools run --rm maintenance python -m finance_quant.backup verify "$backup_dir"; then
  docker compose up -d api worker
  printf '备份或校验失败，停止发布；核对原服务恢复状态。\n' >&2
  exit 1
fi
docker compose up -d api worker
```

记录本次 `backup_dir`；下文恢复示例的 `backup-20261112` 必须替换为实际已校验归档，不能复用不存在的示例日期。备份失败分支只尝试恢复旧服务，仍需核其健康状态，不视为发布成功。

备份包括：

- PostgreSQL 自定义格式 dump。
- 整个 STORAGE_ROOT：共享行情、原始响应、财务观测与不可变版本、历史行业、基准、下载检查点、冻结数据集、研究结果、日志与配额记录。
- 每个文件及归档的 SHA-256 清单；运行中的锁文件不备份。

`.env` 不进入备份，凭据应另外保管。所有数据位置必须位于 STORAGE_ROOT 内；遇到目录外的覆盖配置时命令会说明情况并停止，不会擅自移动已有数据。备份目标必须在 STORAGE_ROOT 之外，且不存在。完整归档需要足够磁盘空间。Docker volume 是持久化手段，不是独立备份。

原生开发环境也使用相同命令：

```powershell
uv run python -m finance_quant.backup create D:/quant-backups/backup-20261112 --env-file .env.development
uv run python -m finance_quant.backup verify D:/quant-backups/backup-20261112
```

需安装与服务器主版本相同或更高的 PostgreSQL 客户端，并将其 bin 加入 PATH；也可设置 `POSTGRES_BIN`，例如 `D:/Program Files/PostgreSQL/18/bin`。应用容器已经包含 PostgreSQL 18 的备份客户端。

## 恢复

网页恢复填写新环境名称，数据库强制使用 `fq_restore_` 前缀，文件存入 `RESTORE_ROOT/<数据库名>`；确认窗口展示目标位置。后台先校验归档，再创建不存在的目标库，仅在空库和空目录上恢复。已有数据的目标会被拒绝，当前应用连接不会切换。

若当前数据库账号没有 CREATEDB 权限，可由管理员预建同名空库，或在环境配置 `RESTORE_POSTGRES_USER` 与 `RESTORE_POSTGRES_PASSWORD`，然后重启应用。凭据只在服务器使用，不发送到网页。使用 Compose 时，备份与恢复目录由 storage-init 自动准备写权限。

回滚恢复与演练使用独立项目、数据库、回环端口和四个真实挂载目录。**先把已校验归档复制到独立 BACKUP_ROOT**，不要直接挂载生产备份目录：Compose 的 storage-init 会修改挂载根目录的所有者，即使只运行恢复工具也会触发。

在单独的 Bash 终端先执行[固定 Docker 目标的函数定义](#固定-docker-与-compose-目标)，只复用清除外部覆盖且固定 context/config 的 `dkr`，不执行生产核验或切换。替换 `verified_archive` 为实际已校验归档；以下不启动任何服务，也不复制生产 `.env`：

```sh
set -eu
verified_archive='/srv/example/finance-quant/backups/<已校验归档>'
verified_compose='/已核旧版本/docker-compose.yaml'
test -d "$verified_archive"
test ! -L "$verified_archive"
test -f "$verified_compose"
drill_root="$(mktemp -d /srv/example/fq-restore.XXXXXX)"
drill_project="$(basename "$drill_root" | tr '[:upper:]' '[:lower:]' | tr '.' '-')"
mkdir "$drill_root/postgres" "$drill_root/storage" "$drill_root/backups" "$drill_root/restores"
cp -- "$verified_compose" "$drill_root/docker-compose.yaml"
: > "$drill_root/.env"
chmod 600 "$drill_root/.env"
cp -a -- "$verified_archive" "$drill_root/backups/input"
printf '恢复项目=%s；独立目录=%s\n' "$drill_project" "$drill_root"
```

用备份对应的已验证旧镜像及 Compose 版本。启动前在空白 `.env` 中只填写下列独立配置，将“唯一目录”全部替换为刚生成的 `drill_root` 绝对路径；使用恢复项目专用账号/密码，不复制生产专用设置。项目、端口与四个数据路径须独立：

```dotenv
APP_IMAGE=ghcr.io/旧命名空间/应用@sha256:备份对应的已验digest
COMPOSE_PROJECT_NAME=刚生成的drill_project
POSTGRES_IMAGE=postgres:18.6-bookworm
POSTGRES_DB=finance_quant_restore_test
POSTGRES_USER=quant_restore
POSTGRES_PASSWORD='仅在本机填写恢复项目专用密码'
POSTGRES_DATA_ROOT=/data/唯一目录/postgres
STORAGE_ROOT=/data/唯一目录/storage
BACKUP_ROOT=/data/唯一目录/backups
RESTORE_ROOT=/data/唯一目录/restores
HTTP_BIND_HOST=127.0.0.1
HTTP_PORT=18081
ALLOWED_ORIGINS=http://127.0.0.1:18081
```

核对四个目录都是真实独立路径，不是生产目录、指向生产的链接或复用挂载；备份副本与源归档内容一致。端口须空闲，旧应用和数据库镜像已可用。所有命令显式使用同一随机项目，外部 COMPOSE_PROJECT_NAME 不会覆盖；在保留 `drill_root`、`drill_project` 的同一终端继续：

```sh
set -euo pipefail
drill_compose() {
  dkr compose -p "$drill_project" --project-directory "$drill_root" --env-file "$drill_root/.env" -f "$drill_root/docker-compose.yaml" "$@"
}
drill_compose config --quiet
drill_compose up -d --pull never db storage-init
drill_compose --profile tools run --rm maintenance python -m finance_quant.backup verify /backups/input
drill_compose --profile tools run --rm -e STORAGE_ROOT=/restores/recovered \
  maintenance python -m finance_quant.backup restore /backups/input
```

恢复目标必须是全新空数据库和空存储目录，不先运行 migrate。CLI 的目标为**可写宿主父目录下的独立子目录** `/restores/recovered`，而非挂载点本身，不提高容器用户权限。

恢复进程按固定顺序执行：先校验归档完整性，再对恢复目标做只读检查（父目录可写、目标不是挂载点、不是符号链接、不存在或为空），这些检查失败时不会打开数据库、不会删除或创建任何目录；检查通过后才建立离线锁、确认实际数据库身份和目标库为空，随后才移除已存在的空目标并由当前用户重建、创建私有暂存目录，最后解包文件并执行 `pg_restore`。挂载点、父目录不可写、非空目标、无法移除的已有空目录或数据库非空都会在写库前拒绝，已有空目录无法移除时保持原样。不要用 `--user 0:0` 恢复，否则文件归 root，后续服务用户无法写入。

恢复成功后保留同一随机项目和数据库，把副本 `.env` 的 `STORAGE_ROOT` 改为刚生成的 `<drill_root>/restores/recovered`，其余目录仍在本次独立根内；使该目录挂载为 `/app/storage`，再启动同版本旧镜像并检查同一运行：

```sh
set -eu
drill_compose --profile tools run --rm maintenance python -m finance_quant.server check
drill_compose up -d --pull never --wait --wait-timeout 180 api worker
curl -fsS http://127.0.0.1:18081/api/v1/health
```

`server check` 只检查表结构和服务可用；还须从该隔离端口读回相同 `run_id`、`dataset_id`，核对冻结输入、结果和报告哈希及旧镜像身份，全部符合后再由负责人考虑访问切换。

**仅在演练已结束、未切流且不再承担服务时**，保存证据后执行 `drill_compose down --volumes`，只清本次演练项目；主机目录另行核对保留/清理。若用于真实回滚并已切换访问，保留恢复后的目标实例、数据目录和旧现场，不能执行这条清理；按实际发布方案维护与后续退出。失败现场也先保留并报告残留，不自动把演练实例切成生产。

恢复不会执行清库或覆盖已有文件。原排队、运行中任务改为中断状态，自动更新计划关闭，确认数据后再手动续传或开启计划。已完成任务、数据版本及结果保留。发布前检查失败不会写入数据库或目标目录；已有空目标无法移除时保持原样，不同级其他挂载不会阻断合法子目录。不会清理原环境。自定义目录布局可在备份 manifest 的 layout 中查看，并在恢复环境配置相同的相对布局。

## 统一数据更新

网页“数据管理 → 统一更新”可设置股票范围、起止日期、行情预热和修订回看长度，并选择财务、申万行业级别和基准。也可关闭行情更新，仅选择已发布行情版本更新对应股票的财务和基准。

任务按“行情与参考数据 → 财务 → 行业 → 基准”串行执行。行情未完整发布时，对应财务和基准步骤显示等待依赖，独立行业步骤仍可执行。各类版本独立发布，报告逐项状态与版本，不把部分成功称为整套成功。续传复用已完成的步骤和报表/行业分区缓存，旧版本不删除。

自动计划使用北京时间，每天到设定小时后入队一次；worker 每分钟检查。午夜至17点的计划使用前一日作为数据截止日，18点后的计划使用当日。worker 不在线时不执行，恢复在线后补入当日到期任务；不自动堆积历史日期的任务。多个数据源仍使用原有配额控制，BaoStock保持串行。

## 隔离测试

完整备份恢复演练使用 [测试指南中的随机项目代码块](testing.md#compose-备份恢复与启动)，全部 up/logs/down 带同一个显式 `-p`，保存测试退出码后清理对应项目，不依赖外部环境可覆盖的默认项目名。无生产主机目录挂载或数据库端口暴露。

原生测试必须显式指定测试数据库和临时存储，数据库名为 fq_* 或 *_test；夹具拒绝开发库配置。备份恢复测试在测试服务器中另建一次性数据库，并核验实际连接。测试 Compose 固定 command 只覆盖备份恢复及相关更新测试，不代表整个 PostgreSQL 集成目录；UI 临时回测与清理也见[测试指南](testing.md)。

## 磁盘与清理

定期检查：

```sh
df -h
docker system df
du -sh storage backups
```

不得手动删除 `datasets/` 或 `runs/` 中仍被 PostgreSQL 登记的文件。Docker 构建缓存可清理，但数据库 volume、正式镜像和备份必须先确认引用关系。

临时演练只清理登记过的项目、数据库、目录和镜像；先保存日志/哈希，再停服务、断开连接后清理。保留回滚 digest、部署前备份及失败现场直到负责人确认验收完成。清理失败要列出残留与责任人，不执行全机清理以掩盖问题。发布与回滚执行人负责资源闭环，测试报告冒烟结论，文档维护者记录实际版本与变化。

## 试运行环境重建

官方镜像的初始化参数只对空数据库目录生效。修改 `.env` 中的密码不会改变已有数据库密码，修改库名也不会迁移原库内容。已有业务数据的环境应保留原凭据，或执行明确的账号变更与数据迁移。

确认本项目试运行数据库可丢弃时，可在对应部署目录执行：

```sh
docker compose down
mv postgres "postgres.previous-$(date +%Y%m%d-%H%M%S)"
docker compose up -d
```

以上示例使用默认 `./postgres` 路径：先停止服务，将旧数据库目录移开，再按当前 `.env` 初始化新的空目录。自定义 `POSTGRES_DATA_ROOT` 时，应操作实际配置的目录。storage、backups 和 restores 保留；保留的业务文件不会自动注册为新实例的研究结果。

旧版曾使用的 `finance-quant_postgres` 命名卷不会自动转为主机目录。需要保留旧库时应先备份，再恢复至独立环境；可丢弃的试运行库可直接切换到新的空目录，确认启动成功后再清理旧命名卷。

## 启动流程验证

在 Linux 上使用本地应用镜像验证首次启动、主机目录写权限、重复启动、升级迁移、迁移失败阻断以及旧卷密码不匹配：

```sh
docker build --target application -t finance-quant:startup-check .
python3 scripts/check_compose_startup.py --image finance-quant:startup-check
```

每个场景使用随机 Compose 项目，只挂载脚本创建的临时数据库和业务目录，结束后清理测试资源。GitHub Actions 在发布应用镜像前执行同一组验证。
