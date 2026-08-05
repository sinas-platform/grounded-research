import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';
import {
  ErrorBanner,
  PrimaryButton,
  SecondaryButton,
} from '@/components/Form';

interface Document {
  id: string;
  filename: string;
  summary: string | null;
  toc: Record<string, unknown> | null;
  document_class_id: string | null;
  classification_confidence: number | null;
  collection_file_id: string | null;
  created_at: string;
  updated_at: string;
}

interface DocumentVersion {
  id: string;
  document_id: string;
  version: number;
  created_at: string;
}

interface DocumentClass {
  id: string;
  name: string;
  description: string | null;
}

interface PropertyValue {
  id: string;
  property_id: string;
  value: unknown;
  source_span: Record<string, unknown> | null;
  method: string;
  confidence: number | null;
  reason: string | null;
}

interface DocumentClassProperty {
  id: string;
  document_class_id: string;
  name: string;
  description: string | null;
}

interface EntityMention {
  id: string;
  entity_id: string;
  span: Record<string, unknown>;
  confidence: number | null;
  entity_canonical_form: string;
  entity_type_id: string;
  entity_type_name: string | null;
}

interface RelationshipDefinition {
  id: string;
  name: string;
  source_ref_type: string;
  target_ref_type: string;
}

interface Relationship {
  id: string;
  relationship_definition_id: string;
  source_id: string;
  target_id: string;
  confidence: number | null;
  notes: string | null;
}

interface DocumentContent {
  content: string;
  version: number;
}

interface PartDesc {
  key: string;
  label: string;
}

type Tab = 'overview' | 'content' | 'properties' | 'entities' | 'relationships';

export default function DocumentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [tab, setTab] = useState<Tab>('overview');
  const [reprocessOpen, setReprocessOpen] = useState(false);

  const doc = useQuery({
    queryKey: ['document', id],
    queryFn: () => api<Document>(`/documents/${id}`),
    enabled: !!id,
    refetchInterval: (query) => {
      // While ingestion is still running (no class assigned yet), poll.
      const data = query.state.data;
      return data && !data.document_class_id ? 4000 : false;
    },
  });

  const classes = useQuery({
    queryKey: ['document-classes'],
    queryFn: () => api<DocumentClass[]>('/config/document-classes'),
  });

  const docClass = doc.data?.document_class_id
    ? classes.data?.find((c) => c.id === doc.data!.document_class_id)
    : null;

  if (!id) return null;

  const tabs: { value: Tab; label: string }[] = [
    { value: 'overview', label: 'Overview' },
    { value: 'content', label: 'Content' },
    { value: 'properties', label: 'Properties' },
    { value: 'entities', label: 'Entities' },
    { value: 'relationships', label: 'Relationships' },
  ];

  return (
    <div>
      <PageHeader
        title={doc.data?.filename ?? 'Loading…'}
        description={
          docClass ? (
            <span>
              Class: <span className="font-mono text-forest-700">{docClass.name}</span>
              {doc.data?.classification_confidence != null && (
                <span className="text-stone-400">
                  {' '}· conf {doc.data.classification_confidence.toFixed(2)}
                </span>
              )}
            </span>
          ) : doc.data ? (
            'Awaiting classification…'
          ) : (
            undefined
          )
        }
        actions={
          <div className="flex gap-2">
            <SecondaryButton onClick={() => setReprocessOpen((v) => !v)}>
              {reprocessOpen ? 'Cancel' : 'Reprocess…'}
            </SecondaryButton>
            <Link
              to="/documents"
              className="px-3 py-1.5 rounded border border-stone-300 text-sm hover:bg-stone-100"
            >
              ← Back
            </Link>
          </div>
        }
      />

      {reprocessOpen && (
        <ReprocessPanel docId={id} onClose={() => setReprocessOpen(false)} />
      )}

      <div className="border-b border-stone-200 mb-6">
        <nav className="flex gap-1">
          {tabs.map((t) => (
            <button
              key={t.value}
              onClick={() => setTab(t.value)}
              className={`px-4 py-2 text-sm border-b-2 -mb-px ${
                tab === t.value
                  ? 'border-forest-600 text-forest-700 font-medium'
                  : 'border-transparent text-stone-600 hover:text-stone-900'
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </div>

      {tab === 'overview' && doc.data && <OverviewTab doc={doc.data} docClass={docClass} />}
      {tab === 'content' && <ContentTab docId={id} />}
      {tab === 'properties' && (
        <PropertiesTab docId={id} classId={doc.data?.document_class_id ?? null} />
      )}
      {tab === 'entities' && <EntitiesTab docId={id} />}
      {tab === 'relationships' && <RelationshipsTab docId={id} />}
    </div>
  );
}

function OverviewTab({
  doc,
  docClass,
}: {
  doc: Document;
  docClass: DocumentClass | null | undefined;
}) {
  return (
    <div className="space-y-4">
      <Row label="Filename" value={doc.filename} mono />
      <Row label="Document ID" value={doc.id} mono small />
      <Row label="Collection file" value={doc.collection_file_id ?? '—'} mono small />
      <Row
        label="Class"
        value={
          docClass ? (
            <span>
              {docClass.name}
              {docClass.description && (
                <span className="text-stone-500 ml-2 text-xs">— {docClass.description}</span>
              )}
            </span>
          ) : (
            <span className="text-stone-400">unassigned</span>
          )
        }
      />
      <Row
        label="Classification confidence"
        value={doc.classification_confidence?.toFixed(2) ?? '—'}
      />
      <Row label="Created" value={new Date(doc.created_at).toLocaleString()} small />

      {doc.summary && (
        <div className="mt-6">
          <div className="text-xs font-semibold uppercase tracking-wider text-stone-500 mb-2">
            Summary
          </div>
          <div className="p-4 border border-stone-200 rounded bg-white text-sm whitespace-pre-wrap">
            {doc.summary}
          </div>
        </div>
      )}
      {!doc.summary && (
        <div className="mt-6 text-stone-400 text-sm italic">
          No summary yet — summarizer_agent hasn't run, or hasn't completed.
        </div>
      )}

      {doc.toc && Object.keys(doc.toc).length > 0 && (
        <div className="mt-6">
          <div className="text-xs font-semibold uppercase tracking-wider text-stone-500 mb-2">
            Table of contents
          </div>
          <pre className="p-4 border border-stone-200 rounded bg-stone-50 text-xs overflow-auto">
            {JSON.stringify(doc.toc, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

function ContentTab({ docId }: { docId: string }) {
  const versions = useQuery({
    queryKey: ['document-versions', docId],
    queryFn: () => api<DocumentVersion[]>(`/documents/${docId}/versions`),
  });
  const latest = versions.data?.length ? versions.data[versions.data.length - 1] : null;

  const content = useQuery({
    queryKey: ['document-content', docId, latest?.version],
    queryFn: () =>
      api<DocumentContent>(`/documents/${docId}/versions/${latest!.version}/content`),
    enabled: !!latest,
  });

  if (!versions.data) return <div className="text-stone-500">Loading…</div>;
  if (!latest) {
    return (
      <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
        No versions yet.
      </div>
    );
  }
  return (
    <div>
      <div className="text-xs text-stone-500 mb-3">
        version {latest.version} · {versions.data.length} total
      </div>
      {content.isLoading && <div className="text-stone-500">Loading content…</div>}
      {content.error && (
        <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
          No extracted content for this version. The post-upload function may not have produced
          markdown (binary file, or MarkItDown failed).
        </div>
      )}
      {content.data && (
        <pre className="p-4 border border-stone-200 rounded bg-white text-sm whitespace-pre-wrap font-mono overflow-auto max-h-[70vh]">
          {content.data.content || '(empty)'}
        </pre>
      )}
    </div>
  );
}

function PropertiesTab({ docId, classId }: { docId: string; classId: string | null }) {
  const values = useQuery({
    queryKey: ['property-values', docId],
    queryFn: () => api<PropertyValue[]>(`/documents/${docId}/property-values`),
  });
  const props = useQuery({
    queryKey: ['document-class-properties', classId],
    queryFn: () =>
      api<DocumentClassProperty[]>(`/config/document-classes/${classId}/properties`),
    enabled: !!classId,
  });
  const propMap = new Map((props.data ?? []).map((p) => [p.id, p]));

  if (!values.data) return <div className="text-stone-500">Loading…</div>;
  if (values.data.length === 0) {
    return (
      <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
        No properties extracted yet.
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {values.data.map((v) => {
        const def = propMap.get(v.property_id);
        return (
          <div key={v.id} className="p-3 border border-stone-200 rounded bg-white">
            <div className="flex items-baseline justify-between mb-1">
              <div className="text-sm font-medium">{def?.name ?? v.property_id.slice(0, 8)}</div>
              <div className="text-xs text-stone-400">
                {v.method}
                {v.confidence != null && ` · ${v.confidence.toFixed(2)}`}
              </div>
            </div>
            <pre className="text-xs bg-stone-50 px-2 py-1 rounded overflow-auto">
              {JSON.stringify(v.value, null, 2)}
            </pre>
            {v.reason && <div className="text-xs text-stone-500 mt-1 italic">{v.reason}</div>}
          </div>
        );
      })}
    </div>
  );
}

function EntitiesTab({ docId }: { docId: string }) {
  const mentions = useQuery({
    queryKey: ['entity-mentions', docId],
    queryFn: () => api<EntityMention[]>(`/documents/${docId}/entity-mentions`),
  });

  if (!mentions.data) return <div className="text-stone-500">Loading…</div>;
  if (mentions.data.length === 0) {
    return (
      <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
        No entities extracted yet.
      </div>
    );
  }

  // Group by entity type
  const grouped = new Map<string, EntityMention[]>();
  for (const m of mentions.data) {
    const key = m.entity_type_name ?? m.entity_type_id;
    grouped.set(key, [...(grouped.get(key) ?? []), m]);
  }

  return (
    <div className="space-y-4">
      {Array.from(grouped.entries()).map(([typeName, items]) => (
        <div key={typeName}>
          <div className="text-xs font-semibold uppercase tracking-wider text-stone-500 mb-2">
            {typeName} <span className="text-stone-400">({items.length})</span>
          </div>
          <div className="space-y-1">
            {items.map((m) => (
              <div
                key={m.id}
                className="p-2 border border-stone-200 rounded bg-white flex items-baseline justify-between"
              >
                <div className="text-sm">{m.entity_canonical_form}</div>
                <div className="text-xs text-stone-400">
                  {m.confidence != null ? `conf ${m.confidence.toFixed(2)}` : ''}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function RelationshipsTab({ docId }: { docId: string }) {
  const rels = useQuery({
    queryKey: ['document-relationships', docId],
    queryFn: () => api<Relationship[]>(`/relationships?evidence_document_id=${docId}`),
  });
  const defs = useQuery({
    queryKey: ['relationship-definitions'],
    queryFn: () => api<RelationshipDefinition[]>('/config/relationship-definitions'),
  });
  const defMap = new Map((defs.data ?? []).map((d) => [d.id, d]));

  if (!rels.data) return <div className="text-stone-500">Loading…</div>;
  if (rels.data.length === 0) {
    return (
      <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
        No relationships extracted from this document yet.
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {rels.data.map((r) => {
        const def = defMap.get(r.relationship_definition_id);
        return (
          <div key={r.id} className="p-3 border border-stone-200 rounded bg-white">
            <div className="flex items-baseline justify-between">
              <div className="text-sm font-medium">{def?.name ?? r.relationship_definition_id.slice(0, 8)}</div>
              <div className="text-xs text-stone-400">
                {r.confidence != null && `conf ${r.confidence.toFixed(2)}`}
              </div>
            </div>
            <div className="text-xs text-stone-500 mt-1 font-mono">
              → {r.target_id.slice(0, 8)}
            </div>
            {r.notes && <div className="text-xs text-stone-500 mt-1">{r.notes}</div>}
          </div>
        );
      })}
    </div>
  );
}

function ReprocessPanel({ docId, onClose }: { docId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const parts = useQuery({
    queryKey: ['ingestion-parts'],
    queryFn: () => api<PartDesc[]>('/ingestion/parts'),
  });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);

  // Full pipeline is the default, same as the new-run form.
  useEffect(() => {
    if (parts.data && selected.size === 0) {
      setSelected(new Set(parts.data.map((p) => p.key)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parts.data]);

  const submit = useMutation({
    mutationFn: () =>
      api<{ run_id: string; unit_count: number; status: string }>(
        `/ingestion/documents/${docId}/reprocess`,
        { method: 'POST', body: JSON.stringify({ parts: Array.from(selected) }) },
      ),
    onSuccess: (res) => {
      setError(null);
      setRunId(res.run_id);
      // Invalidate the doc so polling picks up class/summary updates.
      void qc.invalidateQueries({ queryKey: ['document', docId] });
    },
    onError: (err) => setError(err instanceof Error ? err.message : 'failed'),
  });

  const toggle = (key: string) => {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setSelected(next);
  };

  return (
    <div className="mb-6 p-4 border border-forest-500 bg-forest-50 rounded">
      <div className="text-sm font-medium text-stone-900 mb-2">
        Reprocess this document
      </div>
      <div className="text-xs text-stone-600 mb-3">
        Pick which pipeline parts to rerun. Only “extract” wipes auto-extracted
        artifacts (entities, properties) first; manual entries are always kept.
      </div>
      <div className="grid grid-cols-2 gap-1 mb-3">
        {(parts.data ?? []).map((s) => (
          <label key={s.key} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={selected.has(s.key)}
              onChange={() => toggle(s.key)}
            />
            <span className="font-mono text-xs text-stone-500">{s.key}</span>
            <span>— {s.label}</span>
          </label>
        ))}
      </div>
      <ErrorBanner message={error} />
      {runId ? (
        <div className="text-sm text-forest-700 mt-2">
          Run started — track in{' '}
          <Link to="/ingestion/runs" className="underline">
            Ingestion runs
          </Link>
          .{' '}
          <button onClick={onClose} className="underline">
            Close
          </button>
        </div>
      ) : (
        <div className="flex gap-2 mt-2">
          <PrimaryButton
            onClick={() => submit.mutate()}
            disabled={submit.isPending || selected.size === 0}
          >
            {submit.isPending ? 'Starting…' : `Run ${selected.size} stage(s)`}
          </PrimaryButton>
          <SecondaryButton onClick={onClose}>Cancel</SecondaryButton>
        </div>
      )}
    </div>
  );
}

function Row({
  label,
  value,
  mono,
  small,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  small?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-4">
      <div className="w-44 text-xs font-medium text-stone-500 uppercase tracking-wider">
        {label}
      </div>
      <div
        className={`text-stone-900 ${mono ? 'font-mono' : ''} ${small ? 'text-xs' : 'text-sm'}`}
      >
        {value}
      </div>
    </div>
  );
}
