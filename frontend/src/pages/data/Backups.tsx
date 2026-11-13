import { useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Flex,
  Form,
  Input,
  Select,
  Space,
  Table,
  Typography,
} from "antd";
import { CloudDownloadOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  dataApi,
  type BackupItem,
  type BackupOperation,
  type MaintenanceTask,
} from "../../api/data";
import { describeError } from "../../api/http";
import { Resource, Section, StatusTag } from "../../components/ui";
import { useApiResource } from "../../hooks/useApiResource";
import { formatDateTime, formatNumber } from "../../utils/format";

const ACTIONS: Record<string, string> = {
  create: "创建备份",
  verify: "校验备份",
  restore: "恢复至独立环境",
};

export default function Backups() {
  const { message, modal } = App.useApp();
  const status = useApiResource((signal) => dataApi.backups(signal), [], {
    pollMs: 3000,
  });
  const [selected, setSelected] = useState("");
  const [name, setName] = useState(() => "research_" + Date.now().toString(16));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending =
    status.data?.jobs.some((job) =>
      ["queued", "running"].includes(job.status),
    ) ?? false;
  const backupId = selected || status.data?.backups[0]?.id;
  const database = "fq_restore_" + name;
  async function submit(operation: BackupOperation) {
    setBusy(true);
    setError(null);
    try {
      await dataApi.backupOperation(operation);
      status.reload();
      message.success("操作已加入后台队列");
    } catch (e) {
      setError(describeError(e));
      throw e;
    } finally {
      setBusy(false);
    }
  }
  function restore() {
    if (!backupId) return;
    modal.confirm({
      title: "确认恢复到新环境",
      okText: "确认恢复",
      cancelText: "取消",
      content: (
        <Flex vertical gap={8}>
          <Typography.Text>备份：{backupId}</Typography.Text>
          <Typography.Text>目标数据库：{database}</Typography.Text>
          <Typography.Text>
            目标目录：{status.data?.restore_root}/{database}
          </Typography.Text>
          <Typography.Text>
            仅允许空数据库和空目录。已有数据会被拒绝；当前环境不会切换或覆盖，恢复后自动更新关闭。
          </Typography.Text>
        </Flex>
      ),
      onOk: () => submit({ action: "restore", backup_id: backupId, database }),
    });
  }
  return (
    <Flex vertical gap={16}>
      <Alert
        type="info"
        showIcon
        title="完整备份数据库与文件数据"
        description="保留行情、财务、行业版本及下载检查点。备份交给同一后台 worker 串行执行，期间暂停新的写入请求；已有数据不删除。"
      />
      {error ? <Alert type="error" showIcon title={error} /> : null}
      <div className="with-aside">
        <Card title="创建与校验">
          <Flex vertical gap={12}>
            <Typography.Text>
              {status.data?.coverage.join("、")}
            </Typography.Text>
            <Typography.Text type="secondary">
              备份目录：{status.data?.backup_root ?? "加载中"}
              。配置凭据不进入备份。
            </Typography.Text>
            <Space wrap>
              <Button
                type="primary"
                icon={<CloudDownloadOutlined />}
                loading={busy}
                disabled={pending}
                onClick={() =>
                  void submit({ action: "create" }).catch(() => {})
                }
              >
                创建完整备份
              </Button>
              <Button icon={<ReloadOutlined />} onClick={status.reload}>
                刷新状态
              </Button>
            </Space>
            <Typography.Text type="secondary">
              已有任务先执行，备份随后运行。页面关闭不影响任务；备份完成后可在下表选择并校验。
            </Typography.Text>
          </Flex>
        </Card>
        <Card title="恢复到独立环境">
          <Form layout="vertical" disabled={busy || pending}>
            <Form.Item label="备份">
              <Select
                value={backupId}
                placeholder="请选择已完成备份"
                onChange={setSelected}
                options={(status.data?.backups ?? []).map((item) => ({
                  value: item.id,
                  label: `${formatDateTime(item.created_at)} · ${item.files} 个文件`,
                }))}
              />
            </Form.Item>
            <Form.Item
              label="新环境名称"
              extra="数据库名使用 fq_restore_ 前缀，存储放入独立恢复目录。"
            >
              <Input
                prefix="fq_restore_"
                value={name}
                onChange={(event) => setName(event.target.value.toLowerCase())}
              />
            </Form.Item>
            <Button
              disabled={
                !backupId || !/^[a-z0-9_]{1,45}$/.test(name) || pending || busy
              }
              onClick={restore}
            >
              恢复到新环境
            </Button>
            <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>
              数据库账号需要创建数据库权限；也可由管理员预建同名空库。完成后显示目标位置，由你决定是否切换应用。
            </Typography.Paragraph>
          </Form>
        </Card>
      </div>
      <Section title="完整备份">
        <Resource state={status}>
          {(data) => (
            <Table<BackupItem>
              rowKey="id"
              dataSource={data.backups}
              pagination={{ pageSize: 10 }}
              columns={[
                { title: "备份", dataIndex: "id" },
                {
                  title: "创建时间",
                  render: (_, item) => formatDateTime(item.created_at),
                },
                { title: "文件", dataIndex: "files" },
                {
                  title: "大小",
                  render: (_, item) =>
                    `${formatNumber(item.bytes / 1024 ** 2, 1)} MB`,
                },
                {
                  title: "操作",
                  render: (_, item) => (
                    <Space>
                      <Button
                        disabled={busy || pending}
                        onClick={() =>
                          void submit({
                            action: "verify",
                            backup_id: item.id,
                          }).catch(() => {})
                        }
                      >
                        校验
                      </Button>
                      <Button onClick={() => setSelected(item.id)}>
                        选择恢复
                      </Button>
                    </Space>
                  ),
                },
              ]}
            />
          )}
        </Resource>
      </Section>
      <Section title="备份与恢复任务">
        <Resource state={status}>
          {(data) => (
            <Table<MaintenanceTask>
              rowKey="id"
              dataSource={data.jobs}
              pagination={{ pageSize: 10 }}
              columns={[
                {
                  title: "操作",
                  render: (_, job) =>
                    ACTIONS[job.request.action] ?? job.request.name,
                },
                {
                  title: "状态",
                  render: (_, job) => <StatusTag status={job.status} />,
                },
                {
                  title: "说明",
                  render: (_, job) => (
                    <Flex vertical>
                      <Typography.Text type={job.error ? "danger" : undefined}>
                        {job.error ?? job.result?.message ?? job.message}
                      </Typography.Text>
                      {job.result?.database ? (
                        <Typography.Text>
                          数据库：{job.result.database}；目录：
                          {job.result.storage_root}
                        </Typography.Text>
                      ) : null}
                    </Flex>
                  ),
                },
                {
                  title: "创建时间",
                  render: (_, job) => formatDateTime(job.created_at),
                },
              ]}
            />
          )}
        </Resource>
      </Section>
    </Flex>
  );
}
