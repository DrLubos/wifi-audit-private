// A test helper that runs under `node --test` (the frontend image's build
// stage: a test file that throws fails the build) and under any plain
// JavaScript engine (no node:test, no dependencies).
//
//   import { test, eq, ok, done } from "../testing/tiny.js";
//   test("name", () => { eq(f(1), 2); });
//   done();   // last line: throws if any test failed

const failures = [];
let count = 0;

const log = (...a) => (globalThis.console?.log ?? globalThis.print)(...a);

function show(v) {
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

export function eq(actual, expected, msg = "") {
  if (show(actual) !== show(expected)) {
    throw new Error(`${msg ? msg + ": " : ""}expected ${show(expected)}, got ${show(actual)}`);
  }
}

export function ok(value, msg = "expected a truthy value") {
  if (!value) throw new Error(msg);
}

export function throws(fn, msg = "expected an exception") {
  let threw = false;
  try {
    fn();
  } catch {
    threw = true;
  }
  if (!threw) throw new Error(msg);
}

export function test(name, fn) {
  count += 1;
  try {
    fn();
  } catch (e) {
    failures.push(name);
    log(`FAIL ${name}: ${e.message}`);
  }
}

// for promises: `await atest("name", async () => { ... })` (top-level await)
export async function atest(name, fn) {
  count += 1;
  try {
    await fn();
  } catch (e) {
    failures.push(name);
    log(`FAIL ${name}: ${e.message}`);
  }
}

export function done() {
  if (failures.length) throw new Error(`${failures.length} of ${count} tests failed`);
  log(`ok ${count} tests`);
}
