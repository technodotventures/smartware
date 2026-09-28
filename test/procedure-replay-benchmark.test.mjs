import { describe, it, expect } from 'vitest';
import { p50, armSummary, isDistinct, evaluateGates, stubSolver } from '../benchmarks/procedure-replay/runner/index.mjs';

const contract = {
  gates: {
    utility: { turns_ratio_max: 0.81, tool_calls_ratio_max: 0.60 },
    context: { procedure_tokens_max: 500 },
  },
};

describe('procedure-replay measurement kernel', () => {
  it('p50 is the median', () => {
    expect(p50([1, 2, 3, 4, 5])).toBe(3);
    expect(p50([1, 2, 3, 4])).toBe(2.5);
    expect(p50([])).toBe(null);
  });

  it('first proof (fail-before): identical arms are not distinct', () => {
    const A = armSummary([{ turns: 5, toolCalls: 9, testsPassed: true }]);
    const B = armSummary([{ turns: 5, toolCalls: 9, testsPassed: true }]);
    expect(isDistinct(A, B, contract)).toBe(false);
  });

  it('first proof (pass-after): a materially cheaper Arm B is distinct', () => {
    const A = armSummary([{ turns: 10, toolCalls: 10, testsPassed: true }]);
    const B = armSummary([{ turns: 4, toolCalls: 4, testsPassed: true }]);
    expect(isDistinct(A, B, contract)).toBe(true);
  });

  it('stub solver ignores the memory lane, so A and B are identical', () => {
    const fixture = { bugs: [{ id: 'bug-1' }, { id: 'bug-2' }, { id: 'bug-3' }] };
    const a = stubSolver({ arm: { name: 'A' }, fixture });
    const b = stubSolver({ arm: { name: 'B' }, fixture });
    expect(a.turns).toBe(b.turns);
    expect(a.toolCalls).toBe(b.toolCalls);
    expect(isDistinct(armSummary([a]), armSummary([b]), contract)).toBe(false);
  });

  it('context gate rejects a procedure over 500 tokens', () => {
    const { gates } = evaluateGates(
      { procedure_tokens: 600, armB_total_tokens: 700, armC_total_tokens: 1400, env: {} },
      contract,
      {
        A: armSummary([{ turns: 5, toolCalls: 5, testsPassed: true }]),
        B: armSummary([{ turns: 5, toolCalls: 5, testsPassed: true }]),
        C: armSummary([{ turns: 5, toolCalls: 5, testsPassed: true }]),
      },
    );
    expect(gates.context.procedure_tokens_pass).toBe(false);
    expect(gates.context.pass).toBe(false);
  });
});
