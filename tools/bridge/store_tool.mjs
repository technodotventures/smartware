// Store interop helper for tests/test_interop.py: open a store written by either engine, then export, recall, or
// write through the TypeScript service. Prints JSON.
//   node store_tool.mjs <db> export <principal-json>
//   node store_tool.mjs <db> recall <principal-json> <query>
//   node store_tool.mjs <db> remember <principal-json> <body-json>
import { Service, SqliteStore, Principal, HashingEncoder, rankerParams } from "../../packages/core/dist/index.js";
const [db, op, prj, arg] = process.argv.slice(2);
const p = JSON.parse(prj);
const pr = new Principal(p.id, p.groups, p.tenant);
const svc = new Service(new HashingEncoder(), { params: rankerParams("smartware-rrf-1"), store: new SqliteStore(db), secretScan: false });
let out;
if (op === "export") out = await svc.export(pr);
else if (op === "recall") out = await svc.recall({ query: arg, as_of: "2027-01-01T00:00:00" }, pr);
else if (op === "remember") out = await svc.remember(JSON.parse(arg), pr);
else if (op === "changes") out = await svc.changes(0, pr);
delete out.request_id;
svc.close();
process.stdout.write(JSON.stringify(out));
