// Dump a generated op sequence as JSON (if_version = "CUR" is resolved by the differential driver).
import { makeGen } from "./gen.mjs";
const [seed = 1, n = 100, idProb = 0.6] = process.argv.slice(2).map(Number);
const g = makeGen(seed, 8, idProb);
const ops = [];
for (let i = 0; i < n; i++) ops.push(g.next(() => "CUR"));
process.stdout.write(JSON.stringify(ops));
