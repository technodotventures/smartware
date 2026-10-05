// Smartware embedded core API
//
// This module lets host runtimes such as Coffee Pod use Smartware as an
// in-process protocol engine instead of talking to the stdio MCP server.

import fs from 'fs';
import path from 'path';
import { ulid } from 'ulid';

import { loadConfig, POD_SELF_SCOPE, saveConfig, substrateActorId, type Grant, type ScopeEntry, type SmartwareConfig, type SourceEntry, type SourceKind, type SourceStatus } from './config.js';
import { registerSourceEntry, listSourceEntries, type RegisterSourceParams } from './ingestion/sources.js';
import { IngestionStore } from './ingestion/store.js';
import { handleIngest, type IngestDeps } from './ingestion/ingest.js';
import type { IngestHooks, IngestParams, IngestResult } from './ingestion/types.js';
import { computeSourceSyncStatus, type SourceSyncStatus } from './ingestion/sync.js';
import { SMARTWARE_VERSION } from './version.js';
import { Layer0Index } from './layer0/index.js';
import { ClaimStore } from './layer1/store.js';
import { isEffectiveCurrent } from './layer1/effective_current.js';
import { backfillTombstones } from './layer1/tombstone-backfill.js';
import { writeRegistryMarkdown } from './auth/registry-md.js';
import { ensureDefaultAliases } from './auth/alias-map.js';
import { CascadePreviewStore } from './preview_store/index.js';
import {
  SearchIndex,
  syncSearchFromClaims,
  syncObservationsFromEvidence,
  observationToIndexRow,
  searchObservationQueryTerms,
  makeObservationSnippet,
} from './layer3/search.js';
import type {
  ObservationFreshness,
  ObservationSearchResult as IndexedObservationSearchResult,
} from './layer3/search.js';
import {
  claimToSemanticDocument,
  semanticDocumentSetHash,
  type EmbeddingAdapter,
  type SemanticDocument,
  type SemanticEmbeddingRecord,
} from './layer3/semantic.js';
import type { TemporalConstraint } from './layer3/temporal.js';
import {
  syncPersistedSemanticRecords,
  type PersistedSemanticSyncResult,
  type SemanticRecordLoadStatus,
  type SemanticRecordStore,
} from './layer3/semantic-store.js';
import {
  rankHybridDocuments,
  type HybridMatch,
  type SemanticChannelStatus,
} from './layer3/hybrid.js';
import { buildAuthorizedClaimSnapshot } from './layer4/authorized-claims.js';
import { ScopeRegistry } from './scopes/registry.js';
import { writeManifest, countWikiPages } from './layer2/manifest.js';
import { ensureGitRepo } from './layer2/git.js';
import { replayCatchUp } from './layer1/replay.js';
import { iterAllClaimVersions, type ClaimVersionRecord } from './layer1/jsonl.js';
import { readAll } from './layer0/log.js';
import type { Actor, Observation } from './layer0/types.js';
import type { ClaimRelation, EpistemicTag } from './layer1/types.js';
import { epistemicToTag } from './layer1/types.js';
import { runDefaultDream, type DreamResult } from './dream/phases.js';
import { runRecovery, type RecoveryReport } from './ops_log/recovery.js';
import type { MutationFence } from './ops_log/commit.js';
import { ensurePrivateDirectory } from './storage/private-fs.js';
import { FenceStore } from './storage/fence.js';

import { handleObserve, type ObserveParams, type ObserveResult, type ObserveCommitHooks } from './protocol/observe.js';
import {
  handleQuery,
  recallMinimumConfidence,
  type QueryParams,
  type QueryResult,
} from './protocol/query.js';
import { handleCompile, type CompileParams, type CompileHandlerResult } from './protocol/compile.js';
import { handleRead, type ReadParams, type ReadResult, type ScopeBrowseResult } from './protocol/read.js';
import { handleExplain, type ExplainParams, type ExplainResult } from './protocol/explain.js';
import { handleCorrect, type CorrectParams, type CorrectResult } from './protocol/correct.js';
import { handleRevise as handleReviseSpec, type ReviseParams, type ReviseResult } from './protocol/revise.js';
import { handleForget, handleRevive, type ForgetParams, type ForgetResult, type ReviveParams, type ReviveResult } from './protocol/forget.js';
import { handleExpireRetention, type ExpireRetentionParams, type ExpireRetentionResult } from './protocol/retention.js';
import { handleConsolidate, type ConsolidateParams, type ConsolidateResult } from './protocol/consolidate.js';
import {
  handleForgetScope,
  type ForgetScopeParams,
  type ForgetScopeResult,
} from './protocol/forget_scope.js';
import {
  handleExportScope,
  type ExportScopeParams,
  type ExportScopeResult,
} from './protocol/export_scope.js';
import {
  handleRestoreScope,
  type RestoreScopeParams,
  type RestoreScopeResult,
} from './protocol/restore_scope.js';
import { handleEndorse, type EndorseParams, type EndorseResult } from './protocol/endorse.js';
import { handleHoldRelease, type HoldReleaseParams, type HoldReleaseResult } from './protocol/hold_release.js';
import {
  handleQuarantineReview,
  type QuarantineReviewParams,
  type QuarantineReviewResult,
} from './protocol/quarantine_review.js';
import { handleGrant, type GrantParams, type GrantResult } from './protocol/grant.js';
import { handleRevoke, type RevokeParams, type RevokeResult } from './protocol/revoke.js';
import {
  handleSessionStart, handleSessionDescribe, handleSessionEnd,
  requireSessionCapability, resolveActorFromSession,
  type SessionStartParams, type SessionStartResult,
  type SessionDescribeResult, type SessionEndResult,
} from './protocol/session.js';
import { handleStatus, type StatusResult } from './protocol/status.js';
import { handleHealth, type HealthParams, type HealthReport } from './protocol/health.js';
import { MetricsStore, defaultMetricsPath } from './observability/metrics.js';
import { LatencyRecorder } from './observability/latency.js';
import { installObservability } from './observability/instrument.js';
import { handleContext, type ContextParams, type ContextBundle } from './protocol/context.js';
import { SessionStore } from './session/store.js';
import { createGrant, getGrantForActor, isOwner, checkGrant } from './auth/grants.js';
import { ProtocolError, requireGrant, requireOwner } from './auth/middleware.js';
import {
  openCompileQueue,
  runCompileBatch,
  type CompileBatchResult,
  type CompileJobStatus,
  type CompileQueue,
  type CompileWorkerContext,
  type FingerprintIndex,
} from './compile_queue/index.js';

export interface SmartwareCoreOptions {
  dataDir: string;
  ownerId?: string;
  /**
   * Fencing epoch for this writer (ADR-0007). When present, the writer is bound to this
   * monotonic token: it is claimed at open (a stale owner fails fast), and every canonical
   * mutation is refused before any artifact is written if the brain has already seen a
   * higher epoch. Omit for legacy, unfenced operation.
   */
  fencingToken?: number;
  /** Crash-boundary test seam (ADR-0010): fired at intent / observation / commit. */
  commitHooks?: ObserveCommitHooks;
}

/** Public fencing state of a core writer (ADR-0007). */
export interface FencingState {
  /** True once this brain has seen any claim (i.e. fencing was adopted). */
  enabled: boolean;
  /** The epoch this writer presented (null when it has none). */
  token: number | null;
  /** The highest epoch this brain has seen. */
  high_water: number;
  /** When the current epoch was claimed (null when no epoch has been seen). */
  claimed_at: string | null;
  /** Canonical mutations refused by the guard so far. */
  refusals: number;
  /** The most recent refusal, or null. */
  last_refusal: { op: string; token: number | null; high_water: number; at: string } | null;
}

// Public type surface for the source registry (re-exported for hosts).
export type { SourceEntry, SourceKind, SourceStatus };

export interface SmartwareDreamParams {
  actor: Actor;
  scope: string;
}

export interface SmartwareSemanticDocumentsParams {
  actor: Actor;
  /** Server-anchored identity overrides actor.id when supplied. */
  session_id?: string;
  scope: string;
  min_confidence?: number;
  epistemic?: string[];
  epistemic_tags?: EpistemicTag[];
  entity_type?: string;
  include_sensitive?: boolean;
  include_stale?: boolean;
}

export interface SmartwareSemanticIndexOptions {
  adapter: EmbeddingAdapter;
  store: SemanticRecordStore;
  batch_size?: number;
  max_documents?: number;
  timeout_ms?: number;
}

export interface SmartwareHybridRecallOptions {
  adapter: EmbeddingAdapter | null;
  store: SemanticRecordStore | null;
  min_similarity: number;
  limit?: number;
  candidate_limit?: number;
  rrf_k?: number;
  lexical_weight?: number;
  semantic_weight?: number;
  semantic_timeout_ms?: number;
  temporal?: TemporalConstraint;
}

export interface SmartwareHybridRecallMatch extends HybridMatch {
  claim_id: string;
  entity_id: string;
  entity_name: string;
  predicate: string;
  object: unknown;
  epistemic: string;
  confidence: number;
  status: string;
  observation_ids: string[];
}

export interface SmartwareHybridRecallResult {
  canonical: QueryResult;
  hybrid_results: SmartwareHybridRecallMatch[];
  selected_channel: 'canonical' | 'hybrid';
  semantic_status: SemanticChannelStatus;
  semantic_index_status: SemanticRecordLoadStatus | 'not_configured';
  semantic_error?: string;
  semantic_index_error?: string;
}

export interface FederatedRecallParams {
  actor: Actor;
  query: string;
  /** Named scopes: every one must be readable by the actor, or the read denies. */
  scopes?: string[];
  /** Per-scope result limit. */
  limit?: number;
  min_confidence?: QueryParams['min_confidence'];
  include_stale?: boolean;
  include_superseded?: boolean;
  include_forgotten?: boolean;
  include_sensitive?: boolean;
}

export interface FederatedRecallResult {
  /** Scopes actually queried, in request (or registration) order. */
  scopes: string[];
  /** Per-scope ranked results, concatenated scope-major. Every row carries `scope`. */
  results: QueryResult['results'];
  per_scope: Array<{ scope: string; total_found: number; returned: number }>;
  total_found: number;
}

export interface SmartwareActivityEvent {
  id: string;
  type: string;
  scope: string;
  actor_id: string;
  actor_type: string;
  actor_display_name: string;
  observed_at: string;
  captured_at: string;
  content: string | object;
  source_id: string | null;
  /** Registered source id this evidence came from (null on legacy records). */
  source_ref: string | null;
  sensitive: boolean;
}

export interface SmartwareObservationSearchResult {
  id: string;
  type: string;
  scope: string;
  actor_id: string;
  observed_at: string;
  captured_at: string;
  snippet: string;
  source_app: string;
  source_id: string | null;
  /** Registered source id this evidence came from (null on legacy records). */
  source_ref: string | null;
  /** Effective status (accepted / quarantined / tombstoned / redacted / rejected). */
  status: string;
  /** State-based raw-freshness label: unverified | EXTRACTED | FAILED (spec §10a). */
  freshness: ObservationFreshness;
}

export interface SmartwareObservationEvidence extends SmartwareActivityEvent {
  status: string;
  source_app: string;
}

export interface SmartwareKnowledgeGraphSnapshot {
  entities: Array<{
    entity_id: string;
    entity_name: string;
    type: string;
    scope: string;
    created_at: string;
  }>;
  claims: Array<{
    claim_id: string;
    subject_id: string;
    subject_name: string;
    predicate: string;
    object: { type: string; value: unknown };
    scope: string;
    epistemic_tag: EpistemicTag;
    confidence: number;
    created_at: string;
    valid_at: string;
    invalid_at: string | null;
    recorded_at: string | null;
    invalidated_at: string | null;
    provenance: {
      origin: 'deterministic' | 'model' | 'user';
      observation_ids: string[];
      model_id?: string;
    };
    relations: ClaimRelation[];
  }>;
}

export interface SmartwareConflictSnapshot {
  claims: Array<{
    claim_id: string;
    subject_id: string;
    subject_name: string;
    predicate: string;
    object: { type: string; value: unknown };
    scope: string;
    status: 'contested';
    contested_by: string[];
    epistemic_tag: EpistemicTag;
    confidence: number;
    version: number;
    created_at: string;
    valid_at: string;
    invalid_at: string | null;
    provenance: {
      origin: 'deterministic' | 'model' | 'user';
      observation_ids: string[];
      model_id?: string;
    };
  }>;
}

export interface SmartwarePodProfile {
  pod_id: string;
  name: string;
  owner_id: string;
  scopes: {
    personal: string;
    workspace: string;
  };
  memory_policy: {
    read_policy: 'task_start';
    write_policy: 'durable_summary';
    sensitive_policy: 'confirm';
    raw_transcript_policy: 'off';
  };
}

export class SmartwareCore {
  readonly dataDir: string;
  readonly evidenceDir: string;
  readonly wikiDir: string;
  /** Operations log canonical surface — see ops_log/ and docs/atomicity.md. */
  readonly opsDir: string;
  /** Cascade preview store for REVISE two-phase endorsement (PR-7 / A6). */
  readonly previewStore: CascadePreviewStore;

  private layer0: Layer0Index;
  private store: ClaimStore;
  private searchIndex: SearchIndex;
  private sessionStore: SessionStore;
  private ingestionStore: IngestionStore;
  private previewGcInterval: NodeJS.Timeout | null = null;
  /** Durable compile queue + fingerprint index (async-compile path, §9.1). */
  private compileQueue: CompileQueue | null = null;
  private fingerprintIndex: FingerprintIndex | null = null;
  /** Fencing epoch state (ADR-0007); null token = legacy unfenced writer. */
  private readonly fence: FenceStore;
  private fenceToken: number | null = null;
  /** Durable operational metrics (refusals, latency, recovery summaries). */
  private readonly metrics: MetricsStore;
  /** In-process latency buffer; flushed to `metrics` on report / close. */
  private readonly latency: LatencyRecorder;
  /** When this process opened the brain (health: restart detection / uptime). */
  private readonly openedAt: string = new Date().toISOString();
  /** The brain's canonical writer identity (config.writer_id) — stamped with the epoch. */
  private writerId = '';
  /** Crash-boundary test seam forwarded into OBSERVE (see SmartwareCoreOptions.commitHooks). */
  private observeHooks: ObserveCommitHooks | null = null;
  /** Recovery report captured at open (ADR-0010); see lastRecoveryReport(). */
  private openRecoveryReport: RecoveryReport | null = null;

  private constructor(dataDir: string, layer0: Layer0Index, store: ClaimStore, searchIndex: SearchIndex, sessionStore: SessionStore, fence: FenceStore, metrics: MetricsStore, latency: LatencyRecorder) {
    this.dataDir = dataDir;
    this.evidenceDir = path.join(dataDir, 'evidence');
    this.wikiDir = path.join(dataDir, 'wiki');
    this.opsDir = path.join(dataDir, 'operations');
    this.layer0 = layer0;
    this.store = store;
    this.searchIndex = searchIndex;
    this.sessionStore = sessionStore;
    this.fence = fence;
    this.metrics = metrics;
    this.latency = latency;
    this.previewStore = new CascadePreviewStore(path.join(dataDir, 'indices', 'previews.db'));
    // Ingestion ledger (batch receipts + stream cursors). Operational state:
    // see the honesty note in src/ingestion/store.ts.
    this.ingestionStore = new IngestionStore(path.join(dataDir, 'smartware.db'));
  }

  static async open(options: SmartwareCoreOptions): Promise<SmartwareCore> {
    ensurePrivateDirectory(options.dataDir);
    if (!fs.existsSync(path.join(options.dataDir, 'config.json'))) {
      await initialiseDataDir(options.dataDir, options.ownerId);
    }

    const dbPath = path.join(options.dataDir, 'smartware.db');
    const layer0 = new Layer0Index(dbPath);
    const store = new ClaimStore(dbPath);
    const searchIndex = new SearchIndex(dbPath);
    const sessionStore = new SessionStore(dbPath);
    const fence = FenceStore.open(dbPath);
    const metrics = MetricsStore.open(defaultMetricsPath(options.dataDir));
    const latency = new LatencyRecorder(metrics);

    const core = new SmartwareCore(options.dataDir, layer0, store, searchIndex, sessionStore, fence, metrics, latency);
    // Canonical writer identity + the crash-boundary hook seam (ADR-0010 / test harness).
    core.writerId = loadConfig(options.dataDir).writer_id;
    core.observeHooks = options.commitHooks ?? null;
    // ADR-0007: a fenced writer claims its epoch before any recovery or derived-index
    // work. A stale owner fails fast here — it must not run recovery or write anything.
    if (options.fencingToken !== undefined) {
      try {
        core.adoptFenceToken(options.fencingToken, 'open');
      } catch (error) {
        core.close();
        throw error;
      }
    }
    // PR-14: tell the ClaimStore where the L1 JSONL canonical lives. Every
    // subsequent insertClaim will also append a versioned record.
    store.setDataDir(options.dataDir);
    // Finalize only exact intent-backed canonical artifacts before derived
    // indices catch up. Ambiguous operations remain untouched for Dream/manual
    // review; startup never invents a completion decision.
    const recovery = runRecovery({
      opsDir: core.opsDir,
      evidenceDir: core.evidenceDir,
      claimsDir: core.dataDir,
      wikiDir: core.wikiDir,
      quarantineDir: path.join(core.dataDir, 'quarantine', 'operations'),
      fence: core.mutationFence(),
    });
    core.openRecoveryReport = recovery;
    // The recovery scan is an operational event: record what it found at open so
    // a host can alert on "this brain has been recovering" without reading logs.
    // Counts only — no locators, no content (see docs/integration/observability.md).
    metrics.recordRecovery({
      at: new Date().toISOString(),
      opened_at: core.openedAt,
      committed_operations: recovery.committedOperations,
      orphans: recovery.orphans.length,
      pending_operations: recovery.pendingOperations.length,
      intent_errors: recovery.intentErrors.length,
      requires_manual_review: recovery.requiresManualReview.length,
      completed: recovery.completed.length,
      quarantined: recovery.quarantined.length,
      aborted: recovery.aborted.length,
    });
    layer0.catchUp(core.evidenceDir);
    if (recovery.pendingOperations.length === 0) {
      await replayCatchUp(core.evidenceDir, store, layer0);
    }
    // Backfill derived search structures on upgrade/open. Older databases do
    // not have the claim-granular FTS table until this version creates it.
    syncSearchFromClaims(store, searchIndex);
    // Backfill the raw-observation FTS window (spec §10a). Regenerable from
    // the evidence JSONL; terminal-state observations are excluded here so a
    // wipe-and-rebuild equals the live index (rebuild-equivalence contract).
    syncObservationsFromEvidence(core.evidenceDir, layer0, searchIndex);

    // Durable compile queue (spec §9.1): open the derived ledger + O(1)
    // fingerprint index, reconcile running/pending jobs with the evidence
    // log and ops receipts, and re-apply EXTRACTED/FAILED labels after a
    // rebuild. Derived state — a wiped indices/ dir regenerates at open.
    const openedQueue = await openCompileQueue({
      evidenceDir: core.evidenceDir,
      dataDir: core.dataDir,
      layer0,
      searchIndex,
      opsDir: core.opsDir,
    });
    if (openedQueue) {
      core.compileQueue = openedQueue.queue;
      core.fingerprintIndex = openedQueue.fingerprintIndex;
    }

    // PR-4 (A3): backfill tombstones for legacy `retracted` claims. Idempotent.
    // Do not synthesize lifecycle artifacts while an intent-backed mutation
    // is incomplete; its retry must remain the sole writer of those records.
    if (recovery.pendingOperations.length === 0) {
      backfillTombstones(store, core.wikiDir);
    }

    // PR-5 (A4): write the markdown projection of the agent registry to
    // pod_data/agents/registry.md. Read-only for humans today; becomes the
    // canonical source in a later PR.
    writeRegistryMarkdown(core.dataDir, loadConfig(core.dataDir));

    // PR-9 (A4 alias map): seed Coffee Pod's default forward-only actor
    // renames into pod_data/agents/aliases.jsonl. Idempotent on re-open.
    ensureDefaultAliases(core.dataDir);

    // PR-7 (A6): start the cascade preview store GC timer. Sweeps the
    // SQLite-backed previews table for expired and consumed rows.
    core.startPreviewGc();
    // GC once on open so a long-stopped Pod doesn't accumulate stale rows.
    core.previewStore.gc();

    // Host-facing accounting (refusals + latency) is installed at the boundary.
    return installObservability(core, metrics, latency);
  }

  /**
   * Fencing (ADR-0007). A host that arbitrates brain ownership outside the brain
   * (a lease with a monotonic epoch) binds its writer to that epoch here. The brain
   * persists the highest epoch it has seen and refuses any canonical mutation from a
   * writer whose epoch is older — before any artifact is written.
   */

  /** Register a new ownership epoch on this writer (takeover without reopen). */
  claimFence(token: number): FencingState {
    this.adoptFenceToken(token, 'claim');
    return this.fencingState();
  }

  /** The auditable fencing state of this brain (session token + persisted high-water). */
  fencingState(): FencingState {
    const state = this.fence.state();
    return {
      enabled: state.high_water > 0,
      token: this.fenceToken,
      high_water: state.high_water,
      claimed_at: state.high_water > 0 ? state.updated_at : null,
      refusals: state.refusals,
      last_refusal: state.last_refusal,
    };
  }

  private adoptFenceToken(token: number, op: string): void {
    this.fence.claim(token, op);
    this.fenceToken = token;
  }

  /**
   * The mutation-boundary guard: every canonical mutation calls this first, so a
   * stale writer is refused with a code-carrying ProtocolError BEFORE any artifact
   * (evidence JSONL, claim version, ops entry) is touched.
   */
  private fenceGuard(op: string): void {
    this.fence.guard(this.fenceToken, op);
  }

  /**
   * The storage-level fence (ADR-0010): stamps the ownership epoch into the intent and the
   * commit signal, and gates the commit against the persisted high-water mark so a stale
   * writer's partial artifact set is refused at the gate — never finalized, never merged.
   */
  private mutationFence(): MutationFence {
    return {
      stamp: () => (this.fenceToken === null
        ? null
        : { epoch: this.fenceToken, writer_id: this.writerId }),
      guardCommit: (operationIds: string[], op: string) => {
        const stamp = this.fenceToken === null
          ? null
          : { epoch: this.fenceToken, writer_id: this.writerId };
        this.fence.guardCommit(operationIds, stamp, op);
      },
      highWater: () => this.fence.highWater(),
      authorization: (operationId: string) => this.fence.authorization(operationId),
      authorizeAtEpoch: (operationIds: string[], epoch: number, writerId: string) =>
        this.fence.authorizeAtEpoch(operationIds, epoch, writerId),
    };
  }

  /**
   * The recovery report from this writer's open (ADR-0010 surface): how the brain classified
   * intent-backed state when it last opened, including `staleEpochRejected` — uncommitted sets
   * from an epoch behind the high-water mark, rejected and never merged. Null before open.
   */
  lastRecoveryReport(): RecoveryReport | null {
    return this.openRecoveryReport;
  }

  getConfig(): SmartwareConfig {
    return loadConfig(this.dataDir);
  }

  getRegistry(): ScopeRegistry {
    return new ScopeRegistry(this.getConfig());
  }

  /**
   * Ensure an application-defined set of scopes exists.
   *
   * Smartware owns scope enforcement and hierarchy, not product taxonomy.
   * Consumers may register app, project, or domain spaces without adding
   * those concepts to Smartware's profile contract.
   */
  ensureScopes(entries: ScopeEntry[]): void {
    this.fenceGuard('ensureScopes');
    const config = this.getConfig();
    const existing = new Set(config.scopes.map(scope => scope.id));
    const additions = entries.filter(entry => !existing.has(entry.id));
    if (additions.length === 0) return;
    config.scopes.push(...additions);
    saveConfig(this.dataDir, config);
  }

  /**
   * Register (or update) a provenance origin for this business brain.
   *
   * Owner-only, like every other provisioning change. Coffee owns OAuth,
   * scheduling and connector credentials; the brain owns the label that makes
   * ingested evidence attributable. Re-registering an id updates the mutable
   * fields and preserves `created_at` — never duplicates the entry.
   */
  registerSource(params: RegisterSourceParams): SourceEntry {
    this.fenceGuard('registerSource');
    return registerSourceEntry(this.dataDir, params);
  }

  /** Every registered source in this brain. Owner-only. */
  listSources(params: { actor: Actor }): SourceEntry[] {
    requireOwner(params.actor.id, this.getConfig());
    return listSourceEntries(this.getConfig());
  }

  /**
   * Sync status projection for the host's UI/scheduler: per registered source,
   * per scope: cursor, last sync, and batch outcome counts. Owner-only; a
   * named unknown source denies (`source_unregistered`) rather than answering
   * empty.
   */
  sourceSyncStatus(params: { actor: Actor; source_id?: string }): SourceSyncStatus[] {
    requireOwner(params.actor.id, this.getConfig());
    return computeSourceSyncStatus(this.getConfig(), this.ingestionStore, params.source_id);
  }

  /**
   * Create a pod profile: the pod's memory policy, owner, and its two
   * host-registered lanes.
   *
   * The ids in `scopes` (`pod/<pod>/personal`, `pod/<pod>/workspace`) are
   * **host-registered lanes**, not protocol-native v0.5.0 `Scope` values: they
   * are registered in this brain's scope registry and are the pod's live scope
   * ids, but the published vocabulary admits no host-lane form, so a record
   * written in one is outside the v0.5.0 schema-conformance claim (ADR-0015).
   * Hosts that need schema-conformant records use protocol-native lanes
   * (`self` / `workspace` / `project:<slug>` / `agent:<slug>`).
   */
  createPodProfile(podId: string, name = 'Pod'): SmartwarePodProfile {
    this.fenceGuard('createPodProfile');
    const config = this.getConfig();
    const scope = (suffix: string) => `pod/${podId}/${suffix}`;
    const scopes = {
      personal: scope('personal'),
      workspace: scope('workspace'),
    };

    this.ensureScopes([
      { id: scopes.personal, parent: null, visibility_default: 'private' as const },
      { id: scopes.workspace, parent: null, visibility_default: 'workspace' as const },
    ]);

    return {
      pod_id: podId,
      name,
      owner_id: config.owner_id,
      scopes,
      memory_policy: {
        read_policy: 'task_start',
        write_policy: 'durable_summary',
        sensitive_policy: 'confirm',
        raw_transcript_policy: 'off',
      },
    };
  }

  ensureTrustedClientGrant(actorId: string, actorType: 'agent' | 'person' | 'system', scopes: string[]): Grant {
    this.fenceGuard('ensureTrustedClientGrant');
    const existing = getGrantForActor(actorId, this.getConfig());
    if (existing) {
      const config = this.getConfig();
      const stored = config.grants.find(grant => grant.id === existing.id)!;
      const capabilities = {
        observe: [...scopes],
        query: [...scopes],
        compile: [...scopes],
        correct: [...scopes],
        forget: [] as string[],
        read: [...scopes],
      };
      Object.assign(stored, { actor_type: actorType, capabilities, trusted: true, quarantine: false });
      saveConfig(this.dataDir, config);
      writeRegistryMarkdown(this.dataDir, config);
      return stored;
    }
    const grant = createGrant(this.dataDir, {
      actor_type: actorType,
      actor_id: actorId,
      capabilities: {
        observe: scopes,
        query: scopes,
        compile: scopes,
        correct: scopes,
        forget: [],
        read: scopes,
      },
      trusted: true,
      quarantine: false,
    });
    // PR-5 (A4): keep the markdown projection in sync on every grant mutation.
    writeRegistryMarkdown(this.dataDir, this.getConfig());
    return grant;
  }

  async observe(params: ObserveParams): Promise<ObserveResult> {
    this.fenceGuard('observe');
    const hostHooks = this.observeHooks;
    return handleObserve(
      params,
      this.evidenceDir,
      this.layer0,
      this.getConfig(),
      this.sessionStore,
      this.opsDir,
      {
        ...(hostHooks ?? {}),
        // Sync-raw freshness (spec §10a) + async-compile (spec §9.1), then the host hook
        // (crash-boundary test seam; may be async — awaited inside handleObserve).
        afterObservation: obs => {
          this.afterObservationCommitted(obs);
          return hostHooks?.afterObservation?.(obs);
        },
      },
      this.mutationFence(),
    );
  }

  /**
   * Post-commit hook shared by OBSERVE and ingestion batches: index the raw
   * observation into the always-searchable window and enqueue accepted
   * evidence on the durable compile queue (the LLM never runs on the write
   * path).
   */
  private afterObservationCommitted(obs: Observation): void {
    this.searchIndex.indexObservation(observationToIndexRow(obs));
    if (obs.status === 'accepted') {
      this.compileQueue?.enqueue(obs.id, obs.scope);
    }
  }

  /**
   * Ingest one batch of source-native items (connector polling loop).
   *
   * See `src/ingestion/ingest.ts` for the guarantees. Hosts call this with an
   * authenticated actor + registered source; context problems fail closed
   * before anything is written.
   */
  async ingest(params: IngestParams, hooks?: IngestHooks): Promise<IngestResult> {
    this.fenceGuard('ingest');
    const deps: IngestDeps = {
      evidenceDir: this.evidenceDir,
      layer0: this.layer0,
      config: this.getConfig(),
      store: this.ingestionStore,
      sessionStore: this.sessionStore,
      afterObservation: obs => this.afterObservationCommitted(obs),
    };
    return handleIngest(params, deps, hooks);
  }

  async query(params: QueryParams): Promise<QueryResult> {
    return handleQuery(params, this.store, this.searchIndex, this.getConfig(), this.getRegistry(), this.sessionStore);
  }

  async recall(params: QueryParams): Promise<QueryResult> {
    return this.query(params);
  }

  /**
   * Federated RECALL across multiple scopes in one call (the company-brain
   * "read across my workspaces" lane). Constraints:
   *
   *   - named scopes: the actor must be able to read EVERY named scope —
   *     otherwise the whole read denies (`insufficient_permission` /
   *     `actor_unregistered`). A federated read never partially answers a
   *     request that named an unauthorized scope;
   *   - omitted scopes: exactly the actor's readable scopes (owner: all scopes
   *     in the brain; a registered actor: the scopes its grants cover; an
   *     unregistered actor: denial, not an empty result).
   *
   * Results are scope-tagged and ordered scope-major (each scope's own
   * ranking); scores are comparable within a scope, not across scopes.
   */
  async recallFederated(params: FederatedRecallParams): Promise<FederatedRecallResult> {
    const config = this.getConfig();
    const owner = isOwner(params.actor.id, config);
    const allScopes = config.scopes.map(entry => entry.id);

    let scopes: string[];
    if (params.scopes && params.scopes.length > 0) {
      scopes = [...new Set(params.scopes)];
      for (const scope of scopes) {
        requireGrant(params.actor.id, 'query', scope, config);
      }
    } else if (owner) {
      scopes = allScopes;
    } else {
      scopes = allScopes.filter(scope => checkGrant(params.actor.id, 'query', scope, config));
      if (scopes.length === 0) {
        const known = config.grants.some(grant => grant.actor_id === params.actor.id || grant.actor_id === '*');
        throw new ProtocolError(
          known ? 'insufficient_permission' : 'actor_unregistered',
          known
            ? `Actor '${params.actor.id}' has no readable scope`
            : `Actor '${params.actor.id}' is not registered with this Pod.`,
        );
      }
    }

    const results: FederatedRecallResult['results'] = [];
    const perScope: FederatedRecallResult['per_scope'] = [];
    let totalFound = 0;
    for (const scope of scopes) {
      const scoped = await handleQuery(
        {
          actor: params.actor,
          query: params.query,
          scope,
          limit: params.limit,
          min_confidence: params.min_confidence,
          include_stale: params.include_stale,
          include_superseded: params.include_superseded,
          include_forgotten: params.include_forgotten,
          include_sensitive: params.include_sensitive,
        },
        this.store,
        this.searchIndex,
        config,
        this.getRegistry(),
        this.sessionStore,
      );
      results.push(...scoped.results);
      totalFound += scoped.total_found;
      perScope.push({ scope, total_found: scoped.total_found, returned: scoped.results.length });
    }

    return { scopes, results, per_scope: perScope, total_found: totalFound };
  }

  async context(params: ContextParams): Promise<ContextBundle> {
    return handleContext(
      params,
      this.store,
      this.searchIndex,
      this.getConfig(),
      this.getRegistry(),
      this.evidenceDir,
      this.layer0,
    );
  }

  /**
   * Produce claim-level semantic documents through Smartware's canonical
   * authorization, sensitivity, lifecycle, and effective-current boundary.
   *
   * This is an embedded-core extension, not a change to the frozen RECALL wire
   * contract. Hosts may persist embeddings for these rebuildable documents but
   * must pass the same returned set to semantic ranking.
   */
  prepareSemanticDocuments(
    params: SmartwareSemanticDocumentsParams,
  ): SemanticDocument[] {
    const config = this.getConfig();
    let actorId = params.actor.id;
    if (params.session_id) {
      const resolved = resolveActorFromSession(params.session_id, this.sessionStore);
      if (!resolved) {
        throw new ProtocolError(
          'session_not_found',
          `Session '${params.session_id}' not found`,
        );
      }
      if (resolved.effective_policy.read_mode === 'off') {
        throw new ProtocolError('read_disabled', 'Session policy does not allow reads');
      }
      requireSessionCapability(resolved, 'query', params.scope);
      actorId = resolved.actor_id;
    }
    requireGrant(actorId, 'query', params.scope, config);
    const snapshot = buildAuthorizedClaimSnapshot({
      actorId,
      scope: params.scope,
      minConfidence: params.min_confidence,
      epistemic: params.epistemic,
      epistemicTags: params.epistemic_tags,
      entityType: params.entity_type,
      includeSensitive: params.include_sensitive,
      includeStale: params.include_stale,
      includeSuperseded: false,
      includeForgotten: false,
    }, this.store, config);

    return snapshot.claims
      .map(claimToSemanticDocument)
      .sort((left, right) =>
        right.version.localeCompare(left.version)
        || left.id.localeCompare(right.id));
  }

  /**
   * Refresh one disposable semantic-index scope through Smartware's canonical
   * query eligibility boundary. This is an explicit maintenance operation and
   * never runs as an implicit side effect of RECALL.
   */
  async syncSemanticIndex(
    params: SmartwareSemanticDocumentsParams,
    options: SmartwareSemanticIndexOptions,
  ): Promise<PersistedSemanticSyncResult> {
    if (options.max_documents !== undefined
      && (!Number.isInteger(options.max_documents) || options.max_documents < 1)) {
      throw new Error('Semantic index max documents must be a positive integer');
    }
    const sourceDocuments = this.prepareSemanticDocuments(params);
    const documents = sourceDocuments.slice(0, options.max_documents);
    return syncPersistedSemanticRecords(
      options.store,
      params.scope,
      documents,
      options.adapter,
      options.batch_size,
      {
        source_documents: sourceDocuments,
        coverage: documents.length === sourceDocuments.length ? 'complete' : 'partial',
        timeout_ms: options.timeout_ms,
      },
    );
  }

  /**
   * Evaluate opt-in hybrid retrieval without changing canonical RECALL.
   *
   * The canonical result is returned byte-for-byte from the existing path.
   * Hybrid candidates become selectable only after a complete local index and
   * successful semantic query. Missing/corrupt indices and provider failures
   * select the canonical result instead.
   */
  async recallHybrid(
    params: QueryParams,
    options: SmartwareHybridRecallOptions,
  ): Promise<SmartwareHybridRecallResult> {
    const temporalHistory = params.temporal?.axis === 'transaction_time'
      && params.temporal.mode !== 'current';
    if (params.include_superseded || params.include_forgotten || temporalHistory) {
      throw new ProtocolError(
        'unsupported_hybrid_history',
        'Hybrid recall does not support superseded or forgotten history',
      );
    }

    const canonical = await this.recall(params);
    const documents = this.prepareSemanticDocuments({
      actor: params.actor,
      session_id: params.session_id,
      scope: params.scope,
      min_confidence: recallMinimumConfidence(params.min_confidence),
      epistemic: params.epistemic,
      epistemic_tags: params.epistemic_tags,
      entity_type: params.entity_type,
      include_sensitive: params.include_sensitive,
      include_stale: params.include_stale,
    });
    let semanticIndexStatus: SmartwareHybridRecallResult['semantic_index_status']
      = 'not_configured';
    let semanticIndexError: string | undefined;
    let records: SemanticEmbeddingRecord[] = [];
    if (options.adapter && options.store) {
      const loaded = options.store.load(
        options.adapter,
        params.scope,
        undefined,
        {
          source_document_count: documents.length,
          document_set_hash: semanticDocumentSetHash(documents),
          require_complete: true,
        },
      );
      semanticIndexStatus = loaded.status;
      semanticIndexError = loaded.error;
      if (loaded.status === 'ready') records = loaded.records;
    }

    // Spec §11.1 D1 (BINDING): the RRF lexical channel must be claim-level
    // FTS ranks (searchClaims), not the canonical RECALL order. Canonical
    // order is entity-aggregated: an exact entity-name match ("Project
    // Aster") outranks a claim-level text match ("owns: Project Aster"),
    // which pushed `c_aster_schedule` ahead of `c_deliverable_aster` on q03.
    // Claim-level FTS ranks the correct claim #1 (9.70 vs 6.66, measured).
    // Claims the claim FTS missed keep a canonical-order tail so an empty
    // claim index degrades to the pre-fix feed instead of dropping lexical
    // coverage entirely.
    const ftsClaimIds = params.query.trim()
      ? this.searchIndex.searchClaims(params.query, params.scope).map(result => result.claim_id)
      : [];
    const canonicalClaimIds = canonical.results.flatMap(result => result.claim ? [result.claim.id] : []);
    const lexicalClaimIds = [
      ...ftsClaimIds,
      ...canonicalClaimIds.filter(id => !ftsClaimIds.includes(id)),
    ];
    const ranked = await rankHybridDocuments(
      params.query,
      lexicalClaimIds,
      documents,
      records,
      options.adapter,
      {
        min_similarity: options.min_similarity,
        limit: options.limit ?? params.limit ?? 20,
        candidate_limit: options.candidate_limit,
        rrf_k: options.rrf_k,
        lexical_weight: options.lexical_weight,
        semantic_weight: options.semantic_weight,
        semantic_timeout_ms: options.semantic_timeout_ms,
        temporal: options.temporal ?? params.temporal,
      },
    );
    const hybridResults = ranked.matches.flatMap(match => {
      const claim = this.store.getClaim(match.id);
      if (!claim) return [];
      return [{
        ...match,
        claim_id: claim.id,
        entity_id: claim.subject_id,
        entity_name: claim.subject_name,
        predicate: claim.predicate,
        object: claim.object,
        epistemic: claim.epistemic,
        confidence: claim.confidence,
        status: claim.status,
        observation_ids: [...claim.supporting_evidence],
      }];
    });
    const selectedChannel = ranked.semantic_status === 'ok' && hybridResults.length > 0
      ? 'hybrid'
      : 'canonical';

    return {
      canonical,
      hybrid_results: hybridResults,
      selected_channel: selectedChannel,
      semantic_status: ranked.semantic_status,
      semantic_index_status: semanticIndexStatus,
      ...(ranked.semantic_error === undefined
        ? {}
        : { semantic_error: ranked.semantic_error }),
      ...(semanticIndexError === undefined
        ? {}
        : { semantic_index_error: semanticIndexError }),
    };
  }

  /**
   * Activity feed over accepted raw observations. Actor-bound: the caller's
   * identity decides the scopes it may see, whether the request names one
   * scope (`requireGrant`, so an ungranted scope is a denial rather than an
   * empty feed) or asks across scopes (the feed is filtered to the scopes the
   * actor may read). Sensitive observations additionally require the owner AND
   * an explicit opt-in.
   */
  listActivity(options: {
    actor: Actor;
    scope?: string;
    types?: string[];
    actorId?: string;
    limit?: number;
    includeSensitive?: boolean;
  }): SmartwareActivityEvent[] {
    const config = this.getConfig();
    const owner = isOwner(options.actor.id, config);
    const includeSensitive = options.includeSensitive === true && owner;
    if (options.scope) requireGrant(options.actor.id, 'read', options.scope, config);

    // Per-scope decision memo: one config read per distinct scope, not per row.
    const readableScopes = new Map<string, boolean>();
    const mayRead = (scope: string): boolean => {
      const cached = readableScopes.get(scope);
      if (cached !== undefined) return cached;
      const allowed = owner || checkGrant(options.actor.id, 'read', scope, config);
      readableScopes.set(scope, allowed);
      return allowed;
    };

    const limit = options.limit ?? 50;
    const types = new Set(options.types ?? []);
    const events: SmartwareActivityEvent[] = [];

    for (const obs of readAll(this.evidenceDir)) {
      if (obs.status !== 'accepted') continue;
      if (options.scope && obs.scope !== options.scope) continue;
      if (!options.scope && !mayRead(obs.scope)) continue;
      if (types.size > 0 && !types.has(obs.type)) continue;
      if (options.actorId && obs.source.actor.id !== options.actorId) continue;
      if (obs.policy.sensitive && !includeSensitive) continue;
      events.push({
        id: obs.id,
        type: obs.type,
        scope: obs.scope,
        actor_id: obs.source.actor.id,
        actor_type: obs.source.actor.type,
        actor_display_name: obs.source.actor.display_name,
        observed_at: obs.source.observed_at,
        captured_at: obs.source.captured_at,
        content: obs.content.body,
        source_id: obs.source.source_id,
        source_ref: obs.source.source_ref ?? null,
        sensitive: obs.policy.sensitive,
      });
    }

    return events
      .sort((a, b) => b.observed_at.localeCompare(a.observed_at))
      .slice(0, limit);
  }

  /**
   * Raw-observation window (spec §10a) — the lane where un-compiled evidence is
   * searchable. Actor-bound like every other read lane: the caller's identity
   * decides what it may see, and an actor with no `read` grant on the scope is
   * denied rather than answered with an empty window. Sensitive observations
   * additionally require the owner AND an explicit opt-in — a staff caller can
   * never widen its own view by setting the flag.
   */
  searchObservations(params: {
    actor: Actor;
    query: string;
    scope: string;
    limit?: number;
    includeSensitive?: boolean;
    temporalRange?: { from: string; to: string };
    /** Restrict to these state-based freshness labels (spec §10a). */
    freshness?: ObservationFreshness[];
  }): SmartwareObservationSearchResult[] {
    const config = this.getConfig();
    requireGrant(params.actor.id, 'read', params.scope, config);
    const includeSensitive = params.includeSensitive === true && isOwner(params.actor.id, config);
    const terms = searchObservationQueryTerms(params.query);
    // The legacy substring matcher returned [] for a blank query with no
    // temporal anchor; keep that contract (the FTS fallback would otherwise
    // scan the whole scope).
    if (terms.length === 0 && !params.temporalRange) return [];

    // Time bound vs. state bound: the FTS window is state-based — the
    // freshness label, never a timestamp. Layer 0 is the authoritative
    // effective-status source, so hits are re-checked live: a mutation that
    // landed after indexing (tombstone/redaction/reject/approve) drops the
    // row immediately rather than after the next rebuild.
    const hits: IndexedObservationSearchResult[] = this.searchIndex.searchObservations(
      params.query,
      params.scope,
      {
        limit: params.limit,
        includeSensitive,
        temporalRange: params.temporalRange,
        freshness: params.freshness,
      },
    );

    const results: SmartwareObservationSearchResult[] = [];
    for (const hit of hits) {
      const effectiveStatus = this.layer0.getEffectiveStatus(hit.obs_id);
      if (effectiveStatus !== null && effectiveStatus !== 'accepted') continue;
      results.push({
        id: hit.obs_id,
        type: hit.type,
        scope: hit.scope,
        actor_id: hit.actor_id,
        observed_at: hit.observed_at,
        captured_at: hit.captured_at,
        snippet: makeObservationSnippet(hit.content, terms),
        source_app: hit.source_app,
        source_id: hit.source_id,
        source_ref: hit.source_ref,
        status: hit.status,
        freshness: hit.freshness,
      });
    }

    return results
      .sort((a, b) => b.observed_at.localeCompare(a.observed_at))
      .slice(0, params.limit ?? 10);
  }

  /**
   * Mark the state-based freshness label of one indexed observation.
   * Compile-queue callers transition unverified → EXTRACTED / FAILED as jobs
   * resolve (spec §10a — never time-based).
   */
  markObservationFreshness(obsId: string, freshness: ObservationFreshness): void {
    this.searchIndex.updateObservationFreshness(obsId, freshness);
  }

  /** Current state-based freshness label of an indexed observation (or null). */
  getObservationFreshness(obsId: string): ObservationFreshness | null {
    return this.searchIndex.getObservationFreshness(obsId);
  }

  readObservationEvidence(params: {
    actor: Actor;
    observation_id: string;
    include_sensitive?: boolean;
  }): SmartwareObservationEvidence | null {
    const observation = [...readAll(this.evidenceDir)].find(item => item.id === params.observation_id);
    if (!observation) return null;
    const config = this.getConfig();
    requireGrant(params.actor.id, 'read', observation.scope, config);
    if (observation.policy.sensitive && !(params.include_sensitive && isOwner(params.actor.id, config))) {
      return null;
    }
    return {
      id: observation.id,
      type: observation.type,
      status: this.layer0.getEffectiveStatus(observation.id) ?? observation.status,
      scope: observation.scope,
      actor_id: observation.source.actor.id,
      actor_type: observation.source.actor.type,
      actor_display_name: observation.source.actor.display_name,
      observed_at: observation.source.observed_at,
      captured_at: observation.source.captured_at,
      content: observation.content.body,
      source_app: observation.source.app,
      source_id: observation.source.source_id,
      source_ref: observation.source.source_ref ?? null,
      sensitive: observation.policy.sensitive,
    };
  }

  async compile(params: CompileParams): Promise<CompileHandlerResult> {
    this.fenceGuard('compile');
    return handleCompile(
      params,
      this.evidenceDir,
      this.wikiDir,
      this.layer0,
      this.store,
      this.searchIndex,
      this.getConfig(),
      this.dataDir,
      { opsDir: this.opsDir },
    );
  }

  async reflect(params: CompileParams): Promise<CompileHandlerResult> {
    return this.compile(params);
  }

  /**
   * Drain one batch of the durable compile queue (async-compile, spec §9.1).
   * The host decides when this runs — the MCP server starts a background
   * loop; embedded hosts may call this on an interval of their own. Returns
   * null when the compile queue is unavailable (derived-DB failure).
   */
  async drainCompileQueue(
    opts: { limit?: number; useLLM?: boolean } = {},
  ): Promise<CompileBatchResult | null> {
    this.fenceGuard('drainCompileQueue');
    if (!this.compileQueue || !this.fingerprintIndex) return null;
    const ctx: CompileWorkerContext = {
      evidenceDir: this.evidenceDir,
      dataDir: this.dataDir,
      layer0: this.layer0,
      store: this.store,
      searchIndex: this.searchIndex,
      config: this.getConfig(),
      opsDir: this.opsDir,
      queue: this.compileQueue,
      fingerprintIndex: this.fingerprintIndex,
    };
    return runCompileBatch(ctx, opts);
  }

  /**
   * Queue ledger, freshness and timing surface for the compile payload contract.
   *
   * `timing` is the half that names a stalled drain. A queue with work waiting and a queue whose
   * drain has stopped are identical in the counts alone, and only the age of the oldest pending job
   * separates them. It is exposed here because embedding hosts read the core rather than this
   * package's own health route, and the queue lives behind this method: without it a host can see
   * that work is waiting but cannot tell whether anything is draining it. A host that wants a
   * derived age computes it against its own clock from `oldest_pending_at`.
   */
  compileQueueStats(): {
    statuses: Record<CompileJobStatus, number>;
    pending_count: number;
    freshness: { unverified: number; extracted: number; failed: number };
    timing: {
      oldest_pending_at: string | null;
      last_completed_at: string | null;
      last_failed_at: string | null;
    };
  } | null {
    if (!this.compileQueue) return null;
    return {
      statuses: this.compileQueue.stats(),
      pending_count: this.compileQueue.countPending(),
      freshness: this.searchIndex.countObservationsByFreshness(),
      timing: this.compileQueue.timingStats(),
    };
  }

  /**
   * Run one owner-authorized Dream inspection pass.
   *
   * This is intentionally manual and derived-only: it does not install a
   * scheduler and does not pass a canonical L2 recompile callback.
   */
  dream(params: SmartwareDreamParams): DreamResult {
    this.fenceGuard('dream');
    const config = this.getConfig();
    if (!isOwner(params.actor.id, config)) {
      throw new ProtocolError('owner_required', 'Dream is an owner-only operator command');
    }
    const substrateId = substrateActorId(config);
    return runDefaultDream(
      { opsDir: this.opsDir },
      substrateId,
      params.scope,
      {
        evidenceDir: this.evidenceDir,
        claimsDir: this.dataDir,
        wikiDir: this.wikiDir,
        quarantineDir: path.join(this.dataDir, 'quarantine', 'operations'),
        reportDir: path.join(this.dataDir, 'derived', 'dream'),
      },
    );
  }

  async read(params: ReadParams): Promise<ReadResult | ScopeBrowseResult> {
    return handleRead(params, this.wikiDir, this.getConfig(), this.store, this.sessionStore);
  }

  /**
   * Authorization-aware view of unresolved contested claims. Conflict status
   * is intentionally separate from ordinary RECALL because contested claims
   * are not eligible current context.
   */
  readConflicts(params: {
    actor: Actor;
    scopes: string[];
    include_sensitive?: boolean;
  }): SmartwareConflictSnapshot {
    const config = this.getConfig();
    const scopes = [...new Set(params.scopes)];
    for (const scope of scopes) requireGrant(params.actor.id, 'read', scope, config);

    const allowedScopes = new Set(scopes);
    const canReadSensitive = params.include_sensitive === true && isOwner(params.actor.id, config);
    const latestVersions = new Map<string, ClaimVersionRecord>();
    for (const version of iterAllClaimVersions(this.dataDir)) {
      const previous = latestVersions.get(version.claim_id);
      if (!previous || version.version > previous.version) latestVersions.set(version.claim_id, version);
    }

    const db = this.store.getDB();
    const unresolved = this.store.getAllClaims()
        .filter(claim => claim.status === 'contested')
        .filter(claim => allowedScopes.has(claim.scope))
        .filter(claim => canReadSensitive || !claim.sensitive)
        .filter(claim => latestVersions.get(claim.id)?.state !== 'forgotten')
        .filter(claim => !db || isEffectiveCurrent(claim.id, db));
    const unresolvedIds = new Set(unresolved.map(claim => claim.id));

    return {
      claims: unresolved
        .map(claim => ({
          claim,
          contestedBy: claim.contested_by.filter(claimId => unresolvedIds.has(claimId)),
        }))
        .filter(({ contestedBy }) => contestedBy.length > 0)
        .map(({ claim, contestedBy }) => ({
          version: latestVersions.get(claim.id)?.version ?? 1,
          claim_id: claim.id,
          subject_id: claim.subject_id,
          subject_name: claim.subject_name,
          predicate: claim.predicate,
          object: claim.object,
          scope: claim.scope,
          status: 'contested' as const,
          contested_by: contestedBy,
          epistemic_tag: 'contested' as const,
          confidence: claim.confidence,
          created_at: claim.created_at ?? claim.extraction.extracted_at,
          valid_at: claim.validity.from,
          invalid_at: claim.validity.to,
          provenance: {
            origin: claim.extraction.method === 'llm'
              ? 'model' as const
              : claim.extraction.method === 'user_input'
                ? 'user' as const
                : 'deterministic' as const,
            observation_ids: [...claim.supporting_evidence],
            ...(claim.extraction.model ? { model_id: claim.extraction.model } : {}),
          },
        }))
        .sort((left, right) => left.claim_id.localeCompare(right.claim_id)),
    };
  }

  /**
   * Bulk, authorization-aware L1 snapshot for host knowledge-graph projections.
   * This is deliberately narrower than exposing ClaimStore: grants are checked
   * before a single bulk entity/claim read, and sensitive claims never cross
   * this seam unless the caller explicitly opts in.
   */
  readKnowledgeGraph(params: {
    actor: Actor;
    scopes: string[];
    include_sensitive?: boolean;
  }): SmartwareKnowledgeGraphSnapshot {
    const config = this.getConfig();
    const scopes = [...new Set(params.scopes)];
    for (const scope of scopes) requireGrant(params.actor.id, 'read', scope, config);

    const allowedScopes = new Set(scopes);
    const canReadSensitive = params.include_sensitive === true && isOwner(params.actor.id, config);
    const latestVersions = new Map<string, ClaimVersionRecord>();
    for (const version of iterAllClaimVersions(this.dataDir)) {
      const previous = latestVersions.get(version.claim_id);
      if (!previous || version.version > previous.version) latestVersions.set(version.claim_id, version);
    }
    const claims = this.store.getActiveClaims()
      .filter((claim) => allowedScopes.has(claim.scope))
      .filter((claim) => canReadSensitive || !claim.sensitive)
      .filter((claim) => latestVersions.get(claim.id)?.state !== 'forgotten');
    const claimById = new Map(claims.map((claim) => [claim.id, claim]));
    const scopedEntities = this.store.getAllEntities()
      .filter((entity) => allowedScopes.has(entity.scope));
    const entityById = new Map(scopedEntities.map((entity) => [entity.id, entity]));
    const entityByScopedName = new Map(scopedEntities.map((entity) => [`${entity.scope}\u0000${entity.canonical_name}`, entity]));
    const visibleEntityIds = new Set(claims.map((claim) => claim.subject_id));
    for (const claim of claims) {
      if (claim.object.type !== 'entity_ref' || typeof claim.object.value !== 'string') continue;
      const target = entityById.get(claim.object.value)
        ?? entityByScopedName.get(`${claim.scope}\u0000${claim.object.value}`);
      if (target) visibleEntityIds.add(target.id);
    }
    const entities = scopedEntities
      .filter((entity) => visibleEntityIds.has(entity.id))
      .map((entity) => ({
        entity_id: entity.id,
        entity_name: entity.canonical_name,
        type: entity.type,
        scope: entity.scope,
        created_at: entity.created_at,
      }));

    return {
      entities,
      claims: claims.map((claim) => ({
        claim_id: claim.id,
        subject_id: claim.subject_id,
        subject_name: claim.subject_name,
        predicate: claim.predicate,
        object: claim.object,
        scope: claim.scope,
        epistemic_tag: latestVersions.get(claim.id)?.epistemic_tag ?? epistemicToTag(claim.epistemic, claim.status),
        confidence: claim.confidence,
        created_at: claim.created_at ?? claim.extraction.extracted_at,
        valid_at: claim.validity.from,
        invalid_at: claim.validity.to,
        recorded_at: claim.t_ingested.value,
        invalidated_at: claim.t_invalidated.value,
        provenance: {
          origin: claim.extraction.method === 'llm'
            ? 'model'
            : claim.extraction.method === 'user_input'
              ? 'user'
              : 'deterministic',
          observation_ids: claim.supporting_evidence,
          ...(claim.extraction.model ? { model_id: claim.extraction.model } : {}),
        },
        relations: (latestVersions.get(claim.id)?.relations ?? claim.relations ?? [])
          .filter((relation) => claimById.has(relation.target)),
      })),
    };
  }

  async explain(params: ExplainParams): Promise<ExplainResult> {
    return handleExplain(params, this.evidenceDir, this.layer0, this.store, this.getConfig(), this.dataDir);
  }

  async correct(params: CorrectParams): Promise<CorrectResult> {
    this.fenceGuard('correct');
    const result = await handleCorrect(params, this.evidenceDir, this.layer0, this.store, this.getConfig());
    // Keep the claim-FTS surface truthful, exactly as `consolidate()` does below:
    // CORRECT retracts the target claim and spawns a replacement, and both halves
    // live in the derived rows the index is built from. A verb that appends claim
    // versions without re-syncing leaves the replacement unfindable — a warranted
    // correction answers *nothing* until some later write re-syncs the scope
    // (measured through the Coffee adapter, kanban t_8ddfa350; retention replay
    // hides the retracted side already, so the verdict was an empty result set,
    // which a user cannot tell apart from data loss).
    const correctedScope = this.store.getClaim(result.original_claim_id)?.scope;
    syncSearchFromClaims(this.store, this.searchIndex, correctedScope);
    return result;
  }

  async revise(params: ReviseParams): Promise<ReviseResult> {
    this.fenceGuard('revise');
    const result = await handleReviseSpec(
      params,
      this.dataDir,
      this.store,
      this.getConfig(),
      { opsDir: this.opsDir },
    );
    // Keep the claim-FTS surface truthful, exactly as `correct()` above and
    // `consolidate()` below do: REVISE appends a claim version through the
    // store, and the derived rows the index is built from move with it. A verb
    // that resyncs nothing answers a warranted revision with a claim the search
    // lane cannot see — the other half of the corrected claim keeps answering,
    // the revised one does not, until some later write happens to re-sync the
    // scope (measured through the Coffee adapter, kanban t_336ba0b9: in-process
    // recall served one row while every restart — the canonical surface —
    // served two). The re-sync is semantics-neutral on purpose: it makes the
    // live lane equal whatever the store says is indexable, so it holds
    // whether a REVISE leaves the target active or (per the demotion
    // carry-forward decision, t_742e31f9) still superseded.
    const revisedScope = this.store.getClaim(result.claim_id)?.scope;
    syncSearchFromClaims(this.store, this.searchIndex, revisedScope);
    return result;
  }

  async forget(params: ForgetParams): Promise<ForgetResult> {
    this.fenceGuard('forget');
    const result = await handleForget(
      params,
      this.evidenceDir,
      this.layer0,
      this.store,
      this.getConfig(),
      { opsDir: this.opsDir },
    );
    // Keep the raw-search window truthful after mutations: a terminal
    // observation (tombstone/redaction → tombstoned/redacted) must leave the
    // raw window immediately, not after the next rebuild.
    const targetId = params.target?.type === 'observation'
      ? params.target.id
      : params.target_obs_id;
    if (targetId) this.reconcileObservationIndexRow(targetId);
    return result;
  }

  /**
   * FORGET.SCOPE (protocol v0.5.0, spec §10): erasure or offboarding of a
   * whole scope. Owner-only. `semanticStore` is optional — when the host owns
   * a persisted vector index, pass the store so erasure removes the
   * embeddings too (they are part of the leak surface, §10).
   */
  async forgetScope(
    params: ForgetScopeParams,
    options: { semanticStore?: SemanticRecordStore | null } = {},
  ): Promise<ForgetScopeResult> {
    this.fenceGuard('forgetScope');
    const config = this.getConfig();
    return handleForgetScope(params, {
      evidenceDir: this.evidenceDir,
      dataDir: this.dataDir,
      layer0: this.layer0,
      store: this.store,
      searchIndex: this.searchIndex,
      config,
      commitCtx: { opsDir: this.opsDir },
      semanticStore: options.semanticStore ?? null,
      compileQueue: this.compileQueue,
      fingerprintIndex: this.fingerprintIndex,
    });
  }

  /**
   * Legal hold release (ADR-0009): lifts a scope's hold so erasure and the
   * retention sweep resume. Owner-only; idempotent per operation_id.
   */
  async releaseHold(params: HoldReleaseParams): Promise<HoldReleaseResult> {
    const config = this.getConfig();
    return handleHoldRelease(params, {
      dataDir: this.dataDir,
      opsDir: this.opsDir,
      config,
    });
  }

  /**
   * EXPORT.SCOPE (spec §10c.4, G3.1): one consumer's exact-scope canonical
   * record package under `<data_dir>/exports/<export_id>/`. Owner-only,
   * read-only to pod data; derived indexes are excluded (regenerable).
   * `operation_id` makes the export idempotent (retry ⇒ same export_id).
   */
  async exportScope(params: ExportScopeParams): Promise<ExportScopeResult> {
    const config = this.getConfig();
    return handleExportScope(params, {
      evidenceDir: this.evidenceDir,
      dataDir: this.dataDir,
      opsDir: this.opsDir,
      store: this.store,
      config,
    });
  }

  /**
   * RESTORE.SCOPE — the return path for an EXPORT.SCOPE package: write one package's canonical
   * records back into this brain (owner-only; target scope must be empty). Derived state
   * catches up from the canonical records, exactly like a wipe-and-rebuild.
   */
  async restoreScope(params: RestoreScopeParams): Promise<RestoreScopeResult> {
    this.fenceGuard('restoreScope');
    const config = this.getConfig();
    const result = await handleRestoreScope(params, {
      evidenceDir: this.evidenceDir,
      dataDir: this.dataDir,
      opsDir: this.opsDir,
      store: this.store,
      config,
    });
    if (result.status === 'restored') {
      this.layer0.catchUp(this.evidenceDir);
      this.store.setDataDir(this.dataDir);
      syncSearchFromClaims(this.store, this.searchIndex, result.scope);
      syncObservationsFromEvidence(this.evidenceDir, this.layer0, this.searchIndex);
    }
    return result;
  }

  /**
   * Retention expiry sweep (ADR-0001, ADR-0013). Tombstones elapsed
   * `duration`-policy observations in one scope and retracts their sole-evidence
   * claims, committing exactly one `retention.expire` ops entry per sweep with
   * exact counts (the caller's `operation_id` when supplied, otherwise one the
   * substrate mints for this invocation and returns). Idempotent by effect —
   * re-running finds no new expired records — and replay-idempotent when the
   * caller supplies the same id. Host-triggered, like `drainCompileQueue`.
   */
  async expireRetention(params: ExpireRetentionParams): Promise<ExpireRetentionResult> {
    this.fenceGuard('expireRetention');
    const config = this.getConfig();
    return handleExpireRetention(params, {
      evidenceDir: this.evidenceDir,
      dataDir: this.dataDir,
      layer0: this.layer0,
      store: this.store,
      config,
      opsDir: this.opsDir,
    });
  }

  /**
   * CONSOLIDATE (ADR-0002): collapse 2+ active claims into one reviewed
   * "current understanding" claim, preserving the evidence lineage and
   * tombstoning (not deleting) the inputs.
   */
  async consolidate(params: ConsolidateParams): Promise<ConsolidateResult> {
    this.fenceGuard('consolidate');
    const result = await handleConsolidate(
      params,
      this.dataDir,
      this.store,
      this.getConfig(),
      { opsDir: this.opsDir },
    );
    // Keep the claim-FTS surface truthful: the consolidated claim must be
    // findable and the tombstoned inputs must leave the index.
    syncSearchFromClaims(this.store, this.searchIndex, params.scope);
    return result;
  }

  /**
   * Reconcile one observation's raw-index row with its Layer-0 effective
   * status. Terminal states leave the index (matching wipe-and-rebuild
   * semantics); accepted/quarantined rows only update the status column.
   */
  private reconcileObservationIndexRow(obsId: string): void {
    const effective = this.layer0.getEffectiveStatus(obsId);
    if (effective === null || effective === 'accepted' || effective === 'quarantined') {
      if (effective) this.searchIndex.updateObservationStatus(obsId, effective);
      return;
    }
    this.searchIndex.removeObservation(obsId);
  }

  /**
   * REVIVE re-admits a forgotten claim's fact: the canonical record returns to
   * `active` and the derived row follows. The claim-FTS lane does not — it is
   * rebuilt only by `syncSearchFromClaims` — so without this re-sync the live
   * process answers without the re-admitted claim while every restart serves
   * it. FORGET deliberately keeps its rows (forget_scope.ts:519-537: the
   * authorized snapshot filters forgotten claims at query time, so served
   * results are rebuild-equivalent without touching the lane; only the raw
   * counters differ), but any later write in the scope replaces the lane with
   * the store's indexable set and drops the forgotten claim's row, so the
   * "searchable again without waiting for a reindex" guarantee has to be
   * re-established here rather than assumed from the keep. The scope comes
   * from the store so `ReviveResult`'s shape is untouched.
   */
  async revive(params: ReviveParams): Promise<ReviveResult> {
    this.fenceGuard('revive');
    const result = await handleRevive(
      params,
      this.dataDir,
      this.store,
      this.getConfig(),
      { opsDir: this.opsDir },
      this.store.getDB(),
    );
    const scope = this.store.getClaim(result.claim_id)?.scope;
    if (scope) syncSearchFromClaims(this.store, this.searchIndex, scope);
    return result;
  }

  async endorse(params: EndorseParams): Promise<EndorseResult> {
    this.fenceGuard('endorse');
    return handleEndorse(
      params,
      this.dataDir,
      this.store,
      this.previewStore,
      this.getConfig(),
      { opsDir: this.opsDir },
    );
  }

  async quarantineReview(
    params: QuarantineReviewParams,
  ): Promise<QuarantineReviewResult> {
    this.fenceGuard('quarantineReview');
    const result = await handleQuarantineReview(
      params,
      this.evidenceDir,
      this.layer0,
      this.store,
      this.getConfig(),
    );
    // Quarantine approval flips quarantined → accepted: the row must move
    // into the raw-search window; rejection removes it.
    this.reconcileObservationIndexRow(params.target_obs_id);
    return result;
  }

  async grant(params: GrantParams): Promise<GrantResult> {
    this.fenceGuard('grant');
    return handleGrant(
      params,
      this.evidenceDir,
      this.layer0,
      this.getConfig(),
      this.dataDir,
    );
  }

  async revoke(params: RevokeParams): Promise<RevokeResult> {
    this.fenceGuard('revoke');
    return handleRevoke(
      params,
      this.evidenceDir,
      this.layer0,
      this.getConfig(),
      this.dataDir,
    );
  }

  async sessionStart(params: SessionStartParams): Promise<SessionStartResult> {
    return handleSessionStart({ ...params, opsDir: params.opsDir ?? this.opsDir }, this.sessionStore, this.getConfig());
  }

  async sessionDescribe(actorId: string, sessionId: string): Promise<SessionDescribeResult> {
    return handleSessionDescribe(
      { actor_id: actorId, session_id: sessionId },
      this.sessionStore,
      this.getConfig(),
    );
  }

  async sessionEnd(actorId: string, sessionId: string): Promise<SessionEndResult> {
    return handleSessionEnd(
      { actor_id: actorId, session_id: sessionId, opsDir: this.opsDir },
      this.sessionStore,
      this.getConfig(),
    );
  }

  async status(ownerActorId?: string): Promise<StatusResult> {
    const config = this.getConfig();
    return handleStatus(
      { actor: { type: 'person', id: ownerActorId ?? config.owner_id, display_name: ownerActorId ?? config.owner_id } },
      this.layer0,
      this.store,
      this.searchIndex,
      this.wikiDir,
      config,
    );
  }

  /**
   * Host-facing health/metrics report (P1-3). Owner or read-granted actor;
   * counts, states and ids only — never tenant content. See
   * `src/protocol/health.ts` and docs/integration/observability.md for the
   * field definitions.
   */
  async health(params: HealthParams): Promise<HealthReport> {
    const state = this.fencingState();
    return handleHealth(params, {
      config: this.getConfig(),
      layer0: this.layer0,
      store: this.store,
      searchIndex: this.searchIndex,
      wikiDir: this.wikiDir,
      compileQueue: this.compileQueue,
      metrics: this.metrics,
      latency: this.latency,
      dataDir: this.dataDir,
      opsDir: this.opsDir,
      ingestionStatus: () => computeSourceSyncStatus(this.getConfig(), this.ingestionStore),
      openedAt: this.openedAt,
      ownership: {
        arbitration: 'external',
        enforcement: state.enabled ? 'fencing' : 'none',
        role: state.token !== null ? 'writer' : (state.enabled ? 'observer' : 'unfenced_writer'),
        epoch_high_water: state.high_water,
        epoch_claimed_at: state.claimed_at,
        presented_token: state.token,
        refusals: state.refusals,
        last_refusal: state.last_refusal,
      },
    });
  }

  findObservationBySource(app: string, sourceId: string, scope: string): string | null {
    return this.layer0.checkDedup(app, sourceId, scope);
  }

  close(): void {
    if (this.previewGcInterval) {
      clearInterval(this.previewGcInterval);
      this.previewGcInterval = null;
    }
    this.compileQueue?.close();
    this.fingerprintIndex?.close();
    this.fence.close();
    this.latency.close();
    this.metrics.close();
    this.layer0.close();
    this.store.close();
    this.searchIndex.close();
    this.sessionStore.close();
    this.previewStore.close();
    this.ingestionStore.close();
  }

  /**
   * Start the cascade preview store's GC timer. Run-once on open. Idempotent
   * — calling twice is a no-op. Tests can skip by setting interval = 0.
   */
  startPreviewGc(intervalMs: number = 60_000): void {
    if (this.previewGcInterval || intervalMs <= 0) return;
    this.previewGcInterval = setInterval(() => {
      try {
        this.previewStore.gc();
      } catch {
        // GC errors are non-fatal; preview-store callers also handle
        // expired/not-found gracefully.
      }
    }, intervalMs);
    // Don't keep the event loop alive just for GC.
    if (typeof this.previewGcInterval.unref === 'function') {
      this.previewGcInterval.unref();
    }
  }
}

async function initialiseDataDir(dataDir: string, ownerId?: string): Promise<SmartwareConfig> {
  ensurePrivateDirectory(dataDir);
  ensurePrivateDirectory(path.join(dataDir, 'evidence'));
  // Pages are written under `wiki/<category>/` (spec §9 L2 conventions: concepts,
  // entities, decisions, synthesis, tombstones, profiles) and the compiler creates
  // its category directory on demand (`layer2/compiler.ts`), so init creates the
  // wiki root only. The `wiki/personal`, `wiki/workspace` and `wiki/project`
  // directories init used to create were vestigial — nothing in the library reads
  // or writes them (kanban t_574be8cd).
  ensurePrivateDirectory(path.join(dataDir, 'wiki'));

  const config: SmartwareConfig = {
    instance_id: `smartware_${ulid()}`,
    owner_id: ownerId ?? `user:${ulid().toLowerCase()}`,
    writer_id: `writer_local_${ulid()}`,
    version: SMARTWARE_VERSION,
    data_dir: dataDir,
    scopes: [
      { id: POD_SELF_SCOPE, parent: null, visibility_default: 'private' },
      { id: 'workspace', parent: null, visibility_default: 'workspace' },
      { id: 'project:default', parent: 'workspace', visibility_default: 'scope' },
    ],
    grants: [],
    llm: { provider: 'none', model: '' },
    staleness: { default_half_life_days: 90, scope_overrides: { self: 365, 'project:*': 30 }, stale_threshold: 0.3 },
  };

  saveConfig(dataDir, config);

  const dbPath = path.join(dataDir, 'smartware.db');
  new Layer0Index(dbPath).close();
  new ClaimStore(dbPath).close();
  new SearchIndex(dbPath).close();

  writeManifest(path.join(dataDir, 'wiki'), config, {
    layer0: { total: 0, accepted: 0, quarantined: 0, tombstoned: 0 },
    layer1: { claims: 0, entities: 0 },
    layer2: { pages: countWikiPages(path.join(dataDir, 'wiki')) },
  });
  await ensureGitRepo(path.join(dataDir, 'wiki'));
  return config;
}

export * from './config.js';
export * from './layer0/types.js';
// Selective re-export of layer1 types to avoid the ClaimTimeState /
// ClaimTimeValue naming collision with layer0.
export type {
  Claim,
  Entity,
  ClaimStatus,
  ClaimState,
  ClaimType,
  ClaimRole,
  ClaimAuthor,
  ClaimRelation,
  ConfidenceBucket,
  EpistemicTag,
  EpistemicLabel,
  RelationKind,
} from './layer1/types.js';
export {
  statusToState,
  epistemicToTag,
  confidenceToBucket,
  // Host-side claim persistence: a consumer that persists claims itself (rather than relying on
  // the LLM compile path) needs to build ClaimTimeValue fields and to test whether a claim
  // already exists for the same canonical key — otherwise re-observed evidence mints duplicates.
  // These existed in source but were unreachable from the published package.
  knownTime,
  inferredTime,
  nullTime,
  canonicalKey,
  compatibilityValidity,
} from './layer1/types.js';
export * from './layer3/semantic.js';
export * from './layer3/semantic-store.js';
export * from './layer3/temporal.js';
export * from './layer3/hybrid.js';
export * from './evaluation/retrieval.js';
export * from './ops_log/index.js';
export {
  appendClaimVersion,
  claimsJsonlPath,
  iterAllClaimVersions,
  nextVersion as nextClaimVersion,
  readClaimHistory,
  readLatestVersion as readLatestClaimVersion,
  snapshotAt as snapshotClaimAt,
} from './layer1/jsonl.js';
export type { ClaimVersionRecord } from './layer1/jsonl.js';
export {
  ACTOR_ID_PATTERN,
  isSpecConformantActorId,
} from './auth/grants.js';
export {
  evaluateAccess,
} from './auth/middleware.js';
export type { AccessDecision, AccessOperation } from './auth/middleware.js';
export { renderRegistryMarkdown, writeRegistryMarkdown } from './auth/registry-md.js';
export {
  appendAlias,
  ensureDefaultAliases,
  loadAliasMap,
  resolveActorId,
  COFFEE_POD_DEFAULT_ALIASES,
} from './auth/alias-map.js';
export type { AliasEntry } from './auth/alias-map.js';
export {
  CascadePreviewStore,
  DEFAULT_TTL_SECONDS as CASCADE_PREVIEW_TTL_SECONDS,
  PREVIEW_ID_PATTERN,
  isValidCascadePreviewId,
} from './preview_store/index.js';
export type { CascadePreviewPayload, PreviewLookup } from './preview_store/index.js';
export * from './layer4/context-planning.js';
export * from './protocol/observe.js';
export * from './protocol/query.js';
export * from './protocol/compile.js';
export * from './protocol/read.js';
export * from './protocol/explain.js';
export * from './protocol/correct.js';
export * from './protocol/revise.js';
export * from './protocol/endorse.js';
export * from './protocol/forget.js';
export * from './protocol/retention.js';
export * from './protocol/consolidate.js';
export * from './protocol/session.js';
export * from './protocol/status.js';
export * from './protocol/health.js';
export * from './session/types.js';
export * from './session/checkpoint.js';
