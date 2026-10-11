import { test, eq, done } from "../testing/tiny.js";
import { filterSearch, readStored, resolveFilters, writeStored } from "./filters.js";

const url = (o) => (k) => (k in o ? o[k] : null);

test("the URL wins, then the last choice, then the defaults", () => {
  eq(resolveFilters(["sensor", "range"], url({ sensor: "pi-fri", range: "30d" }), { range: "24h" }, ["pi-fri"]),
     { values: { sensor: "pi-fri", range: "30d" }, changed: false });
  eq(resolveFilters(["sensor", "range"], url({}), { sensor: "pi-fri", range: "24h" }, ["pi-fri"]),
     { values: { sensor: "pi-fri", range: "24h" }, changed: true });
  eq(resolveFilters(["sensor", "range"], url({}), null, null),
     { values: { sensor: "all", range: "7d" }, changed: true });
});

test("invalid values fall back; sensors are checked once known", () => {
  eq(resolveFilters(["range"], url({ range: "90d" }), null, null).values.range, "7d");
  eq(resolveFilters(["sensor"], url({ sensor: "pi-lab" }), null, null).values.sensor, "pi-lab");   // not loaded yet
  eq(resolveFilters(["sensor"], url({ sensor: "pi-lab" }), null, ["pi-fri"]),
     { values: { sensor: "all", range: "7d" }, changed: true });
  eq(resolveFilters(["sensor"], url({ sensor: "pi-lab" }), { sensor: "pi-fri" }, ["pi-fri"]).values.sensor, "pi-fri");
});

test("only the page's keys are written", () => {
  eq(resolveFilters(["sensor"], url({ sensor: "all" }), null, ["pi-fri"]).changed, false);    // no range on Events
});

test("links carry the global keys only", () => {
  eq(filterSearch({ sensor: "pi-fri", range: "30d", view: "week" }), "?sensor=pi-fri&range=30d");
});

test("storage failures are harmless", () => {
  const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  eq(readStored(broken), null);
  writeStored(broken, { sensor: "all", range: "7d" });
  const mem = new Map();
  const s = { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, v) };
  writeStored(s, { sensor: "pi-fri", range: "30d", other: 1 });
  eq(readStored(s), { sensor: "pi-fri", range: "30d" });
  mem.set("wifi-audit.filters.v1", "{not json");
  eq(readStored(s), null);
});

done();
