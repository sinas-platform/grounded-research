import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';
import { DocumentModal, EvidenceSpan } from '@/components/DocumentViewer';

/* ---------------------------------- types ---------------------------------- */

interface QueryRun {
  id: string;
  question: string;
  reference: string | null;
  tags: string[];
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
  // why the claim rests on the source it cites. The evidence rows carry the
  // opposite: whether the passage carries the sentence.
  rationale: string | null;
  evidence: Evidence[];
}

interface RetrievalPlan {
  queries?: string[];
  anchor_names?: Record<string, string>;
  class_boost?: string[];
  effort?: string;
}

interface ResultFull {
  id: string;
  query: string;
  status: string;
  filter: { plan?: RetrievalPlan; briefing?: unknown[]; retrieval_first?: boolean };
}

/* ------------------------------- stage model ------------------------------- */

type StageState = 'pending' | 'active' | 'done' | 'error';

interface StageNode {
  id: string;
  title: string;
  sub: string;
  state: StageState;
  count?: string;
  /** Shown inside the node as a short preview list (e.g. the searches run). */
  items?: string[];
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

/* ---------------------------------- replay ----------------------------------
   Replays a finished run: each stage lights up at the point in the run where
   it actually finished, scaled to REPLAY_SECONDS.

   Everything shown is stored data, and every boundary below is a timestamp the
   run recorded. Stages the run never timed are not animated — the earlier
   version of this drove the replay off `decompose`/`search`/`merge`, and when
   the pipeline became retrieval-first those keys stopped being written, so the
   replay ran for 30 seconds against an empty diagram. Deriving the timeline
   from the same telemetry the diagram reads keeps the two from drifting apart
   again: if the timestamps are missing, `replayTimeline` returns null and the
   replay button is not offered at all. */

const REPLAY_SECONDS = 30;

interface ReplayTimeline {
  t0: number;
  tEnd: number;
  retrievalDone: number;
  draftStart: number;
  draftDone: number;
}

/** The run's real stage boundaries, or null when it did not record them. */
function replayTimeline(run: QueryRun): ReplayTimeline | null {
  const tel = run.telemetry ?? {};
  const ms = (iso?: string | null) => (iso ? Date.parse(iso) : NaN);
  const t0 = ms(run.started_at ?? run.created_at);
  const retrievalDone = ms(tel.retrieval?.completed);
  const draftStart = ms(tel.draft?.started);
  const draftDone = ms(tel.draft?.completed);
  const tEnd = ms(tel.validate?.published ?? run.completed_at ?? tel.draft?.completed);
  if (!Number.isFinite(t0) || !Number.isFinite(tEnd) || tEnd <= t0) return null;
  // A retrieval-only run has no draft; its timeline is just the retrieval.
  if (!Number.isFinite(retrievalDone)) return null;
  return {
    t0,
    tEnd,
    retrievalDone,
    draftStart: Number.isFinite(draftStart) ? draftStart : tEnd,
    draftDone: Number.isFinite(draftDone) ? draftDone : tEnd,
  };
}

/**
 * The run as it stood `t` (0..1) of the way through, by wall-clock.
 *
 * Documents appear when retrieval completed and claims when drafting
 * completed, because that is the resolution the run recorded — neither
 * carries per-item timings, and inventing them would show a sequence that
 * never happened.
 */
function maskForReplay(
  run: QueryRun,
  activity: RunActivity | undefined,
  docs: ResultDoc[] | undefined,
  claims: ClaimWithEvidence[] | undefined,
  t: number,
  tl: ReplayTimeline,
): { run: QueryRun; activity?: RunActivity; docs?: ResultDoc[]; claims?: ClaimWithEvidence[] } {
  const tel = run.telemetry ?? {};
  const asOf = tl.t0 + t * (tl.tEnd - tl.t0);
  const done = t >= 1;

  const retrieved = asOf >= tl.retrievalDone;
  const extracted = asOf >= tl.draftStart;
  const drafted = asOf >= tl.draftDone;

  const mTel: Record<string, any> = {};
  if (tel.retrieval) {
    // Present but uncompleted reads as the stage running.
    mTel.retrieval = retrieved ? tel.retrieval : {};
  }
  if (tel.extract && extracted) mTel.extract = tel.extract;
  if (tel.draft) {
    if (drafted) mTel.draft = tel.draft;
    else if (extracted) mTel.draft = { started: tel.draft.started, extract_mode: tel.draft.extract_mode };
  }
  if (tel.validate && done) mTel.validate = tel.validate;
  if (tel.partial && done) mTel.partial = tel.partial;
  mTel._replay_elapsed_s = Math.round((asOf - tl.t0) / 1000);

  // Synthesis actions stream across the drafting window (the agent-driven
  // path records them individually); retrieval-first runs have none.
  const draftSpan = Math.max(tl.draftDone - tl.draftStart, 1);
  const draftProgress = Math.min(Math.max((asOf - tl.draftStart) / draftSpan, 0), 1);
  const mActivity: RunActivity | undefined = activity && {
    searches: retrieved ? activity.searches : [],
    synthesis: activity.synthesis
      ? {
          ...activity.synthesis,
          actions: activity.synthesis.actions.slice(
            0,
            Math.floor(activity.synthesis.actions.length * draftProgress),
          ),
        }
      : null,
  };

  return {
    run: {
      ...run,
      status: done ? run.status : 'replaying',
      telemetry: mTel,
      parent_result_id: retrieved ? run.parent_result_id : null,
      answer_id: done ? run.answer_id : null,
    },
    activity: mActivity,
    docs: retrieved ? docs : [],
    claims: drafted ? claims : [],
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
function buildStages(
  run: QueryRun, activity?: RunActivity, docCount?: number, plan?: RetrievalPlan,
): { rows: StageNode[][]; edges: [string, string][] } {
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

  // A synthesis run answers from a result some earlier run retrieved. That
  // retrieval is still readable through the result, so show it here rather
  // than making someone hunt for the upstream run in the list.
  if (run.mode === 'synthesis' && plan?.queries?.length) {
    rows.push([{
      id: 'retrieve', title: 'Retrieved earlier', sub: 'the searches behind this document set',
      state: 'done',
      items: plan.queries,
      count: `${plan.queries.length} searches`,
      wide: true,
    }]);
    edges.push(['query', 'retrieve']);
  }

  if (retrievalFirst) {
    const rDone = !!tel.retrieval?.completed || !!run.parent_result_id;
    const queries = plan?.queries ?? [];
    rows.push([{
      id: 'retrieve', title: 'Retrieve', sub: 'plan the searches, then gather the documents',
      state: rDone ? 'done' : failed ? 'error' : run.status === 'pending' ? 'pending' : 'active',
      items: queries,
      count: tel.retrieval?.documents != null
        ? `${tel.retrieval.documents} documents · ${tel.retrieval.queries ?? queries.length} searches`
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
  const beforeResult = run.mode === 'synthesis'
    ? (plan?.queries?.length ? 'retrieve' : 'query')
    : retrievalFirst ? 'retrieve' : 'merge';
  edges.push([beforeResult, 'result']);

  if (withSynthesis) {
    // the synthesis stage writes telemetry under "draft"
    const sState = stageOf(tel, 'draft');
    const vDone = !!tel.validate?.published;
    const extractMode = !!tel.draft?.extract_mode || !!tel.extract;
    rows.push([{
      id: 'synth', title: 'Synthesis',
      sub: extractMode
        ? 'plan the argument, quote the sources, draft from the quotes'
        : 'draft the answer, cite every claim',
      state: failed && sState === 'active' ? 'error' : partial && sState !== 'done' ? 'done' : sState,
      count: tel.draft?.claims != null
        ? `${tel.draft.claims} claims${tel.extract?.passages_verified != null ? ` · ${tel.extract.passages_verified} verified passages` : ' drafted'}`
        : undefined,
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
  // A document opened for reading, plus the cited passage to highlight when
  // it was opened from a citation rather than the document list.
  const [preview, setPreview] = useState<{ docId: string; span?: EvidenceSpan | null } | null>(null);
  const [question, setQuestion] = useState('');
  const [mode, setMode] = useState<'retrieval' | 'full'>('full');
  const [effort, setEffort] = useState<'low' | 'medium' | 'high'>('medium');
  const [replayT, setReplayT] = useState<number | null>(null); // 0..1 while replaying
  const [listLimit, setListLimit] = useState(25);
  const [tagFilter, setTagFilter] = useState('');
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const runs = useQuery({
    queryKey: ['query-runs', listLimit, tagFilter],
    queryFn: () =>
      api<QueryRun[]>(
        `/query-runs?limit=${listLimit}${tagFilter ? `&tag=${encodeURIComponent(tagFilter)}` : ''}`,
      ),
    refetchInterval: 5000,
    placeholderData: (prev) => prev, // keep the list steady while a longer page loads
  });
  // The list is full when the server returned as many rows as asked for.
  const listMaybeTruncated = (runs.data?.length ?? 0) >= listLimit;

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
  // The retrieval plan (searches run, entity anchors, class boosts) lives on
  // the result's filter payload, so it is readable for every past run too.
  const result = useQuery({
    queryKey: ['result', resultId],
    queryFn: () => api<ResultFull>(`/results/${resultId}`),
    enabled: !!resultId,
  });
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

  // Export uses the authenticated client directly: the response is NDJSON
  // for saving, not JSON for rendering, so the api<T> helper does not fit.
  const [exporting, setExporting] = useState(false);
  const downloadExport = async (path: string, body?: unknown) => {
    setExporting(true);
    try {
      const { client, API_BASE } = await import('@/lib/api');
      const res = await client.fetch(`${API_BASE}${path}`, {
        method: body ? 'POST' : 'GET',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'sgr-review-export.jsonl';
      a.click();
      URL.revokeObjectURL(a.href);
    } finally {
      setExporting(false);
    }
  };

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
  const timeline = useMemo(() => (run.data ? replayTimeline(run.data) : null), [run.data]);
  const canReplay = !!timeline && !!run.data && TERMINAL.has(run.data.status);
  const startReplay = () => setReplayT(0);
  useLayoutEffect(() => {
    if (replayT === null || replayT >= 1) return;
    const id = setTimeout(() => setReplayT((v) => (v === null ? null : Math.min(v + 0.2 / REPLAY_SECONDS, 1))), 200);
    return () => clearTimeout(id);
  }, [replayT]);

  const view = useMemo(() => {
    if (run.data && timeline && replayT !== null && replayT < 1 && TERMINAL.has(run.data.status)) {
      return maskForReplay(run.data, activity.data, docs.data, claims.data, replayT, timeline);
    }
    return { run: run.data ?? undefined, activity: activity.data, docs: docs.data, claims: claims.data };
  }, [run.data, activity.data, docs.data, claims.data, replayT, timeline]);

  const plan = result.data?.filter?.plan;
  const graph = useMemo(
    () => (view.run ? buildStages(view.run, view.activity, view.docs?.length, plan) : null),
    [view, plan],
  );

  const pick = (id: string) => {
    setSelectedId(id);
    setInspected(null);
    setReplayT(null);
  };

  const toggleSel = (id: string) =>
    setSelected((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

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
          className="flex-1 border border-stone-300 rounded-md px-3.5 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-primary-100 focus:border-primary-500"
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
          className="bg-primary-600 hover:bg-primary-700 disabled:opacity-50 text-white rounded-md px-5 py-2 text-sm font-medium"
        >
          {ask.isPending ? 'Starting…' : 'Ask'}
        </button>
      </div>

      <div className="flex gap-4 items-start">
        {/* recent runs — scrolls on its own so the run being inspected stays
            put no matter how far back the history is loaded */}
        <div className="w-56 shrink-0 sticky top-0 max-h-[calc(100vh-280px)] flex flex-col">
          <div className="flex items-center justify-between mb-2 shrink-0">
            <div className="text-[11px] font-semibold text-stone-400 uppercase tracking-wider">Recent runs</div>
            <button
              onClick={() => { setSelecting(!selecting); setSelected(new Set()); }}
              className={`text-[10.5px] font-medium rounded px-1.5 py-0.5 border ${
                selecting ? 'border-primary-500 text-primary-700' : 'border-stone-200 text-stone-500 hover:border-stone-300'
              }`}
            >
              {selecting ? 'Done' : 'Select'}
            </button>
          </div>
          <input
            value={tagFilter}
            onChange={(e) => setTagFilter(e.target.value)}
            placeholder="Filter by tag…"
            className="mb-2 shrink-0 w-full border border-stone-200 rounded px-2 py-1 text-xs bg-white focus:outline-none focus:border-primary-500"
          />
          <div className="space-y-1.5 flex-1 min-h-0 overflow-y-auto pr-1">
            {(runs.data ?? []).map((r) => (
              <button
                key={r.id}
                onClick={() => (selecting ? toggleSel(r.id) : pick(r.id))}
                className={`w-full text-left p-2.5 border rounded-md bg-white transition-colors ${
                  (selecting ? selected.has(r.id) : r.id === runId)
                    ? 'border-primary-500 ring-1 ring-primary-500'
                    : 'border-stone-200 hover:border-stone-300'
                }`}
              >
                <div className="flex items-start gap-1.5">
                  {selecting && (
                    <span
                      className={`mt-0.5 w-3 h-3 shrink-0 rounded-sm border ${
                        selected.has(r.id) ? 'bg-primary-600 border-primary-600' : 'border-stone-300'
                      }`}
                    />
                  )}
                  <div className="text-xs font-medium text-stone-900 line-clamp-2">{r.question}</div>
                </div>
                <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                  <span className="text-[10px] px-1.5 rounded bg-stone-100 text-stone-500">{r.mode}</span>
                  <StatusPill status={r.status} />
                  {r.reference && (
                    <span className="text-[10px] px-1.5 rounded bg-primary-50 text-primary-700 font-mono truncate max-w-full">{r.reference}</span>
                  )}
                  {(r.tags ?? []).slice(0, 2).map((t) => (
                    <span key={t} className="text-[10px] px-1.5 rounded bg-stone-100 text-stone-500">#{t}</span>
                  ))}
                </div>
              </button>
            ))}
            {runs.data?.length === 0 && (
              <div className="text-stone-400 text-xs py-6 text-center border border-dashed border-stone-300 rounded">
                No runs yet — ask something.
              </div>
            )}
            {listMaybeTruncated && (
              <button
                onClick={() => setListLimit((n) => n + 50)}
                className="w-full text-xs text-primary-700 border border-stone-200 hover:border-primary-500 rounded-md py-1.5 font-medium"
              >
                {runs.isFetching ? 'Loading…' : 'Show older runs'}
              </button>
            )}
          </div>
          {selecting && selected.size > 0 && (
            <button
              onClick={() => downloadExport('/query-runs/export', { run_ids: [...selected] })}
              disabled={exporting}
              className="mt-2 shrink-0 w-full text-xs bg-primary-600 hover:bg-primary-700 text-white rounded-md py-1.5 font-medium disabled:opacity-50"
            >
              {exporting ? 'Exporting…' : `Export ${selected.size} selected`}
            </button>
          )}
          {!!tagFilter.trim() && (
            <button
              onClick={() =>
                downloadExport(
                  `/query-runs/export/by-tag?tag=${encodeURIComponent(tagFilter.trim())}&latest_per_reference=true`,
                )
              }
              disabled={exporting}
              title="One document per reference: the newest completed run of each question carrying this tag"
              className="mt-2 shrink-0 w-full text-xs border border-primary-500 text-primary-700 rounded-md py-1.5 font-medium disabled:opacity-50"
            >
              {exporting ? 'Exporting…' : 'Export tag (latest per question)'}
            </button>
          )}
        </div>

        {/* diagram */}
        <div className="w-[340px] shrink-0 sticky top-0 max-h-[calc(100vh-280px)] overflow-y-auto bg-white border border-stone-200 rounded-lg p-4">
          {run.data && TERMINAL.has(run.data.status) && (
            <div className="flex gap-2 mb-3">
              {canReplay && (
                <button
                  onClick={startReplay}
                  title="Replay the run at its real relative pace"
                  className="text-xs border border-stone-300 rounded px-2.5 py-1 text-primary-700 hover:border-primary-500 font-medium"
                >
                  {replayT !== null && replayT < 1 ? 'Replaying…' : '▶ Replay'}
                </button>
              )}
              {run.data.mode === 'retrieval' && run.data.status === 'published' && run.data.parent_result_id && (
                <button
                  onClick={() => synthesize.mutate(run.data!)}
                  disabled={synthesize.isPending}
                  className="text-xs border border-stone-300 rounded px-2.5 py-1 text-primary-700 hover:border-primary-500 font-medium disabled:opacity-50"
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
        <div className="flex-1 min-w-0 bg-white border border-stone-200 rounded-lg sticky top-0 max-h-[calc(100vh-280px)] flex flex-col">
          {view.run && (
            <Inspector
              run={view.run}
              activity={view.activity}
              docs={view.docs}
              claims={view.claims}
              plan={plan}
              inspected={inspected}
              onResume={() => resume.mutate()}
              resuming={resume.isPending}
              onPreviewDoc={(docId, span) => setPreview({ docId, span })}
            />
          )}
        </div>
      </div>

      {preview && (
        <DocumentModal
          docId={preview.docId}
          span={preview.span}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  );
}

/* ------------------------------ subcomponents ------------------------------ */

function StatusPill({ status }: { status: string }) {
  const cls =
    status === 'published'
      ? 'bg-primary-100 text-primary-700'
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
    ? 'bg-primary-500'
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
                inspected === n.id ? 'ring-2 ring-primary-500' : 'hover:border-primary-500'
              }`}
            >
              <div className="flex items-center gap-2 text-xs font-semibold text-stone-900 whitespace-nowrap">
                <span className={`w-2 h-2 rounded-full shrink-0 ${stateDot(n.state)}`} />
                {n.title}
              </div>
              <div className="text-[10.5px] text-stone-500 mt-0.5 truncate">{n.sub}</div>
              {!!n.items?.length && (
                <div className="mt-1 space-y-0.5">
                  {n.items.slice(0, 4).map((it, i) => (
                    <div key={i} className="text-[10px] text-stone-500 truncate border-l-2 border-primary-100 pl-1.5">{it}</div>
                  ))}
                  {n.items.length > 4 && (
                    <div className="text-[10px] text-stone-400 pl-1.5">+{n.items.length - 4} more</div>
                  )}
                </div>
              )}
              {n.count && <div className="text-[10.5px] text-primary-600 font-medium mt-1">{n.count}</div>}
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}

function RunIdentity({ run }: { run: QueryRun }) {
  const qc = useQueryClient();
  const [ref, setRef] = useState(run.reference ?? '');
  const [newTag, setNewTag] = useState('');
  const save = useMutation({
    mutationFn: (patch: { reference?: string | null; tags?: string[] }) =>
      api<QueryRun>(`/query-runs/${run.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['query-runs'] });
      qc.invalidateQueries({ queryKey: ['query-run', run.id] });
    },
  });
  const refDirty = ref.trim() !== (run.reference ?? '');
  const saveRef = () => save.mutate({ reference: ref.trim() || null });
  const addTag = () => {
    const t = newTag.trim();
    setNewTag('');
    if (t && !(run.tags ?? []).includes(t)) save.mutate({ tags: [...(run.tags ?? []), t] });
  };
  const removeTag = (t: string) =>
    save.mutate({ tags: (run.tags ?? []).filter((x) => x !== t) });

  return (
    <>
      <div className="text-[10.5px] font-semibold text-stone-400 uppercase tracking-wider mt-4 mb-1.5">Reference</div>
      <div className="flex gap-1.5">
        <input
          value={ref}
          onChange={(e) => setRef(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && refDirty) saveRef(); }}
          placeholder="e.g. benchmark-q16"
          className="flex-1 min-w-0 border border-stone-300 rounded px-2 py-1 text-xs font-mono bg-white focus:outline-none focus:border-primary-500"
        />
        {refDirty && (
          <button
            onClick={saveRef}
            disabled={save.isPending}
            className="text-xs border border-primary-500 text-primary-700 rounded px-2.5 py-1 font-medium disabled:opacity-50"
          >
            {save.isPending ? '…' : 'Save'}
          </button>
        )}
      </div>
      <div className="text-[10px] text-stone-400 mt-1">Shared by reruns of the same question — not unique.</div>
      <div className="text-[10.5px] font-semibold text-stone-400 uppercase tracking-wider mt-4 mb-1.5">Tags</div>
      <div className="flex flex-wrap gap-1 items-center">
        {(run.tags ?? []).map((t) => (
          <span
            key={t}
            className="text-[10.5px] pl-1.5 pr-0.5 py-0.5 rounded bg-stone-100 text-stone-600 inline-flex items-center gap-0.5"
          >
            #{t}
            <button
              onClick={() => removeTag(t)}
              disabled={save.isPending}
              title="Remove tag"
              className="text-stone-400 hover:text-red-600 px-0.5"
            >
              ×
            </button>
          </span>
        ))}
        <input
          value={newTag}
          onChange={(e) => setNewTag(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') addTag(); }}
          placeholder="+ tag"
          className="w-20 border border-stone-200 rounded px-1.5 py-0.5 text-[10.5px] bg-white focus:outline-none focus:border-primary-500"
        />
      </div>
    </>
  );
}

function Inspector({
  run, activity, docs, claims, plan, inspected, onResume, resuming, onPreviewDoc,
}: {
  run: QueryRun;
  activity?: RunActivity;
  docs?: ResultDoc[];
  claims?: ClaimWithEvidence[];
  plan?: RetrievalPlan;
  inspected: string | null;
  onResume: () => void;
  resuming: boolean;
  onPreviewDoc: (id: string, span?: EvidenceSpan | null) => void;
}) {
  const tel = run.telemetry ?? {};
  const totalActions =
    (activity?.searches.reduce((n, s) => n + s.actions.length, 0) ?? 0) +
    (activity?.synthesis?.actions.length ?? 0);
  const citedPassages = (claims ?? []).reduce((n, c) => n + c.evidence.length, 0);
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
        <RunIdentity key={run.id} run={run} />
        {run.error && (
          <>
            <Label>Error</Label>
            <div className="text-red-700 text-xs bg-red-50 border border-red-200 rounded p-2.5">{run.error}</div>
            {run.status === 'failed' && (
              <button
                onClick={onResume}
                disabled={resuming}
                className="mt-2 text-xs border border-stone-300 rounded px-3 py-1.5 hover:border-primary-500"
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
    const anchors = Object.values(plan?.anchor_names ?? {});
    const upstream = run.mode === 'synthesis';
    body = (
      <>
        <Label>What ran</Label>
        <div className="text-stone-700">
          The question is turned into a set of targeted searches over the corpus, which
          are then run and ranked into one document set — no search agents involved.
          {upstream && ' This run answers from a document set an earlier run retrieved; the plan below is that run’s.'}
        </div>
        <Label>Searches run ({plan?.queries?.length ?? tel.retrieval?.queries ?? 0})</Label>
        {(plan?.queries ?? []).map((q, i) => (
          <div key={i} className="border-l-2 border-primary-100 pl-2.5 py-0.5 mb-1 text-stone-700">{q}</div>
        ))}
        {!plan?.queries?.length && (
          <div className="text-stone-400 italic text-xs">Not recorded for this run.</div>
        )}
        {!!anchors.length && (
          <>
            <Label>Entities the plan anchored on ({anchors.length})</Label>
            <div className="flex flex-wrap gap-1">
              {anchors.map((a) => (
                <span key={a} className="text-[10.5px] px-1.5 py-0.5 rounded bg-stone-100 text-stone-600">{a}</span>
              ))}
            </div>
          </>
        )}
        {!!plan?.class_boost?.length && (
          <>
            <Label>Document classes favoured</Label>
            <div className="flex flex-wrap gap-1">
              {plan.class_boost.map((c) => (
                <span key={c} className="text-[10.5px] px-1.5 py-0.5 rounded bg-primary-50 text-primary-700">{c}</span>
              ))}
            </div>
          </>
        )}
        <Label>Outcome</Label>
        <KV k="Documents kept" v={tel.retrieval?.documents ?? docs?.length ?? '—'} />
        {run.parent_result_id && (
          <KV k="Result" v={<span className="font-mono text-xs text-stone-500">{run.parent_result_id.slice(0, 8)}</span>} />
        )}
        <div className="text-xs text-stone-400 italic mt-2">
          The searches come from a plan that reads the corpus schema first — which
          entities the question names, which document classes are likely to hold the
          answer — so the queries are grounded in what the corpus actually contains.
        </div>
      </>
    );
  } else if (inspected === 'plan') {
    title = 'Plan';
    const subs: string[] = tel.decompose?.subqueries ?? run.subqueries ?? [];
    body = (
      <>
        <Label>Sub-searches (effort: {run.effort})</Label>
        {subs.map((s) => (
          <div key={s} className="border-l-2 border-primary-100 pl-2.5 py-0.5 mb-2 text-stone-700">{s}</div>
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
        <div className="border-l-2 border-primary-100 pl-2.5 text-stone-700 mb-2">{subs[searchIdx]}</div>
        {act?.chat_id && <KV k="Agent chat" v={<span className="font-mono text-xs text-stone-500">{act.chat_id.slice(0, 8)}</span>} />}
        <Label>Actions ({act?.actions.length ?? 0})</Label>
        <div className="font-mono text-[11px] leading-relaxed text-stone-500">
          {(act?.actions ?? []).map((a, i) => (
            <div key={i} className="truncate">
              <span className="inline-block w-6 text-stone-300">{i + 1}</span>
              <span className={a.name.startsWith('add_files') ? 'text-primary-600 font-bold' : 'text-primary-600'}>{a.name}</span>{' '}
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
            <span className="font-mono text-[10.5px] text-primary-600 shrink-0">{d.filename}</span>
            <span className="text-xs text-stone-600 truncate">{d.document_class_name ?? ''}{d.summary ? ` · ${d.summary}` : ''}</span>
          </button>
        ))}
        {!docs?.length && <div className="text-stone-400 italic text-xs">No documents attached yet.</div>}
      </>
    );
  } else if (inspected === 'synth') {
    title = 'Synthesis';
    const acts = activity?.synthesis?.actions ?? [];
    const ex = tel.extract;
    const argPlan: any[] = tel.draft?.argument_plan ?? [];
    const extractMode = !!tel.draft?.extract_mode || !!ex;
    body = extractMode ? (
      <>
        <Label>Approach</Label>
        <div className="text-stone-700 text-xs">
          Three steps, no agent: plan what the answer has to establish, pull verbatim
          passages for each point out of the documents that should carry it, then write
          the answer from those passages alone. Every quote is checked against the
          source text first, so a passage that was not printed in the document never
          reaches the drafter.
        </div>
        {ex && (
          <>
            <Label>Passages</Label>
            <KV k="Documents read" v={ex.documents_read ?? '—'} />
            <KV k="Passages proposed" v={ex.passages_proposed ?? '—'} />
            <KV
              k="Verified verbatim"
              v={
                <span className={ex.passages_verified === ex.passages_proposed ? 'text-primary-700 font-semibold' : 'text-amber-700 font-semibold'}>
                  {ex.passages_verified ?? '—'}
                  {ex.passages_proposed ? ` of ${ex.passages_proposed}` : ''}
                </span>
              }
            />
          </>
        )}
        {!!argPlan.length && (
          <>
            <Label>What the answer set out to establish ({argPlan.length})</Label>
            {argPlan.map((c, i) => (
              <div key={i} className="mb-2">
                <div className="text-[13px] leading-relaxed text-stone-800 border-l-2 border-primary-100 pl-2.5">
                  {c.establishes}
                </div>
                {!!c.anchors?.length && (
                  <div className="pl-2.5 mt-0.5 font-mono text-[10px] text-stone-400">
                    {(c.anchors as string[]).join(' · ')}
                  </div>
                )}
              </div>
            ))}
          </>
        )}
      </>
    ) : (
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
              <span className="text-primary-600">{a.name}</span> <span className="text-stone-400">{a.args}</span>
            </div>
          ))}
          {!acts.length && <div className="text-stone-400 italic">No activity yet…</div>}
        </div>
      </>
    );
  } else if (inspected === 'answer') {
    const isPartial = run.status === 'partial';
    title = isPartial ? 'Partial outcome' : 'Answer';
    const abstentions = (claims ?? []).filter((c) => c.claim_type === 'abstention');
    const verified = (claims ?? []).filter((c) => c.evidence.length > 0 && c.evidence.every((e) => e.validated));
    body = (
      <>
        {isPartial && <PartialNote tel={tel} />}
        <Label>
          {isPartial
            ? `Claims kept · ${claims?.length ?? 0} drafted · ${verified.length} fully verified`
            : `The complete answer · ${claims?.length ?? 0} claims · ${verified.length} fully verified`
              + (abstentions.length ? ` · ${abstentions.length} not established` : '')}
        </Label>
        <div className="space-y-3">
          {(claims ?? []).map((c) => {
            const ok = c.evidence.length > 0 && c.evidence.every((e) => e.validated);
            // An abstention states what the sources do NOT settle. It carries
            // no evidence by nature, so it must not read as an unsupported
            // claim — it is a finding in its own right.
            const abstained = c.claim_type === 'abstention';
            if (abstained) {
              return (
                <div
                  key={c.id}
                  className="text-[13px] leading-relaxed text-stone-700 border-l-2 border-amber-400 bg-amber-50/60 pl-2.5 py-1.5 rounded-r"
                >
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-amber-700 mr-2">
                    not established
                  </span>
                  {c.claim_text}
                </div>
              );
            }
            return (
              <div key={c.id} className="text-[13px] leading-relaxed text-stone-800">
                {c.claim_text}
                <span className="ml-2 inline-flex gap-1 align-baseline">
                  {c.evidence.map((e, i) => (
                    <button
                      key={i}
                      onClick={() => onPreviewDoc(e.document_id, e.span as EvidenceSpan)}
                      title={e.validation_reasoning ?? e.stance}
                      className={`text-[9.5px] font-mono px-1 rounded border ${
                        e.validated
                          ? 'border-primary-100 bg-primary-50 text-primary-700'
                          : 'border-amber-200 bg-amber-50 text-amber-700'
                      }`}
                    >
                      {i + 1}{e.validated ? '✓' : '?'}
                    </button>
                  ))}
                  {ok && <span className="text-[9.5px] text-primary-600 font-semibold">verified</span>}
                </span>
                {c.rationale && (
                  <div className="mt-1 text-[11.5px] leading-relaxed text-stone-500 border-l-2 border-stone-200 pl-2">
                    {c.rationale}
                  </div>
                )}
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
            ? (plan?.queries?.length ? [['Retrieved earlier', `${plan.queries.length} searches`]] : [])
            : !tel.decompose
              ? [['Retrieve', plan?.queries?.length ? `${plan.queries.length} searches` : tel.retrieval?.documents != null ? `${tel.retrieval.documents} documents` : '']]
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
        {/* Chatless drafting takes no agent actions; its unit of work is the
            passage. Runs from before that was recorded fall back to the
            passages actually cited in the answer — a different count, so it
            gets a different label rather than being folded into one. */}
        {tel.extract ? (
          <span>passages <b className="text-stone-900 font-semibold">{tel.extract.passages_verified ?? '—'}</b></span>
        ) : tel.draft?.extract_mode ? (
          <span>cited <b className="text-stone-900 font-semibold">{citedPassages || '—'}</b></span>
        ) : (
          <span>actions <b className="text-stone-900 font-semibold">{totalActions || '—'}</b></span>
        )}
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
