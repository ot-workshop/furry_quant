# 开发数据库测试事故记录

日期：2026-11-12。数据库：本机 `.env.development` 配置的 `finance_quant_dev`。

## 已确认的经过与影响

执行人：本次 Codex 修复任务。原计划创建临时数据库，但开发账号没有 CREATEDB 权限。随后尝试创建临时 schema，并通过 PGOPTIONS 指定 search_path；应用 make_engine 显式传入 options=-c timezone=UTC，覆盖了该环境配置。没有先断言 current_schema 就运行了旧集成测试，这是执行错误。

旧集成测试包含 TRUNCATE，实际清空了 public 下的：run_artifact、run_trade、run_equity、run_metrics、research_run、dataset、update_partition、research_task。事故已在对话中告知用户。原记录数量没有事前快照，不能声称已完整恢复。

测试留下的 integration-evaluation 与 dataset-integration 已按精确 id、名称、关联和路径核对后删除；没有据旧文件伪造数据库记录。

## 现存数据

- 行情库 daily_bar、adjustment、valuation 各 192477 行；index_member 6900 行；data_release 1014 行；data_revision 780110 行。上述表不在 TRUNCATE 列表中。
- 旧 runs 目录仍有 121 个 result.json；storage-dev/runs 下仍有 9211 个文件（含因子历史、持仓、评估报告、Qlib中间数据等）。磁盘文件未被本次事故删除。
- 财务、行业、行情版本及冻结数据集文件仍在磁盘。冻结数据集的数据库登记可通过现有注册流程重新建立，但这不等于恢复原来的任务和回测账本。
- 项目目录内未找到 .dump、.backup 或匹配 backup 的 SQL 备份。没有执行覆盖式恢复或擅自重新计算替代旧结果。

## 已完成的防复发修复

集成测试移除了 TRUNCATE。每项测试自行创建随机独立 schema，用显式连接参数同时设置 timezone 和 search_path；在运行任何迁移或写入前断言 current_schema 精确匹配该随机 schema。Alembic 与待测仓库使用同一连接配置；结束后仅清理测试创建的 schema。

已在该隔离机制下验证 PostgreSQL 迁移、任务队列、结果存储，以及财务数据→入队→Qlib→评分/回测/评估结果读取的完整链路。外层核对 public 的研究、任务、数据集、更新分区表数量，运行前后保持一致。

## 未完成

开发数据库历史任务、完整回测净值/成交等仅存于 SQL 的记录尚未恢复。若存在项目目录之外的 PostgreSQL备份、系统快照或WAL归档，应另行核验并制定恢复步骤；不得把磁盘中部分衍生文件称为完整备份。远端 Linux 服务未因本次测试而被修改。
