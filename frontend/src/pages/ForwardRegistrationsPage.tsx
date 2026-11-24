import { useEffect } from "react";
import { Alert, Button, Pagination, Tag } from "antd";
import {
  forwardRegistrationsApi,
  type ForwardRegistration,
} from "../api/forwardRegistrations";
import { paths, setSearch, useReturnFocus } from "../app/routes";
import { useMediaQuery } from "../app/useMediaQuery";
import {
  AppLink,
  EmptyState,
  Loading,
  PageHeader,
  Section,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import {
  ForwardRegistrationDetail,
  factorDirection,
  registrationStatus,
} from "./ForwardRegistrationDetail";
import "./forwardRegistration.css";

const PAGE_SIZE = 20;

const dateColumns = [
  ["信号日", "signal_date"],
  ["入场日", "entry_date"],
  ["退出日", "exit_date"],
] as const;

function Dates({ row, from = 0 }: { row: ForwardRegistration; from?: number }) {
  return (
    <dl className="forward-list-dates">
      {dateColumns.slice(from).map(([label, key]) => (
        <div key={key}>
          <dt>{label}</dt>
          <dd>{row[key] || "暂不可用"}</dd>
        </div>
      ))}
    </dl>
  );
}

function NameLink({ row }: { row: ForwardRegistration }) {
  return (
    <AppLink
      to={paths.forwardRegistration(row.id)}
      returnFocusId={`forward-registration-${row.id}`}
      className="forward-link forward-name"
    >
      {row.name}
    </AppLink>
  );
}

function Parameters({ row }: { row: ForwardRegistration }) {
  return (
    <div className="forward-list-parameters">
      <span>
        {row.factor === "momentum" ? "动量" : row.factor} {row.factor_window}{" "}
        日，{factorDirection(row.factor_direction)}
      </span>
      <span>
        前 {row.top_k} / 全池 {row.pool_size}
      </span>
    </div>
  );
}

function RegistrationRows({
  rows,
  mobile,
  combinedDates,
}: {
  rows: ForwardRegistration[];
  mobile: boolean;
  combinedDates: boolean;
}) {
  if (mobile)
    return (
      <ul className="forward-mobile-list" aria-label="前瞻登记列表">
        {rows.map((row) => (
          <li key={row.id}>
            <NameLink row={row} />
            <div>
              <Tag>{registrationStatus(row.status)}</Tag>
            </div>
            <Dates row={row} />
            <Parameters row={row} />
          </li>
        ))}
      </ul>
    );
  return (
    <table
      className="forward-table forward-registration-table"
      aria-label="前瞻登记列表"
    >
      <thead>
        <tr>
          <th scope="col">登记名称</th>
          <th scope="col">登记状态</th>
          {!combinedDates && <th scope="col">信号日</th>}
          <th scope="col">{combinedDates ? "登记日期" : "入场与退出"}</th>
          <th scope="col">冻结参数</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id}>
            <th scope="row">
              <NameLink row={row} />
            </th>
            <td>
              <Tag>{registrationStatus(row.status)}</Tag>
            </td>
            {!combinedDates && <td>{row.signal_date || "暂不可用"}</td>}
            <td>
              <Dates row={row} from={combinedDates ? 0 : 1} />
            </td>
            <td>
              <Parameters row={row} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ForwardRegistrationList({ page }: { page: number }) {
  const mobile = useMediaQuery("(max-width: 767px)");
  const combinedDates = useMediaQuery("(max-width: 1100px)");
  const offset = (page - 1) * PAGE_SIZE;
  const state = useApiResource(
    (signal) => forwardRegistrationsApi.list(offset, PAGE_SIZE, signal),
    [offset],
  );
  const list = state.data;
  useReturnFocus(Boolean(list), String(page));
  useEffect(() => {
    if (list && page > 1 && offset >= list.total) {
      const last = Math.ceil(list.total / PAGE_SIZE);
      setSearch({ page: last > 1 ? String(last) : null });
    }
  }, [list, offset, page]);

  return (
    <div className="forward-page">
      <PageHeader
        title="前瞻登记"
        extra={
          <AppLink
            to={paths.newForwardRegistration()}
            returnFocusId="new-forward-registration"
            className="forward-link forward-create-link"
          >
            新建登记
          </AppLink>
        }
      />
      <Section>
        {state.error ? (
          <Alert
            type="warning"
            title="登记列表加载失败"
            description={state.error}
            action={
              <Button onClick={state.reload} loading={state.refreshing}>
                重试
              </Button>
            }
          />
        ) : !list ? (
          <Loading rows={6} />
        ) : list.items.length ? (
          <>
            <RegistrationRows
              rows={list.items}
              mobile={mobile}
              combinedDates={combinedDates}
            />
            <div className="forward-pagination">
              <span>共 {list.total} 条登记</span>
              {list.total > PAGE_SIZE && (
                <Pagination
                  current={page}
                  pageSize={PAGE_SIZE}
                  total={list.total}
                  simple={mobile}
                  showSizeChanger={false}
                  onChange={(next) =>
                    setSearch({ page: next > 1 ? String(next) : null })
                  }
                />
              )}
            </div>
          </>
        ) : list.total === 0 ? (
          <EmptyState
            title="暂无前瞻登记"
            description="已保存的前瞻登记会显示在这里。"
          />
        ) : (
          <Loading rows={4} />
        )}
      </Section>
    </div>
  );
}

export default function ForwardRegistrationsPage({
  id,
  page,
}: {
  id?: string;
  page: string | null;
}) {
  if (id) return <ForwardRegistrationDetail key={id} id={id} />;
  const number = Number(page);
  return (
    <ForwardRegistrationList
      page={
        number > 0 &&
        Number.isSafeInteger(number) &&
        Number.isSafeInteger(number * PAGE_SIZE)
          ? number
          : 1
      }
    />
  );
}
