// Contract: the core's queue surface carries timing, not just counts.
//
// A host that embeds this package reads the core, not the package's own health route. Counts alone
// cannot separate a queue with work waiting from a queue whose drain has stopped: they are the same
// four numbers in both cases, which is how an undrained queue stayed invisible for days in the Pod
// (34 pending jobs, attempts = 0 on every row, no completion for two days, and nothing to alert on).
// The oldest pending timestamp is what names the stall, so it has to be reachable from the core.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import { SmartwareCore } from '../src/core.js';

const opened: SmartwareCore[] = [];
const directories: string[] = [];

afterEach(() => {
  for (const core of opened.splice(0)) core.close();
  for (const directory of directories.splice(0)) {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

async function openCore(): Promise<SmartwareCore> {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sw-queue-timing-'));
  directories.push(dataDir);
  const core = await SmartwareCore.open({ dataDir, ownerId: 'user:owner' });
  opened.push(core);
  return core;
}

const OWNER = { type: 'person' as const, id: 'user:owner', display_name: 'Owner' };

async function observeOne(core: SmartwareCore, body: string): Promise<void> {
  await core.observe({
    actor: OWNER,
    type: 'message',
    content: { format: 'text/plain', body },
    scope: 'personal',
  });
}

describe('SmartwareCore.compileQueueStats timing', () => {
  it('reports null timing on an untouched queue rather than inventing an age', async () => {
    const core = await openCore();
    const stats = core.compileQueueStats();
    expect(stats).not.toBeNull();
    expect(stats!.timing.oldest_pending_at).toBeNull();
    expect(stats!.timing.last_completed_at).toBeNull();
    expect(stats!.timing.last_failed_at).toBeNull();
  });

  it('names the oldest pending job once observe enqueues, so a host can age the queue', async () => {
    const core = await openCore();
    await observeOne(core, 'The Atlas release is on hold until Friday.');

    const stats = core.compileQueueStats();
    expect(stats!.pending_count).toBeGreaterThanOrEqual(1);
    expect(stats!.timing.oldest_pending_at).not.toBeNull();

    // The age a host derives for its own health surface must be real and non-negative, which is the
    // whole point of exposing the timestamp rather than the age: the reader owns the clock.
    const ageSeconds = (Date.now() - new Date(stats!.timing.oldest_pending_at!).getTime()) / 1000;
    expect(Number.isFinite(ageSeconds)).toBe(true);
    expect(ageSeconds).toBeGreaterThanOrEqual(0);
  });

  it('clears oldest_pending_at and stamps a completion once a batch drains', async () => {
    const core = await openCore();
    await observeOne(core, 'The Atlas release is on hold until Friday.');

    const result = await core.drainCompileQueue({ limit: 10, useLLM: false });
    expect(result).not.toBeNull();

    const stats = core.compileQueueStats();
    expect(stats!.pending_count).toBe(0);
    expect(stats!.timing.oldest_pending_at).toBeNull();
    expect(stats!.timing.last_completed_at).not.toBeNull();
  });
});
