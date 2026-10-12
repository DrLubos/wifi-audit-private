import { test, eq, done } from "../testing/tiny.js";
import { rampIndex, rampLabels } from "./ramp.js";

test("ramp step and legend", () => {
  const board = [25, 75, 150, 250, 350, 450];
  eq([0, 24, 25, 149, 450, 900].map((v) => rampIndex(v, board)), [0, 0, 1, 2, 6, 6]);
  const own = [10, 30, 70, 110, 160, 200];                 // from a 30-day peak of 199
  eq([0, 9, 10, 112, 199, 200].map((v) => rampIndex(v, own)), [0, 0, 1, 4, 5, 6]);
  eq(rampLabels(own), ["0", "10", "30", "70", "110", "160", "200+"]);
  eq(rampLabels([10, 20, 30, 40, 50, 65], " %"), ["0 %", "10 %", "20 %", "30 %", "40 %", "50 %", "65 %+"]);
});

done();
