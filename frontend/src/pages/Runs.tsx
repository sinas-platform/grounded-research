import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';

/* ---------------------------------- types ---------------------------------- */

interface QueryRun {
  id: string;
  question: string;
  mode: 'full' | 'retrieval' | 'synthesis';
  effort: 'low' | 'medium' | 'high';
  status: string;
  subqueries: string[] | null;
  parent_result_id: string | null;
  answer_id: string | null;
  error: string | null;
  telemetry: Record<string, any>;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

interface AgentAction {
  name: string;
  args: string;
}

interface SearchActivity {
  subquery: string;
  chat_id: string | null;
  result_id: string | null;
  actions: AgentAction[];
}

interface RunActivity {
  searches: SearchActivity[];
  synthesis: SearchActivity | null;
}

interface ResultDoc {
  document_id: string;
  rank: number | null;
  reason: string | null;
  added_by_agent: string | null;
  filename: string | null;
  document_class_name: string | null;
  summary: string | null;
}

interface Evidence {
  document_id: string;
  span: Record<string, any>;
  stance: string;
  validated: boolean;
  validation_reasoning: string | null;
}

interface ClaimWithEvidence {
  id: string;
  sequence: number;
  claim_text: string;
  claim_type: string | null;
  evidence: Evidence[];
}

interface DocumentFull {
  id: string;
  filename: string;
  title: string | null;
  summary: string | null;
  content: string;
  document_class_id: string | null;
}

/* ------------------------------- stage model ------------------------------- */

type StageState = 'pending' | 'active' | 'done' | 'error';

interface StageNode {
  id: string;
  title: string;
  sub: string;
  state: StageState;
  count?: string;
  wide: boolean;
}

const TERMINAL = new Set(['published', 'failed', 'partial', 'cancelled']);
const isLive = (r?: QueryRun | null) => !!r && !TERMINAL.has(r.status);

function stageOf(tel: Record<string, any>, key: string): StageState {
  const t = tel?.[key];
  // `completed` alone is enough: some stages record only their outcome.
  if (t?.completed) return 'done';
  return t?.started ? 'active' : 'pending';
}

/* ------------------------------- replay masking -------------------------------
   Replays a finished run by re-deriving the view from progressively unmasked
   real data: stage telemetry appears at its true relative time (scaled to
   REPLAY_SECONDS), agent actions and documents stream in across their stage
   windows. No synthetic data — everything shown is the stored run. */

const REPLAY_SECONDS = 30;

function maskForReplay(
  run: QueryRun,
  activity: RunActivity | undefined,
  docs: ResultDoc[] | undefined,
  claims: ClaimWithEvidence[] | undefined,
  t: number, // 0..1 replay progress
): { run: QueryRun; activity?: RunActivity; docs?: ResultDoc[]; claims?: ClaimWithEvidence[] } {
  const tel = run.telemetry ?? {};
  const t0 = Date.parse(tel.decompose?.started ?? run.created_at);
  const endIso =
    tel.validate?.published ?? tel.draft?.completed ?? tel.search?.completed ?? run.created_at;
  const tEnd = Math.max(Date.parse(endIso), t0 + 1);
  const frac = (iso?: string) => (iso ? Math.min(Math.max((Date.parse(iso) - t0) / (tEnd - t0), 0), 1) : 1);

  const fDecEnd = frac(tel.decompose?.completed);
  const fSearchEnd = frac(tel.search?.completed);
  const fDraftEnd = frac(tel.draft?.completed);

  const mTel: Record<string, any> = {};
  if (tel.decompose) {
    mTel.decompose = t >= fDecEnd ? tel.decompose : { started: tel.decompose.started, max_fanout: tel.decompose.max_fanout };
  }
  if (tel.search && t >= fDecEnd) {
    mTel.search = t >= fSearchEnd ? tel.search : { started: tel.search.started, results: {} };
  }
  if (tel.merge && t >= fSearchEnd) mTel.merge = tel.merge;
  if (tel.draft && t >= fSearchEnd) {
    mTel.draft = t >= fDraftEnd ? tel.draft : { started: tel.draft.started };
  }
  if (tel.validate && t >= fDraftEnd && t >= 0.97) mTel.validate = tel.validate;
  mTel._replay_elapsed_s = Math.round((t * (tEnd - t0)) / 1000);

  const searchSpan = Math.max(fSearchEnd - fDecEnd, 0.01);
  const searchProgress = Math.min(Math.max((t - fDecEnd) / searchSpan, 0), 1);
  const mActivity: RunActivity | undefined = activity && {
    searches: activity.searches.map((s) => ({
      ...s,
      result_id: t >= fSearchEnd ? s.result_id : null,
      actions: s.actions.slice(0, Math.floor(s.actions.length * searchProgress)),
    })),
    synthesis: activity.synthesis
      ? {
          ...activity.synthesis,
          actions: activity.synthesis.actions.slice(
            0,
            Math.floor(
              activity.synthesis.actions.length *
                Math.min(Math.max((t - fSearchEnd) / Math.max(fDraftEnd - fSearchEnd, 0.01), 0), 1),
            ),
          ),
        }
      : null,
  };

  const mergeReached = t >= fSearchEnd;
  const mDocs = mergeReached ? docs : docs?.slice(0, Math.floor((docs?.length ?? 0) * searchProgress));
  const draftProgress = Math.min(Math.max((t - fSearchEnd) / Math.max(fDraftEnd - fSearchEnd, 0.01), 0), 1);
  const mClaims = t >= 0.97 ? claims : claims?.slice(0, Math.floor((claims?.length ?? 0) * draftProgress));

  return {
    run: {
      ...run,
      status: t >= 1 ? run.status : 'replaying',
      telemetry: mTel,
      parent_result_id: run.mode === 'synthesis' || mergeReached ? run.parent_result_id : null,
      answer_id: t >= 0.97 ? run.answer_id : null,
    },
    activity: mActivity,
    docs: mDocs,
    claims: mClaims,
  };
}

function fmtDuration(start?: string, end?: string): string {
  if (!start) return '';
  const ms = (end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime();
  if (ms < 0 || Number.isNaN(ms)) return '';
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** Derive the diagram from a run + its activity. */
function buildStages(run: QueryRun, activity?: RunActivity, docCount?: number): { rows: StageNode[][]; edges: [string, string][] } {
  const tel = run.telemetry ?? {};
  const failed = run.status === 'failed';
  const published = run.status === 'published';
  const partial = run.status === 'partial';
  const withSynthesis = run.mode !== 'retrieval';

  const subqueries: string[] =
    tel.decompose?.subqueries ?? run.subqueries ?? activity?.searches.map((s) => s.subquery) ?? [];

  const planState: StageState =
    run.mode === 'synthesis' ? 'done' : stageOf(tel, 'decompose');
  const searchState = stageOf(tel, 'search');
  // merge telemetry is written once, on completion — presence means done.
  // Single-search runs adopt the child result directly and write none.
  const mergeState: StageState =
    tel.merge || run.parent_result_id ? 'done' : searchState === 'done' ? 'active' : 'pending';

  const rows: StageNode[][] = [];
  const edges: [string, string][] = [];

  rows.push([{ id: 'query', title: 'Question', sub: new Date(run.created_at).toLocaleString(), state: 'done', wide: true }]);

  // Retrieval-first runs write a single "retrieval" telemetry entry; runs from
  // the retired agent-driven path carry "decompose" and keep the old diagram.
  const retrievalFirst = run.mode !== 'synthesis' && !tel.decompose;

  if (retrievalFirst) {
    const rDone = !!tel.retrieval?.completed || !!run.parent_result_id;
    rows.push([{
      id: 'retrieve', title: 'Retrieve', sub: 'plan the searches, then gather the documents',
      state: rDone ? 'done' : failed ? 'error' : run.status === 'pending' ? 'pending' : 'active',
      count: tel.retrieval?.documents != null
        ? `${tel.retrieval.documents} documents · ${tel.retrieval.queries ?? 0} queries`
        : undefined,
      wide: true,
    }]);
    edges.push(['query', 'retrieve']);
  } else if (run.mode !== 'synthesis') {
    rows.push([{
      id: 'plan', title: 'Plan', sub: 'split into focused searches', state: planState,
      count: subqueries.length ? `${subqueries.length} sub-search${subqueries.length > 1 ? 'es' : ''}` : undefined,
      wide: true,
    }]);
    edges.push(['query', 'plan']);

    const searchRow: StageNode[] = subqueries.map((sq, i) => {
      const act = activity?.searches.find((s) => s.subquery === sq);
      const done = !!act?.result_id && (searchState === 'done' || mergeState !== 'pending');
      return {
        id: `ss${i}`, title: `Search ${i + 1}`, sub: sq,
        state: failed && searchState === 'active' ? 'error' : done ? 'done' : searchState,
        count: act?.actions.length ? `${act.actions.length} actions` : undefined,
        wide: false,
      };
    });
    if (searchRow.length) {
      rows.push(searchRow);
      searchRow.forEach((n) => { edges.push(['plan', n.id]); edges.push([n.id, 'merge']); });
    } else {
      edges.push(['plan', 'merge']);
    }

    const mc = tel.merge;
    rows.push([{
      id: 'merge', title: 'Consolidate', sub: 'combine & de-duplicate', state: mergeState,
      count: mc?.total_documents != null ? `${mc.total_documents} documents kept` : undefined,
      wide: true,
    }]);
  }

  rows.push([{
    id: 'result', title: 'Result', sub: 'the document set',
    state: run.parent_result_id ? 'done' : 'pending',
    count: docCount != null && run.parent_result_id ? `${docCount} documents${run.mode === 'retrieval' && published ? ' · published' : ''}` : undefined,
    wide: true,
  }]);
  edges.push([run.mode === 'synthesis' ? 'query' : retrievalFirst ? 'retrieve' : 'merge', 'result']);

  if (withSynthesis) {
    // the synthesis stage writes telemetry under "draft"
    const sState = stageOf(tel, 'draft');
    const vDone = !!tel.validate?.published;
    rows.push([{
      id: 'synth', title: 'Synthesis', sub: 'draft the answer, cite every claim',
      state: failed && sState === 'active' ? 'error' : partial && sState !== 'done' ? 'done' : sState,
      count: tel.draft?.claims != null ? `${tel.draft.claims} claims drafted` : undefined,
      wide: true,
    }]);
    rows.push([{
      id: 'answer',
      title: partial ? 'Partial outcome' : 'Answer',
      sub: partial ? 'no full answer — sources and any verified claims kept' : 'every claim checked against sources',
      state: partial ? 'done'
        : published && run.answer_id ? 'done'
          : vDone ? 'done'
            : tel.validate ? 'active'
              : run.answer_id ? 'active' : 'pending',
      count: partial && tel.partial?.validated_claims != null
        ? `${tel.partial.validated_claims} verified claims`
        : undefined,
      wide: true,
    }]);
    edges.push(['result', 'synth'], ['synth', 'answer']);
  }

  return { rows, edges };
}

/* --------------------------------- page --------------------------------- */

// Deep-link support: /runs#run=<id>&node=<stage> selects a run (and optionally
// an inspected stage) on load — useful for sharing a specific run.
const hashParam = (key: string) =>
  new URLSearchParams(window.location.hash.slice(1)).get(key);

export default function RunsPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(() => hashParam('run'));
  const [inspected, setInspected] = useState<string | null>(() => hashParam('node'));
  const [previewDocId, setPreviewDocId] = useState<string | null>(null);
  const [question, setQuestion] = useState('');
  const [mode, setMode] = useState<'retrieval' | 'full'>('retrieval');
  const [effort, setEffort] = useState<'low' | 'medium' | 'high'>('medium');
  const [replayT, setReplayT] = useState<number | null>(null); // 0..1 while replaying

  const runs = useQuery({
    queryKey: ['query-runs'],
    queryFn: () => api<QueryRun[]>('/query-runs?limit=25'),
    refetchInterval: 5000,
  });

  const runId = selectedId ?? runs.data?.[0]?.id ?? null;

  const run = useQuery({
    queryKey: ['query-run', runId],
    queryFn: () => api<QueryRun>(`/query-runs/${runId}`),
    enabled: !!runId,
    refetchInterval: (q) => (isLive(q.state.data) ? 2500 : false),
  });

  const activity = useQuery({
    queryKey: ['query-run-activity', runId],
    queryFn: () => api<RunActivity>(`/query-runs/${runId}/activity`),
    enabled: !!runId,
    refetchInterval: isLive(run.data) ? 3000 : false,
  });

  const resultId = run.data?.parent_result_id ?? null;
  const docs = useQuery({
    queryKey: ['result-docs', resultId],
    queryFn: () => api<ResultDoc[]>(`/results/${resultId}/documents`),
    enabled: !!resultId,
    refetchInterval: isLive(run.data) ? 4000 : false,
  });

  const answerId = run.data?.answer_id ?? null;
  const claims = useQuery({
    queryKey: ['answer-evidence', answerId],
    queryFn: () => api<ClaimWithEvidence[]>(`/answers/${answerId}/evidence`),
    enabled: !!answerId,
    refetchInterval: isLive(run.data) ? 5000 : false,
  });

  const previewDoc = useQuery({
    queryKey: ['doc-preview', previewDocId],
    queryFn: () => api<DocumentFull>(`/documents/${previewDocId}`),
    enabled: !!previewDocId,
  });

  const ask = useMutation({
    mutationFn: () =>
      api<QueryRun>('/query-runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, mode, effort }),
      }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ['query-runs'] });
      setSelectedId(created.id);
      setInspected(null);
      setQuestion('');
    },
  });

  const resume = useMutation({
    mutationFn: () => api<QueryRun>(`/query-runs/${runId}/resume`, { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['query-run', runId] }),
  });

  const synthesize = useMutation({
    mutationFn: (from: QueryRun) =>
      api<QueryRun>('/query-runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: from.question,
          mode: 'synthesis',
          effort: from.effort,
          parent_result_id: from.parent_result_id,
        }),
      }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ['query-runs'] });
      setSelectedId(created.id);
      setInspected(null);
    },
  });

  // replay clock
  const startReplay = () => setReplayT(0);
  useLayoutEffect(() => {
    if (replayT === null || replayT >= 1) return;
    const id = setTimeout(() => setReplayT((v) => (v === null ? null : Math.min(v + 0.2 / REPLAY_SECONDS, 1))), 200);
    return () => clearTimeout(id);
  }, [replayT]);

  const view = useMemo(() => {
    if (run.data && replayT !== null && replayT < 1 && TERMINAL.has(run.data.status)) {
      return maskForReplay(run.data, activity.data, docs.data, claims.data, replayT);
    }
    return { run: run.data ?? undefined, activity: activity.data, docs: docs.data, claims: claims.data };
  }, [run.data, activity.data, docs.data, claims.data, replayT]);

  const graph = useMemo(
    () => (view.run ? buildStages(view.run, view.activity, view.docs?.length) : null),
    [view],
  );

  const pick = (id: string) => {
    setSelectedId(id);
    setInspected(null);
    setReplayT(null);
  };

  return (
    <div>
      <PageHeader
        title="Runs"
        description="Ask a question and watch the engine work — planning, parallel search, consolidation, and (in full mode) a verified answer. Click any step to inspect it."
      />

      {/* ask row */}
      <div className="flex gap-2 mb-6">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && question.trim().length >= 8) ask.mutate(); }}
          placeholder="Ask a research question…"
          className="flex-1 border border-stone-300 rounded-md px-3.5 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-forest-100 focus:border-forest-500"
        />
        <select value={mode} onChange={(e) => setMode(e.target.value as any)}
          className="border border-stone-300 rounded-md px-2 py-2 text-sm bg-white text-stone-700">
          <option value="retrieval">Retrieve</option>
          <option value="full">Retrieve + synthesize</option>
        </select>
        <select value={effort} onChange={(e) => setEffort(e.target.value as any)}
          className="border border-stone-300 rounded-md px-2 py-2 text-sm bg-white text-stone-700">
          <option value="low">Effort · low</option>
          <option value="medium">Effort · medium</option>
          <option value="high">Effort · high</option>
        </select>
        <button
          onClick={() => ask.mutate()}
          disabled={question.trim().length < 8 || ask.isPending}
          className="bg-forest-600 hover:bg-forest-700 disabled:opacity-50 text-white rounded-md px-5 py-2 text-sm font-medium"
        >
          {ask.isPending ? 'Starting…' : 'Ask'}
        </button>
      </div>

      <div className="flex gap-4 items-start">
        {/* recent runs */}
        <div className="w-56 shrink-0">
          <div className="text-[11px] font-semibold text-stone-400 uppercase tracking-wider mb-2">Recent runs</div>
          <div className="space-y-1.5">
            {(runs.data ?? []).map((r) => (
              <button
                key={r.id}
                onClick={() => pick(r.id)}
                className={`w-full text-left p-2.5 border rounded-md bg-white transition-colors ${
                  r.id === runId ? 'border-forest-500 ring-1 ring-forest-500' : 'border-stone-200 hover:border-stone-300'
                }`}
              >
                <div className="text-xs font-medium text-stone-900 line-clamp-2">{r.question}</div>
                <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                  <span className="text-[10px] px-1.5 rounded bg-stone-100 text-stone-500">{r.mode}</span>
                  <StatusPill status={r.status} />
                </div>
              </button>
            ))}
            {runs.data?.length === 0 && (
              <div className="text-stone-400 text-xs py-6 text-center border border-dashed border-stone-300 rounded">
                No runs yet — ask something.
              </div>
            )}
          </div>
        </div>

        {/* diagram */}
        <div className="w-[340px] shrink-0 bg-white border border-stone-200 rounded-lg p-4">
          {run.data && TERMINAL.has(run.data.status) && (
            <div className="flex gap-2 mb-3">
              <button
                onClick={startReplay}
                className="text-xs border border-stone-300 rounded px-2.5 py-1 text-forest-700 hover:border-forest-500 font-medium"
              >
                {replayT !== null && replayT < 1 ? 'Replaying…' : '▶ Replay'}
              </button>
              {run.data.mode === 'retrieval' && run.data.status === 'published' && run.data.parent_result_id && (
                <button
                  onClick={() => synthesize.mutate(run.data!)}
                  disabled={synthesize.isPending}
                  className="text-xs border border-stone-300 rounded px-2.5 py-1 text-forest-700 hover:border-forest-500 font-medium disabled:opacity-50"
                >
                  {synthesize.isPending ? 'Starting…' : 'Synthesize answer →'}
                </button>
              )}
            </div>
          )}
          {graph ? (
            <FlowDiagram
              rows={graph.rows}
              edges={graph.edges}
              inspected={inspected}
              onInspect={setInspected}
            />
          ) : (
            <div className="text-stone-400 text-sm py-16 text-center">Select a run.</div>
          )}
        </div>

        {/* inspector panel */}
        <div className="flex-1 min-w-0 bg-white border border-stone-200 rounded-lg sticky top-4 max-h-[calc(100vh-120px)] flex flex-col">
          {view.run && (
            <Inspector
              run={view.run}
              activity={view.activity}
              docs={view.docs}
              claims={view.claims}
              inspected={inspected}
              onResume={() => resume.mutate()}
              resuming={resume.isPending}
              onPreviewDoc={setPreviewDocId}
            />
          )}
        </div>
      </div>

      {/* document preview modal */}
      {previewDocId && (
        <div
          className="fixed inset-0 z-50 bg-stone-900/40 flex items-center justify-center p-8"
          onClick={() => setPreviewDocId(null)}
        >
          <div
            className="bg-white rounded-lg shadow-xl max-w-3xl w-full max-h-[85vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-baseline gap-3 px-5 py-3.5 border-b border-stone-200">
              <div className="font-mono text-xs text-forest-600">{previewDoc.data?.filename ?? '…'}</div>
              <div className="text-sm font-semibold text-stone-900 truncate">{previewDoc.data?.title ?? ''}</div>
              <button
                onClick={() => setPreviewDocId(null)}
                className="ml-auto text-stone-400 hover:text-stone-900 text-lg leading-none"
              >
                ×
              </button>
            </div>
            <div className="overflow-y-auto px-5 py-4">
              {previewDoc.data?.summary && (
                <p className="text-sm text-stone-600 italic border-l-2 border-forest-100 pl-3 mb-4">
                  {previewDoc.data.summary}
                </p>
              )}
              <pre className="whitespace-pre-wrap text-xs leading-relaxed text-stone-700 font-sans">
                {previewDoc.data ? previewDoc.data.content?.slice(0, 20000) : 'Loading…'}
                {previewDoc.data && (previewDoc.data.content?.length ?? 0) > 20000 && '\n\n… (truncated preview)'}
              </pre>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ------------------------------ subcomponents ------------------------------ */

function StatusPill({ status }: { status: string }) {
  const cls =
    status === 'published'
      ? 'bg-forest-100 text-forest-700'
      : status === 'failed'
        ? 'bg-red-50 text-red-700 border border-red-200'
        : status === 'partial'
          ? 'bg-orange-50 text-orange-700 border border-orange-200'
          : status === 'cancelled'
            ? 'bg-stone-100 text-stone-500 border border-stone-200'
            : 'bg-amber-50 text-amber-700 border border-amber-200';
  return <span className={`text-[10px] px-1.5 rounded ${cls}`}>{status}</span>;
}

/** The client-facing note a partial run ends with, plus why it stopped. */
function PartialNote({ tel }: { tel: Record<string, any> }) {
  const p = tel?.partial;
  if (!p) return null;
  const cause: Record<string, string> = {
    budget_ceiling: 'the run reached its spend ceiling',
    coverage: 'the sources found did not cover the question',
    no_progress: 'drafting produced too little to stand behind',
  };
  return (
    <div className="mb-3 rounded border border-orange-200 bg-orange-50 p-2.5">
      <div className="text-[10.5px] font-semibold text-orange-700 uppercase tracking-wider mb-1">
        Partial outcome — {cause[p.cause] ?? p.cause}
      </div>
      {p.message && <div className="text-[13px] leading-relaxed text-stone-800 whitespace-pre-wrap">{p.message}</div>}
      {!!p.sources?.length && (
        <div className="mt-2 text-xs text-stone-600">
          <div className="font-semibold mb-0.5">Sources identified</div>
          {(p.sources as string[]).map((s) => (
            <div key={s} className="truncate font-mono text-[11px] text-stone-500">{s}</div>
          ))}
        </div>
      )}
    </div>
  );
}

function stateDot(state: StageState) {
  return state === 'done'
    ? 'bg-forest-500'
    : state === 'active'
      ? 'bg-amber-600 animate-pulse'
      : state === 'error'
        ? 'bg-red-600'
        : 'bg-stone-300';
}

function FlowDiagram({
  rows, edges, inspected, onInspect,
}: {
  rows: StageNode[][];
  edges: [string, string][];
  inspected: string | null;
  onInspect: (id: string) => void;
}) {
  const flowRef = useRef<HTMLDivElement>(null);
  const [paths, setPaths] = useState<string[]>([]);

  // Draw edges after layout; ResizeObserver keeps them attached on any size change.
  useLayoutEffect(() => {
    const el = flowRef.current;
    if (!el) return;
    const draw = () => {
      const fr = el.getBoundingClientRect();
      const next: string[] = [];
      for (const [a, b] of edges) {
        const na = el.querySelector(`[data-node="${a}"]`);
        const nb = el.querySelector(`[data-node="${b}"]`);
        if (!na || !nb) continue;
        const ra = na.getBoundingClientRect();
        const rb = nb.getBoundingClientRect();
        const x1 = ra.left + ra.width / 2 - fr.left;
        const y1 = ra.bottom - fr.top;
        const x2 = rb.left + rb.width / 2 - fr.left;
        const y2 = rb.top - fr.top;
        const my = (y1 + y2) / 2;
        next.push(`M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`);
      }
      setPaths(next);
    };
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(el);
    return () => ro.disconnect();
  }, [rows, edges]);

  return (
    <div ref={flowRef} className="relative flex flex-col gap-6 items-center">
      <svg className="absolute inset-0 pointer-events-none overflow-visible w-full h-full">
        {paths.map((d, i) => (
          <path key={i} d={d} fill="none" stroke="#d6d3d1" strokeWidth="1.5" />
        ))}
      </svg>
      {rows.map((row, ri) => (
        <div key={ri} className="flex gap-3 justify-center relative z-10 w-full">
          {row.map((n) => (
            <button
              key={n.id}
              data-node={n.id}
              onClick={() => onInspect(n.id)}
              className={`text-left border rounded-lg bg-white px-3 py-2 transition-all ${
                n.wide ? 'w-72' : 'flex-1 min-w-0'
              } ${n.state === 'pending' ? 'border-dashed border-stone-300 opacity-60' : 'border-stone-200'} ${
                inspected === n.id ? 'ring-2 ring-forest-500' : 'hover:border-forest-500'
              }`}
            >
              <div className="flex items-center gap-2 text-xs font-semibold text-stone-900 whitespace-nowrap">
                <span className={`w-2 h-2 rounded-full shrink-0 ${stateDot(n.state)}`} />
                {n.title}
              </div>
              <div className="text-[10.5px] text-stone-500 mt-0.5 truncate">{n.sub}</div>
              {n.count && <div className="text-[10.5px] text-forest-600 font-medium mt-1">{n.count}</div>}
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}

function Inspector({
  run, activity, docs, claims, inspected, onResume, resuming, onPreviewDoc,
}: {
  run: QueryRun;
  activity?: RunActivity;
  docs?: ResultDoc[];
  claims?: ClaimWithEvidence[];
  inspected: string | null;
  onResume: () => void;
  resuming: boolean;
  onPreviewDoc: (id: string) => void;
}) {
  const tel = run.telemetry ?? {};
  const totalActions =
    (activity?.searches.reduce((n, s) => n + s.actions.length, 0) ?? 0) +
    (activity?.synthesis?.actions.length ?? 0);
  const elapsed =
    tel._replay_elapsed_s != null
      ? `${Math.floor(tel._replay_elapsed_s / 60)}:${String(tel._replay_elapsed_s % 60).padStart(2, '0')}`
      : fmtDuration(
          run.started_at ?? tel.decompose?.started ?? run.created_at,
          run.completed_at ?? undefined,
        );

  const Label = ({ children }: { children: React.ReactNode }) => (
    <div className="text-[10.5px] font-semibold text-stone-400 uppercase tracking-wider mt-4 first:mt-0 mb-1.5">{children}</div>
  );

  let title = 'Run overview';
  let body: React.ReactNode = null;

  const searchIdx = inspected?.startsWith('ss') ? Number(inspected.slice(2)) : null;

  if (inspected === 'query') {
    title = 'Question';
    body = (
      <>
        <Label>Text</Label>
        <div className="text-stone-700">{run.question}</div>
        <Label>Settings</Label>
        <KV k="Mode" v={run.mode} />
        <KV k="Effort" v={run.effort} />
        <KV k="Run" v={<span className="font-mono text-xs text-stone-500">{run.id.slice(0, 8)}</span>} />
        {run.error && (
          <>
            <Label>Error</Label>
            <div className="text-red-700 text-xs bg-red-50 border border-red-200 rounded p-2.5">{run.error}</div>
            {run.status === 'failed' && (
              <button
                onClick={onResume}
                disabled={resuming}
                className="mt-2 text-xs border border-stone-300 rounded px-3 py-1.5 hover:border-forest-500"
              >
                {resuming ? 'Resuming…' : '↻ Resume run'}
              </button>
            )}
          </>
        )}
      </>
    );
  } else if (inspected === 'retrieve') {
    title = 'Retrieval';
    body = (
      <>
        <Label>What ran</Label>
        <div className="text-stone-700">
          The question is turned into a set of targeted searches over the corpus, which
          are then run and ranked into one document set — no search agents involved.
        </div>
        <Label>Outcome</Label>
        <KV k="Searches run" v={tel.retrieval?.queries ?? '—'} />
        <KV k="Documents kept" v={tel.retrieval?.documents ?? '—'} />
        {run.parent_result_id && (
          <KV k="Result" v={<span className="font-mono text-xs text-stone-500">{run.parent_result_id.slice(0, 8)}</span>} />
        )}
        {tel.retrieval?.completed && (
          <div className="text-xs text-stone-400 italic mt-2">
            Retrieval is stored as its own result — it can be reopened, re-scored, or
            re-used as the input to a separate synthesis run.
          </div>
        )}
      </>
    );
  } else if (inspected === 'plan') {
    title = 'Plan';
    const subs: string[] = tel.decompose?.subqueries ?? run.subqueries ?? [];
    body = (
      <>
        <Label>Sub-searches (effort: {run.effort})</Label>
        {subs.map((s) => (
          <div key={s} className="border-l-2 border-forest-100 pl-2.5 py-0.5 mb-2 text-stone-700">{s}</div>
        ))}
        {tel.decompose?.completed && (
          <div className="text-xs text-stone-400 italic mt-2">
            Planned in {fmtDuration(tel.decompose.started, tel.decompose.completed)}, then dispatched in parallel.
          </div>
        )}
      </>
    );
  } else if (searchIdx != null) {
    const subs: string[] = tel.decompose?.subqueries ?? run.subqueries ?? [];
    const act = activity?.searches.find((s) => s.subquery === subs[searchIdx]);
    title = `Search ${searchIdx + 1}`;
    body = (
      <>
        <Label>Sub-search</Label>
        <div className="border-l-2 border-forest-100 pl-2.5 text-stone-700 mb-2">{subs[searchIdx]}</div>
        {act?.chat_id && <KV k="Agent chat" v={<span className="font-mono text-xs text-stone-500">{act.chat_id.slice(0, 8)}</span>} />}
        <Label>Actions ({act?.actions.length ?? 0})</Label>
        <div className="font-mono text-[11px] leading-relaxed text-stone-500">
          {(act?.actions ?? []).map((a, i) => (
            <div key={i} className="truncate">
              <span className="inline-block w-6 text-stone-300">{i + 1}</span>
              <span className={a.name.startsWith('add_files') ? 'text-forest-600 font-bold' : 'text-forest-600'}>{a.name}</span>{' '}
              <span className="text-stone-400">{a.args}</span>
            </div>
          ))}
          {!act?.actions.length && <div className="text-stone-400 italic">No activity yet…</div>}
        </div>
      </>
    );
  } else if (inspected === 'merge') {
    title = 'Consolidation';
    const mc = tel.merge;
    body = mc ? (
      <>
        <Label>Sources combined</Label>
        {Object.entries(mc.per_child ?? {}).map(([rid, info]: [string, any]) => (
          <KV
            key={rid}
            k={<span className="font-mono text-xs">{rid.slice(0, 8)}</span>}
            v={`${info.documents} docs · ${info.added} kept`}
          />
        ))}
        <Label>Outcome</Label>
        <KV k="Combined, de-duplicated" v={`${mc.total_documents} documents`} />
        <div className="text-xs text-stone-400 italic mt-2">
          Every merge is recorded with full provenance — which search contributed which document.
        </div>
      </>
    ) : run.parent_result_id ? (
      <div className="text-stone-500 text-xs">
        Single search — its result became the run's result directly; no merge was needed.
      </div>
    ) : (
      <div className="text-stone-400 italic text-xs">Waiting for searches to publish…</div>
    );
  } else if (inspected === 'result') {
    title = 'Result — documents';
    body = (
      <>
        <Label>{docs?.length ?? 0} documents · click to preview</Label>
        {(docs ?? []).map((d) => (
          <button
            key={d.document_id}
            onClick={() => onPreviewDoc(d.document_id)}
            className="w-full text-left flex gap-2 items-baseline py-1 border-b border-stone-100 hover:bg-stone-50"
          >
            <span className="font-mono text-[10.5px] text-forest-600 shrink-0">{d.filename}</span>
            <span className="text-xs text-stone-600 truncate">{d.document_class_name ?? ''}{d.summary ? ` · ${d.summary}` : ''}</span>
          </button>
        ))}
        {!docs?.length && <div className="text-stone-400 italic text-xs">No documents attached yet.</div>}
      </>
    );
  } else if (inspected === 'synth') {
    title = 'Synthesis';
    const acts = activity?.synthesis?.actions ?? [];
    body = (
      <>
        <Label>Approach</Label>
        <div className="text-stone-700 text-xs">
          Reads the consolidated result, drafts a structured memo, and binds every claim to the exact passages that support it.
        </div>
        <Label>Agent actions ({acts.length})</Label>
        <div className="font-mono text-[11px] leading-relaxed text-stone-500">
          {acts.map((a, i) => (
            <div key={i} className="truncate">
              <span className="inline-block w-6 text-stone-300">{i + 1}</span>
              <span className="text-forest-600">{a.name}</span> <span className="text-stone-400">{a.args}</span>
            </div>
          ))}
          {!acts.length && <div className="text-stone-400 italic">No activity yet…</div>}
        </div>
      </>
    );
  } else if (inspected === 'answer') {
    const isPartial = run.status === 'partial';
    title = isPartial ? 'Partial outcome' : 'Answer';
    const verified = (claims ?? []).filter((c) => c.evidence.length > 0 && c.evidence.every((e) => e.validated));
    body = (
      <>
        {isPartial && <PartialNote tel={tel} />}
        <Label>
          {isPartial
            ? `Claims kept · ${claims?.length ?? 0} drafted · ${verified.length} fully verified`
            : `The complete answer · ${claims?.length ?? 0} claims · ${verified.length} fully verified`}
        </Label>
        <div className="space-y-3">
          {(claims ?? []).map((c) => {
            const ok = c.evidence.length > 0 && c.evidence.every((e) => e.validated);
            return (
              <div key={c.id} className="text-[13px] leading-relaxed text-stone-800">
                {c.claim_text}
                <span className="ml-2 inline-flex gap-1 align-baseline">
                  {c.evidence.map((e, i) => (
                    <button
                      key={i}
                      onClick={() => onPreviewDoc(e.document_id)}
                      title={e.validation_reasoning ?? e.stance}
                      className={`text-[9.5px] font-mono px-1 rounded border ${
                        e.validated
                          ? 'border-forest-100 bg-forest-50 text-forest-700'
                          : 'border-amber-200 bg-amber-50 text-amber-700'
                      }`}
                    >
                      {i + 1}{e.validated ? '✓' : '?'}
                    </button>
                  ))}
                  {ok && <span className="text-[9.5px] text-forest-600 font-semibold">verified</span>}
                </span>
              </div>
            );
          })}
        </div>
        {!claims?.length && <div className="text-stone-400 italic text-xs">No answer yet.</div>}
      </>
    );
  } else {
    // overview
    body = (
      <>
        {run.status === 'partial' && <PartialNote tel={tel} />}
        <Label>Stages</Label>
        {[
          ['Question', ''],
          ...(run.mode === 'synthesis'
            ? []
            : !tel.decompose
              ? [['Retrieve', tel.retrieval?.documents != null ? `${tel.retrieval.documents} documents` : '']]
              : [
                ['Plan', tel.decompose?.subqueries ? `${tel.decompose.subqueries.length} sub-search${tel.decompose.subqueries.length > 1 ? 'es' : ''}` : ''],
                ...((tel.decompose?.subqueries ?? run.subqueries ?? []) as string[]).map((sq: string, i: number) => {
                  const act = activity?.searches.find((s) => s.subquery === sq);
                  return [`Search ${i + 1}`, act ? `${act.actions.length} actions` : ''];
                }),
                ['Consolidate', tel.merge ? `${tel.merge.total_documents} documents` : ''],
              ]),
          ['Result', docs ? `${docs.length} documents` : ''],
          ...(run.mode !== 'retrieval'
            ? [
                ['Synthesis', activity?.synthesis ? `${activity.synthesis.actions.length} actions` : ''],
                ['Answer', claims?.length ? `${claims.length} claims` : ''],
              ]
            : []),
        ].map(([label, extra], i) => (
          <div key={i} className="flex items-center justify-between py-1 text-stone-700">
            <span>{label}</span>
            <span className="text-xs text-stone-500 font-medium">{extra}</span>
          </div>
        ))}
        <div className="text-xs text-stone-400 italic mt-3">
          Every stage is stored — runs can be reopened, inspected, and re-scored later. Click a step in the diagram to inspect it.
        </div>
      </>
    );
  }

  return (
    <>
      <div className="flex items-baseline gap-2.5 px-4 py-3 border-b border-stone-200">
        <h3 className="text-sm font-semibold text-stone-900">{title}</h3>
        {inspected && (
          <span className="text-xs text-stone-400">of run {run.id.slice(0, 8)}</span>
        )}
      </div>
      <div className="flex gap-4 px-4 py-2.5 border-b border-stone-200 text-xs text-stone-500 flex-wrap">
        <span>status <b className="text-stone-900 font-semibold">{run.status}</b></span>
        <span>docs <b className="text-stone-900 font-semibold">{docs?.length ?? '—'}</b></span>
        <span>actions <b className="text-stone-900 font-semibold">{totalActions || '—'}</b></span>
        <span>elapsed <b className="text-stone-900 font-semibold">{elapsed || '—'}</b></span>
        <span className="text-[10px] px-1.5 rounded bg-stone-100 text-stone-500 self-center">{run.mode} · {run.effort}</span>
        <StatusPill status={run.status} />
      </div>
      <div className="overflow-y-auto px-4 py-3 text-sm">{body}</div>
    </>
  );
}

function KV({ k, v }: { k: React.ReactNode; v: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3 py-0.5 text-stone-600 text-[13px]">
      <span>{k}</span>
      <span className="text-stone-900 font-medium text-right">{v}</span>
    </div>
  );
}
