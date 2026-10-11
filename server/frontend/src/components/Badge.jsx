import { SEVERITY, STATUS } from "../app/status.js";

export function Badge({ cls = "neu", children, title }) {
  return <span className={`b ${cls}`} title={title}>{children}</span>;
}

export function SeverityBadge({ severity }) {
  const s = SEVERITY[severity] ?? { label: severity, cls: "neu" };
  return <Badge cls={s.cls}>{s.label}</Badge>;
}

export function StatusBadge({ status }) {
  const s = STATUS[status] ?? { label: status, cls: "neu" };
  return <Badge cls={s.cls}>{s.label}</Badge>;
}
