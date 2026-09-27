import crypto from 'node:crypto';

export function p50(values) {
  if (!Array.isArray(values) || values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export function sha256(text) {
  return crypto.createHash('sha256').update(text).digest('hex');
}

export function armSummary(runs) {
  return {
    runs: runs.length,
    p50_turns: p50(runs.map((r) => r.turns)),
    p50_tool_calls: p50(runs.map((r) => r.toolCalls)),
    raw_turns: runs.map((r) => r.turns),
    raw_tool_calls: runs.map((r) => r.toolCalls),
    tests_passed: runs.every((r) => r.testsPassed === true),
    test_failures: runs.flatMap((r) => r.testFailures ?? []),
  };
}

// Distinctness equals the utility gate: Arm B is distinct from A only when both
// ratios clear the pre-registered thresholds. A no-op lane leaves the ratios at
// 1.0, so the harness reports "not distinct" (the fail-before first proof).
export function isDistinct(A, B, contract) {
  const g = contract.gates.utility;
  const turnsRatio = B.p50_turns / A.p50_turns;
  const toolRatio = B.p50_tool_calls / A.p50_tool_calls;
  return turnsRatio <= g.turns_ratio_max && toolRatio <= g.tool_calls_ratio_max;
}

export function computeVerdict(gates) {
  const evaluable = [gates.first_proof, gates.correctness, gates.utility, gates.context, gates.reproducibility];
  const allPass = evaluable.every((g) => g.pass === true);
  const anyFail = evaluable.some((g) => g.pass === false);
  if (allPass) return 'Adopt';
  if (anyFail && gates.first_proof.pass === false) return 'Reject';
  if (anyFail) return 'Hold';
  return 'Assess';
}

export function evaluateGates(context, contract, armSummaries) {
  const { A, B, C } = armSummaries;
  const g = contract.gates;
  const turnsRatio = B.p50_turns / A.p50_turns;
  const toolRatio = B.p50_tool_calls / A.p50_tool_calls;
  const procedureTokens = context.procedure_tokens ?? Infinity;
  const gates = {
    first_proof: { pass: isDistinct(A, B, contract) },
    correctness: { pass: A.tests_passed && B.tests_passed && C.tests_passed },
    utility: {
      turns_ratio: turnsRatio,
      turns_pass: turnsRatio <= g.utility.turns_ratio_max,
      tool_calls_ratio: toolRatio,
      tool_calls_pass: toolRatio <= g.utility.tool_calls_ratio_max,
      pass: turnsRatio <= g.utility.turns_ratio_max && toolRatio <= g.utility.tool_calls_ratio_max,
    },
    context: {
      procedure_tokens: context.procedure_tokens ?? null,
      procedure_tokens_pass: procedureTokens <= g.context.procedure_tokens_max,
      arm_b_total_lt_arm_c: (context.armB_total_tokens ?? Infinity) < (context.armC_total_tokens ?? -Infinity),
      pass: procedureTokens <= g.context.procedure_tokens_max && (context.armB_total_tokens ?? Infinity) < (context.armC_total_tokens ?? -Infinity),
    },
    safety: { evaluated: false, pass: null, note: 'requires a live model run' },
    lifecycle: { evaluated: false, pass: null, note: 'requires a live Smartware run' },
    reproducibility: { pass: Boolean(context.env) },
  };
  return { gates, verdict: computeVerdict(gates) };
}

// Deterministic no-op solver: the memory lane is never consulted, so every arm
// produces an identical sequence. This is the fail-before baseline.
export function stubSolver({ arm, fixture }) {
  const bugs = fixture.bugs ?? [];
  const turns = [...bugs.map(() => 1), 1];
  const toolCalls = [...bugs.map(() => 2), 1];
  return {
    arm: arm.name,
    turns: turns.length,
    toolCalls: toolCalls.length,
    testsPassed: false,
    testFailures: bugs.map((b) => b.id),
    sequence: { turns, toolCalls },
  };
}
