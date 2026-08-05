import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';
import {
  ErrorBanner,
  Field,
  PrimaryButton,
  SecondaryButton,
  inputClasses,
} from '@/components/Form';

interface PartDesc {
  key: string;
  label: string;
}

interface DocumentClass {
  id: string;
  slug: string;
  name: string;
}

interface Run {
  id: string;
  status: string;
  parts: string[];
  filter: Record<string, unknown>;
  total_units: number;
  done_units: number;
  failed_units: number;
  started_by: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
}

interface CreateResult {
  run_id: string | null;
  document_count: number;
  unit_count: number;
  status: string;
}

export default function IngestionRunsPage() {
  const [creating, setCreating] = useState(false);
  const runs = useQuery({
    queryKey: ['ingestion-runs'],
    queryFn: () => api<Run[]>('/ingestion/runs'),
    refetchInterval: 4000,
  });

  return (
    <div>
      <PageHeader
        title="Ingestion runs"
        description="Run the ingestion pipeline over a document selection. Pick a subset of parts to redo only that work — e.g. only relationship extraction after the relationship config changed."
        actions={
          <PrimaryButton onClick={() => setCreating((v) => !v)}>
            {creating ? 'Cancel' : 'New run'}
          </PrimaryButton>
        }
      />
      {creating && (
        <NewRunForm
          onClose={() => setCreating(false)}
          onCreated={() => setCreating(false)}
        />
      )}
      <div className="space-y-2">
        {(runs.data ?? []).map((r) => (
          <RunRow key={r.id} run={r} />
        ))}
        {runs.data && runs.data.length === 0 && (
          <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
            No runs yet.
          </div>
        )}
      </div>
    </div>
  );
}

function NewRunForm({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const qc = useQueryClient();
  const parts = useQuery({
    queryKey: ['ingestion-parts'],
    queryFn: () => api<PartDesc[]>('/ingestion/parts'),
  });
  const classes = useQuery({
    queryKey: ['document-classes'],
    queryFn: () => api<DocumentClass[]>('/config/document-classes'),
  });
  const [selectedParts, setSelectedParts] = useState<Set<string>>(new Set());
  const [classIds, setClassIds] = useState<Set<string>>(new Set());
  const [createdSince, setCreatedSince] = useState('');
  const [preview, setPreview] = useState<CreateResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The full pipeline is the default: preselect every part once loaded.
  useEffect(() => {
    if (parts.data && selectedParts.size === 0) {
      setSelectedParts(new Set(parts.data.map((p) => p.key)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parts.data]);

  const buildBody = (dryRun: boolean) => ({
    parts: Array.from(selectedParts),
    filter: {
      document_class_ids: classIds.size > 0 ? Array.from(classIds) : null,
      created_since: createdSince ? new Date(createdSince).toISOString() : null,
    },
    dry_run: dryRun,
  });

  const previewMutation = useMutation({
    mutationFn: () =>
      api<CreateResult>('/ingestion/runs', {
        method: 'POST',
        body: JSON.stringify(buildBody(true)),
      }),
    onSuccess: (res) => {
      setPreview(res);
      setError(null);
    },
    onError: (err) => setError(err instanceof Error ? err.message : 'preview failed'),
  });

  const submit = useMutation({
    mutationFn: () =>
      api<CreateResult>('/ingestion/runs', {
        method: 'POST',
        body: JSON.stringify(buildBody(false)),
      }),
    onSuccess: () => {
      setError(null);
      void qc.invalidateQueries({ queryKey: ['ingestion-runs'] });
      onCreated();
    },
    onError: (err) => setError(err instanceof Error ? err.message : 'failed'),
  });

  const togglePart = (k: string) => {
    const n = new Set(selectedParts);
    if (n.has(k)) n.delete(k);
    else n.add(k);
    setSelectedParts(n);
    setPreview(null);
  };
  const toggleClass = (id: string) => {
    const n = new Set(classIds);
    if (n.has(id)) n.delete(id);
    else n.add(id);
    setClassIds(n);
    setPreview(null);
  };

  return (
    <div className="mb-6 p-4 border border-stone-200 bg-white rounded space-y-4">
      <div>
        <div className="text-xs font-semibold uppercase tracking-wider text-stone-500 mb-2">
          Pipeline parts
        </div>
        <div className="grid grid-cols-2 gap-1">
          {(parts.data ?? []).map((p) => (
            <label key={p.key} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={selectedParts.has(p.key)}
                onChange={() => togglePart(p.key)}
              />
              <span className="font-mono text-xs text-stone-500">{p.key}</span>
              <span>— {p.label}</span>
            </label>
          ))}
        </div>
        <div className="text-xs text-stone-400 mt-1">
          All parts = full pipeline. A subset redoes only that work; only
          “extract” wipes previously extracted data first.
        </div>
      </div>

      <div>
        <div className="text-xs font-semibold uppercase tracking-wider text-stone-500 mb-2">
          Filter
        </div>
        <Field label="Document classes (leave empty to apply to all)">
          <div className="flex flex-wrap gap-2">
            {(classes.data ?? []).map((c) => (
              <button
                key={c.id}
                onClick={() => toggleClass(c.id)}
                className={`px-2 py-1 rounded border text-xs ${
                  classIds.has(c.id)
                    ? 'border-forest-500 bg-forest-50 text-forest-700'
                    : 'border-stone-300 text-stone-700 hover:bg-stone-100'
                }`}
              >
                {c.name}{' '}
                <span className="font-mono text-stone-400">{c.slug}</span>
              </button>
            ))}
            {classes.data && classes.data.length === 0 && (
              <div className="text-xs text-stone-400">no classes configured</div>
            )}
          </div>
        </Field>
        <Field label="Only documents created since" hint="optional, ISO datetime">
          <input
            type="datetime-local"
            value={createdSince}
            onChange={(e) => {
              setCreatedSince(e.target.value);
              setPreview(null);
            }}
            className={inputClasses}
          />
        </Field>
      </div>

      {preview && (
        <div className="text-sm text-stone-700 bg-stone-100 rounded px-3 py-2">
          Would process <b>{preview.document_count}</b> document(s) through{' '}
          <b>{selectedParts.size}</b> part(s).
        </div>
      )}
      <ErrorBanner message={error} />
      <div className="flex gap-2">
        <SecondaryButton onClick={() => previewMutation.mutate()}>
          {previewMutation.isPending ? 'Counting…' : 'Preview count'}
        </SecondaryButton>
        <PrimaryButton
          onClick={() => submit.mutate()}
          disabled={submit.isPending || selectedParts.size === 0}
        >
          {submit.isPending ? 'Starting…' : 'Start run'}
        </PrimaryButton>
        <SecondaryButton onClick={onClose}>Close</SecondaryButton>
      </div>
    </div>
  );
}

function RunRow({ run }: { run: Run }) {
  const pct = run.total_units > 0 ? Math.round((run.done_units / run.total_units) * 100) : 0;
  const statusColor =
    {
      pending: 'bg-stone-200 text-stone-600',
      running: 'bg-blue-100 text-blue-700',
      completed: 'bg-forest-100 text-forest-700',
      failed: 'bg-red-100 text-red-700',
      cancelled: 'bg-stone-200 text-stone-500',
    }[run.status] ?? 'bg-stone-200 text-stone-600';

  return (
    <div className="p-4 border border-stone-200 rounded bg-white">
      <div className="flex items-baseline justify-between mb-2">
        <div>
          <span className={`text-xs px-2 py-0.5 rounded ${statusColor}`}>{run.status}</span>
          <span className="ml-3 text-xs text-stone-500">
            {new Date(run.created_at).toLocaleString()}
          </span>
        </div>
        <div className="text-xs text-stone-500 font-mono">{run.id.slice(0, 8)}</div>
      </div>
      <div className="text-xs text-stone-600 mb-2">
        Parts: {(run.parts ?? []).map((p) => <span key={p} className="font-mono mr-2">{p}</span>)}
      </div>
      <div className="w-full bg-stone-200 rounded h-2 overflow-hidden">
        <div
          className={`h-full ${run.failed_units > 0 ? 'bg-amber-500' : 'bg-forest-500'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="text-xs text-stone-500 mt-1">
        {run.done_units}/{run.total_units} document(s)
        {run.failed_units > 0 && (
          <span className="text-amber-700"> · {run.failed_units} failed</span>
        )}
      </div>
      {run.error && (
        <div className="text-xs text-red-700 mt-1 font-mono">{run.error}</div>
      )}
    </div>
  );
}
