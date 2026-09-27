import fs from 'node:fs';
import path from 'node:path';
import { armSummary, evaluateGates, stubSolver, sha256 } from '../benchmarks/procedure-replay/runner/index.mjs';

async function main() {
  const contractPath = process.argv[2];
  if (!contractPath) {
    throw new Error('Usage: node scripts/run-procedure-replay-benchmark.mjs <benchmarks/procedure-replay/contract-v1.json>');
  }
  const root = path.dirname(path.resolve(contractPath));
  const contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
  const solverName = process.env.PROCEDURE_REPLAY_SOLVER ?? 'stub';
  const solve = solverName === 'stub'
    ? stubSolver
    : (() => { throw new Error(`unknown solver: ${solverName}`); })();

  const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');
  const fixture = JSON.parse(read('fixture/manifest.json'));
  const arms = ['A', 'B', 'C'].map((name) => JSON.parse(read(`arms/arm-${name.toLowerCase()}.json`)));

  const replications = contract.replications ?? 2;
  const summaries = {};
  const rawRuns = {};
  for (const arm of arms) {
    const runs = [];
    for (let i = 0; i < replications; i++) runs.push(solve({ arm, fixture }));
    summaries[arm.name] = armSummary(runs);
    rawRuns[arm.name] = runs;
  }

  const armB = arms.find((a) => a.name === 'B');
  const armC = arms.find((a) => a.name === 'C');
  const fixtureHashes = {};
  for (const f of ['fixture/manifest.json', 'fixture/task.md', 'fixture/repo/index.js']) {
    fixtureHashes[f] = sha256(read(f));
  }

  const context = {
    procedure_tokens: armB.context.injected_tokens,
    armB_total_tokens: armB.context.total_tokens,
    armC_total_tokens: armC.context.total_tokens,
    env: { solver: solverName, replications, node: process.version, fixture_hashes: fixtureHashes },
  };

  const { gates, verdict } = evaluateGates(context, contract, summaries);
  process.stdout.write(JSON.stringify({
    contract: contract.name,
    solver: solverName,
    replications,
    fixture_hashes: fixtureHashes,
    arms: summaries,
    raw_runs: rawRuns,
    gates,
    verdict,
  }, null, 2) + '\n');
}

await main();
