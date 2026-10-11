#!/usr/bin/env node
import { main } from "../dist/cli.js";
main(process.argv.slice(2)).catch((e) => {
  process.stderr.write(`smartware: ${e && e.message ? e.message : e}\n`);
  process.exit(1);
});
