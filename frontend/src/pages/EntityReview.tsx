import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';

type Tab = 'proposals' | 'relationships' | 'unresolved';

interface EntityType {
  id: string;
  name: string;
}

interface EntityProposal {
  id: string;
  entity_type_id: string;
  canonical_form: string;
  proposing_agent: string | null;
  reasoning: string | null;
  evidence_document_id: string | null;
  confidence: number | null;
  status: string;
  created_at: string;
}

interface UnresolvedMention {
  id: string;
  entity_type_id: string;
  mention_text: string;
  document_id: string;
  span: Record<string, unknown>;
  confidence: number | null;
  proposing_agent: string | null;
  reasoning: string | null;
  status: string;
  created_at: string;
}

interface Entity {
  id: string;
  canonical_form: string;
  entity_type_id?: string;
}

interface RelationshipProposal {
  id: string;
  definition_name: string | null;
  source_name: string | null;
  target_name: string | null;
  proposing_agent: string | null;
  reasoning: string | null;
  confidence: number | null;
  status: string;
  created_at: string;
}

export default function EntityReviewPage() {
  const [tab, setTab] = useState<Tab>('proposals');
  return (
    <div>
      <PageHeader
        title="Entity review"
        description="The graph most answers are retrieved through: proposed entities and relationships awaiting approval, and mentions that didn't match a known entity in a locked type."
      />
      <nav className="flex gap-1 border-b border-stone-200 mb-6">
        {(['proposals', 'relationships', 'unresolved'] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm border-b-2 -mb-px transition-colors capitalize ${
              tab === t
                ? 'border-forest-600 text-forest-700 font-medium'
                : 'border-transparent text-stone-600 hover:text-stone-900'
            }`}
          >
            {t === 'proposals'
              ? 'Entities (Review mode)'
              : t === 'relationships'
                ? 'Relationships'
                : 'Unresolved mentions (Locked mode)'}
          </button>
        ))}
      </nav>
      {tab === 'proposals' ? <ProposalsTab /> : tab === 'relationships' ? <RelationshipsTab /> : <UnresolvedTab />}
    </div>
  );
}

function useEntityTypeMap() {
  const q = useQuery({
    queryKey: ['entity-types'],
    queryFn: () => api<EntityType[]>('/config/entity-types'),
  });
  const map = new Map<string, string>();
  for (const et of q.data ?? []) map.set(et.id, et.name);
  return map;
}

function ProposalsTab() {
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ['entity-proposals', 'pending'],
    queryFn: () => api<EntityProposal[]>('/entities/proposals?status_filter=pending'),
  });
  const types = useEntityTypeMap();

  const decide = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) =>
      api(`/entities/proposals/${id}/decision`, {
        method: 'POST',
        body: JSON.stringify({ approve }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['entity-proposals'] }),
  });

  if (list.isLoading) return <div className="text-stone-500 text-sm">Loading…</div>;
  const rows = list.data ?? [];
  if (rows.length === 0) {
    return (
      <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
        No pending proposals.
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {rows.map((p) => (
        <div key={p.id} className="border border-stone-200 rounded p-3 bg-white">
          <div className="flex items-center justify-between gap-3 mb-1">
            <div>
              <span className="text-xs text-stone-500 mr-2">
                {types.get(p.entity_type_id) ?? p.entity_type_id.slice(0, 8)}
              </span>
              <span className="font-medium">{p.canonical_form}</span>
              {p.confidence !== null && (
                <span className="ml-2 text-xs text-stone-500">
                  ({Math.round(p.confidence * 100)}%)
                </span>
              )}
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => decide.mutate({ id: p.id, approve: true })}
                className="px-3 py-1 rounded bg-forest-600 text-white text-sm hover:bg-forest-700"
              >
                Approve
              </button>
              <button
                onClick={() => decide.mutate({ id: p.id, approve: false })}
                className="px-3 py-1 rounded border border-stone-300 text-sm hover:bg-stone-100"
              >
                Reject
              </button>
            </div>
          </div>
          {p.proposing_agent && (
            <div className="text-xs text-stone-500">by {p.proposing_agent}</div>
          )}
          {p.reasoning && <div className="text-xs text-stone-600 mt-1">{p.reasoning}</div>}
        </div>
      ))}
    </div>
  );
}

function RelationshipsTab() {
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ['relationship-proposals', 'pending'],
    queryFn: () =>
      api<RelationshipProposal[]>('/relationships/proposals?status_filter=pending&limit=200'),
  });

  const decide = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) =>
      api(`/relationships/proposals/${id}/decision`, {
        method: 'POST',
        body: JSON.stringify({ approve }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['relationship-proposals'] }),
  });

  if (list.isLoading) return <div className="text-stone-500 text-sm">Loading…</div>;
  const rows = list.data ?? [];
  if (rows.length === 0) {
    return (
      <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
        No pending relationship proposals.
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <div className="text-xs text-stone-500">
        Showing {rows.length} pending. Approving writes the edge into the graph that
        retrieval traverses.
      </div>
      {rows.map((p) => (
        <div key={p.id} className="border border-stone-200 rounded p-3 bg-white">
          <div className="flex items-center justify-between gap-3 mb-1">
            <div className="min-w-0">
              <span className="font-medium">{p.source_name ?? '(unknown source)'}</span>
              <span className="mx-2 text-xs text-stone-500">
                {p.definition_name ?? 'related to'}
              </span>
              <span className="font-medium">{p.target_name ?? '(unknown target)'}</span>
              {p.confidence !== null && (
                <span className="ml-2 text-xs text-stone-500">
                  ({Math.round(p.confidence * 100)}%)
                </span>
              )}
            </div>
            <div className="flex gap-2 shrink-0">
              <button
                onClick={() => decide.mutate({ id: p.id, approve: true })}
                className="px-3 py-1 rounded bg-forest-600 text-white text-sm hover:bg-forest-700"
              >
                Approve
              </button>
              <button
                onClick={() => decide.mutate({ id: p.id, approve: false })}
                className="px-3 py-1 rounded border border-stone-300 text-sm hover:bg-stone-100"
              >
                Reject
              </button>
            </div>
          </div>
          {p.proposing_agent && (
            <div className="text-xs text-stone-500">by {p.proposing_agent}</div>
          )}
          {p.reasoning && <div className="text-xs text-stone-600 mt-1">{p.reasoning}</div>}
        </div>
      ))}
    </div>
  );
}

function UnresolvedTab() {
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ['unresolved-entity-mentions', 'unresolved'],
    queryFn: () =>
      api<UnresolvedMention[]>('/entities/unresolved?status_filter=unresolved'),
  });
  const types = useEntityTypeMap();

  const match = useMutation({
    mutationFn: ({ id, entityId }: { id: string; entityId: string }) =>
      api(`/entities/unresolved/${id}/match`, {
        method: 'POST',
        body: JSON.stringify({ entity_id: entityId, add_alias: true }),
      }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['unresolved-entity-mentions'] }),
  });
  const promote = useMutation({
    mutationFn: (id: string) =>
      api(`/entities/unresolved/${id}/promote`, { method: 'POST' }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['unresolved-entity-mentions'] }),
  });
  const dismiss = useMutation({
    mutationFn: (id: string) =>
      api(`/entities/unresolved/${id}/dismiss`, { method: 'POST' }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['unresolved-entity-mentions'] }),
  });

  if (list.isLoading) return <div className="text-stone-500 text-sm">Loading…</div>;
  const rows = list.data ?? [];
  if (rows.length === 0) {
    return (
      <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
        No unresolved mentions.
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {rows.map((m) => (
        <UnresolvedRow
          key={m.id}
          mention={m}
          typeName={types.get(m.entity_type_id) ?? m.entity_type_id.slice(0, 8)}
          onMatch={(entityId) => match.mutate({ id: m.id, entityId })}
          onPromote={() => promote.mutate(m.id)}
          onDismiss={() => dismiss.mutate(m.id)}
        />
      ))}
    </div>
  );
}

function UnresolvedRow({
  mention,
  typeName,
  onMatch,
  onPromote,
  onDismiss,
}: {
  mention: UnresolvedMention;
  typeName: string;
  onMatch: (entityId: string) => void;
  onPromote: () => void;
  onDismiss: () => void;
}) {
  const [picking, setPicking] = useState(false);
  return (
    <div className="border border-stone-200 rounded p-3 bg-white">
      <div className="flex items-center justify-between gap-3 mb-1">
        <div>
          <span className="text-xs text-stone-500 mr-2">{typeName}</span>
          <span className="font-medium">{mention.mention_text}</span>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setPicking((v) => !v)}
            className="px-3 py-1 rounded bg-stone-200 text-sm hover:bg-stone-300"
          >
            Match to existing
          </button>
          <button
            onClick={() => {
              if (confirm(`Create new "${mention.mention_text}" in ${typeName}?`)) onPromote();
            }}
            className="px-3 py-1 rounded bg-forest-600 text-white text-sm hover:bg-forest-700"
          >
            Promote new
          </button>
          <button
            onClick={onDismiss}
            className="px-3 py-1 rounded border border-stone-300 text-sm hover:bg-stone-100"
          >
            Dismiss
          </button>
        </div>
      </div>
      {mention.reasoning && (
        <div className="text-xs text-stone-600 mt-1">{mention.reasoning}</div>
      )}
      {picking && (
        <EntityPicker
          entityTypeId={mention.entity_type_id}
          onPick={(id) => {
            setPicking(false);
            onMatch(id);
          }}
        />
      )}
    </div>
  );
}

function EntityPicker({
  entityTypeId,
  onPick,
}: {
  entityTypeId: string;
  onPick: (id: string) => void;
}) {
  const q = useQuery({
    queryKey: ['entities-of-type', entityTypeId],
    queryFn: () => api<Entity[]>(`/entities?entity_type_id=${entityTypeId}&limit=500`),
  });
  const [filter, setFilter] = useState('');
  const filtered = (q.data ?? []).filter((e) =>
    e.canonical_form.toLowerCase().includes(filter.toLowerCase()),
  );
  return (
    <div className="mt-2 p-2 border border-stone-200 rounded bg-stone-50">
      <input
        autoFocus
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="filter…"
        className="w-full border border-stone-300 rounded px-2 py-1 text-sm mb-2"
      />
      <div className="max-h-40 overflow-auto space-y-1">
        {filtered.length === 0 && (
          <div className="text-xs text-stone-500 px-2 py-1">
            No entities of this type yet. Use "Promote new".
          </div>
        )}
        {filtered.map((e) => (
          <button
            key={e.id}
            onClick={() => onPick(e.id)}
            className="block w-full text-left px-2 py-1 text-sm hover:bg-white border border-transparent hover:border-stone-300 rounded"
          >
            {e.canonical_form}
          </button>
        ))}
      </div>
    </div>
  );
}
