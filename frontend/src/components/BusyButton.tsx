import { LoadingOutlined } from "@ant-design/icons";
import { Button, type ButtonProps } from "antd";

type BusyButtonProps = Omit<ButtonProps, "children" | "loading"> & {
  busy: boolean;
  children: string;
};

const loading = { icon: <LoadingOutlined aria-hidden="true" spin /> };

export function BusyButton({ busy, children, ...props }: BusyButtonProps) {
  return (
    <Button
      {...props}
      aria-label={children}
      aria-busy={busy}
      loading={busy && loading}
    >
      {children}
    </Button>
  );
}
