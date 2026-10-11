import { useMemo, useState } from "react";

// columns: [{ key, label, render?(row), sortValue?(row), align?: "right", className? }]
// Nulls sort last in either direction. renderDetail(row), when given, makes rows
// clickable and toggles a full-width detail row under the clicked one.
function compare(a, b) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return String(a).localeCompare(String(b));
}

export default function DataTable({ columns, rows, rowKey, initialSort, onRowClick, renderDetail,
                                    empty = "No rows.", className = "" }) {
  const [sort, setSort] = useState(initialSort ?? { key: columns[0].key, dir: "asc" });
  const [expanded, setExpanded] = useState(() => new Set());

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

  function toggleDetail(key) {
    setExpanded((set) => {
      const next = new Set(set);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  const clickable = Boolean(onRowClick || renderDetail);
  const handleClick = onRowClick ?? (renderDetail ? (row) => toggleDetail(rowKey(row)) : undefined);

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
          ) : sorted.map((row) => {
            const key = rowKey(row);
            const open = renderDetail ? expanded.has(key) : false;
            return [
              <tr key={key}
                  className={`${clickable ? "clickable" : ""} ${open ? "open" : ""}`}
                  onClick={handleClick ? () => handleClick(row) : undefined}
                  aria-expanded={renderDetail ? open : undefined}>
                {columns.map((c) => (
                  <td key={c.key}
                      className={`${c.align === "right" ? "num" : ""} ${c.className ?? ""}`}>
                    {c.render ? c.render(row) : (row[c.key] ?? "-")}
                  </td>
                ))}
              </tr>,
              open ? (
                <tr key={`${key}-detail`} className="detail-row">
                  <td colSpan={columns.length}>{renderDetail(row)}</td>
                </tr>
              ) : null,
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}
