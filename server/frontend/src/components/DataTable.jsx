import { useMemo, useState } from "react";

// columns: [{ key, label, render?(row), sortValue?(row), align?: "right", className? }]
// Nulls sort last in either direction.
function compare(a, b) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return String(a).localeCompare(String(b));
}

export default function DataTable({ columns, rows, rowKey, initialSort, onRowClick,
                                    empty = "No rows.", className = "" }) {
  const [sort, setSort] = useState(initialSort ?? { key: columns[0].key, dir: "asc" });

  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sort.key);
    const get = col?.sortValue ?? ((r) => r[sort.key]);
    const dir = sort.dir === "desc" ? -1 : 1;
    return [...rows].sort((a, b) => {
      const va = get(a);
      const vb = get(b);
      if (va == null || vb == null) return compare(va, vb);   // nulls last regardless of dir
      return dir * compare(va, vb);
    });
  }, [rows, sort, columns]);

  function toggle(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
                                  : { key, dir: "asc" }));
  }

  return (
    <div className={`table-wrap ${className}`}>
      <table>
        <thead>
          <tr>
            {columns.map((c) => {
              const active = sort.key === c.key;
              return (
                <th key={c.key}
                    className={`sortable ${c.align === "right" ? "num" : ""}`}
                    onClick={() => toggle(c.key)}
                    aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
                  {c.label}
                  <span className="arrow" aria-hidden="true">
                    {active ? (sort.dir === "asc" ? "▲" : "▼") : ""}
                  </span>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 ? (
            <tr><td colSpan={columns.length} className="muted">{empty}</td></tr>
          ) : sorted.map((row) => (
            <tr key={rowKey(row)}
                className={onRowClick ? "clickable" : ""}
                onClick={onRowClick ? () => onRowClick(row) : undefined}>
              {columns.map((c) => (
                <td key={c.key}
                    className={`${c.align === "right" ? "num" : ""} ${c.className ?? ""}`}>
                  {c.render ? c.render(row) : (row[c.key] ?? "-")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
