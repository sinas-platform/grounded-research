import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';
import {
  DangerButton,
  ErrorBanner,
  Field,
  PrimaryButton,
  SecondaryButton,
  inputClasses,
  textareaClasses,
} from '@/components/Form';

type RefType = 'document_class' | 'entity_type' | 'dossier_class';
type CreationMode = 'open' | 'review' | 'closed';

interface RelationshipDefinition {
  id: string;
  name: string;
  description: string | null;
  source_ref_type: RefType;
  source_ref_id: string;
  target_ref_type: RefType;
  target_ref_id: string;
  cardinality: 'one' | 'many';
  extraction_guidance: string | null;
  discovery_guidance: string | null;
  creation_mode: CreationMode;
}

const MODE_LABEL: Record<CreationMode, string> = {
  open: 'Allow',
  review: 'Review',
  closed: 'Lock',
};
const MODE_HINT: Record<CreationMode, string> = {
  open: 'Agents may add new edges of this definition directly.',
  review: 'New edges are held as proposals until a human approves them.',
  closed: 'Runtime extraction is blocked. Only the curated edges from the YAML / admin UI exist.',
};
const MODE_BADGE: Record<CreationMode, string> = {
  open: 'bg-stone-100 text-stone-700',
  review: 'bg-amber-100 text-amber-800',
  closed: 'bg-red-100 text-red-800',
};

interface RelationshipState {
  id: string;
  relationship_definition_id: string;
  name: string;
  description: string | null;
  counts_as_active: boolean;
}

interface NamedResource {
  id: string;
  name: string;
}

const REF_TYPE_PATH: Record<RefType, string> = {
  document_class: '/config/document-classes',
  entity_type: '/config/entity-types',
  dossier_class: '/config/dossier-classes',
};

const REF_TYPE_LABEL: Record<RefType, string> = {
  document_class: 'Document class',
  entity_type: 'Entity type',
  dossier_class: 'Dossier class',
};

export default function RelationshipsPage({ embedded = false }: { embedded?: boolean } = {}) {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useQuery({
    queryKey: ['relationship-definitions'],
    queryFn: () => api<RelationshipDefinition[]>('/config/relationship-definitions'),
  });

  // Pre-load all the resource lists so dropdowns and labels work everywhere.
  const refQueries = useQueries({
    queries: (Object.keys(REF_TYPE_PATH) as RefType[]).map((t) => ({
      queryKey: ['ref-list', t],
      queryFn: () => api<NamedResource[]>(REF_TYPE_PATH[t]),
    })),
  });
  const refIndex: Record<RefType, NamedResource[]> = {
    document_class: refQueries[0].data ?? [],
    entity_type: refQueries[1].data ?? [],
    dossier_class: refQueries[2].data ?? [],
  };

  const labelFor = (type: RefType, id: string) => {
    const r = refIndex[type].find((x) => x.id === id);
    return r ? r.name : id.slice(0, 8);
  };

  const selected = list.data?.find((d) => d.id === selectedId) ?? null;

  return (
    <div>
      {!embedded && (
        <PageHeader
          title="Relationship definitions"
          description="Edges between document/entity/dossier types — with extraction and discovery guidance, plus state semantics."
          actions={
            <PrimaryButton
              onClick={() => {
                setSelectedId(null);
                setCreating(true);
              }}
            >
              New relationship
            </PrimaryButton>
          }
        />
      )}
      {embedded && (
        <div className="mb-4 flex justify-end">
          <PrimaryButton
            onClick={() => {
              setSelectedId(null);
              setCreating(true);
            }}
          >
            New relationship
          </PrimaryButton>
        </div>
      )}
      <div className="grid grid-cols-12 gap-6">
        <div className="col-span-4 space-y-2">
          {(list.data ?? []).map((rd) => (
            <button
              key={rd.id}
              onClick={() => {
                setSelectedId(rd.id);
                setCreating(false);
              }}
              className={`block w-full text-left p-3 border rounded ${
                selectedId === rd.id
                  ? 'border-forest-500 bg-forest-50'
                  : 'border-stone-200 bg-white hover:border-stone-300'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="font-medium text-sm">{rd.name}</div>
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded uppercase tracking-wider ${MODE_BADGE[rd.creation_mode]}`}
                >
                  {MODE_LABEL[rd.creation_mode]}
                </span>
              </div>
              <div className="text-xs text-stone-500 mt-1">
                {labelFor(rd.source_ref_type, rd.source_ref_id)}
                {' → '}
                {labelFor(rd.target_ref_type, rd.target_ref_id)}
                {' · '}
                {rd.cardinality}
              </div>
            </button>
          ))}
          {list.data && list.data.length === 0 && (
            <div className="text-stone-500 text-sm py-8 text-center border border-dashed border-stone-300 rounded">
              No relationship definitions yet.
            </div>
          )}
        </div>
        <div className="col-span-8">
          {creating && (
            <Editor
              key="new"
              refIndex={refIndex}
              onSaved={(saved) => {
                qc.invalidateQueries({ queryKey: ['relationship-definitions'] });
                setCreating(false);
                setSelectedId(saved.id);
              }}
            />
          )}
          {!creating && selected && (
            <>
              <Editor
                key={selected.id}
                refIndex={refIndex}
                initial={selected}
                onSaved={() =>
                  qc.invalidateQueries({ queryKey: ['relationship-definitions'] })
                }
                onDeleted={() => {
                  qc.invalidateQueries({ queryKey: ['relationship-definitions'] });
                  setSelectedId(null);
                }}
              />
              <StatesEditor defId={selected.id} />
            </>
          )}
          {!creating && !selected && (
            <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
              Select a relationship to edit, or create a new one.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Editor({
  initial,
  refIndex,
  onSaved,
  onDeleted,
}: {
  initial?: RelationshipDefinition;
  refIndex: Record<RefType, NamedResource[]>;
  onSaved: (saved: RelationshipDefinition) => void;
  onDeleted?: () => void;
}) {
  const isNew = !initial;
  const firstId = (t: RefType) => refIndex[t][0]?.id ?? '';
  const [draft, setDraft] = useState({
    name: initial?.name ?? '',
    description: initial?.description ?? '',
    source_ref_type: initial?.source_ref_type ?? ('document_class' as RefType),
    source_ref_id: initial?.source_ref_id ?? firstId('document_class'),
    target_ref_type: initial?.target_ref_type ?? ('document_class' as RefType),
    target_ref_id: initial?.target_ref_id ?? firstId('document_class'),
    cardinality: initial?.cardinality ?? ('many' as 'one' | 'many'),
    extraction_guidance: initial?.extraction_guidance ?? '',
    discovery_guidance: initial?.discovery_guidance ?? '',
    creation_mode: (initial?.creation_mode ?? 'open') as CreationMode,
  });
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () =>
      api<RelationshipDefinition>(
        isNew
          ? '/config/relationship-definitions'
          : `/config/relationship-definitions/${initial!.id}`,
        { method: isNew ? 'POST' : 'PUT', body: JSON.stringify(draft) },
      ),
    onSuccess: (saved) => {
      setError(null);
      onSaved(saved);
    },
    onError: (err) => setError(err instanceof Error ? err.message : 'save failed'),
  });

  const del = useMutation({
    mutationFn: () =>
      api(`/config/relationship-definitions/${initial!.id}`, { method: 'DELETE' }),
    onSuccess: () => onDeleted?.(),
    onError: (err) => setError(err instanceof Error ? err.message : 'delete failed'),
  });

  const RefPicker = ({
    label,
    typeKey,
    idKey,
  }: {
    label: string;
    typeKey: 'source_ref_type' | 'target_ref_type';
    idKey: 'source_ref_id' | 'target_ref_id';
  }) => {
    const type = draft[typeKey] as RefType;
    return (
      <div className="grid grid-cols-2 gap-2">
        <Field label={`${label} type`}>
          <select
            value={type}
            onChange={(e) => {
              const newType = e.target.value as RefType;
              const newId = refIndex[newType][0]?.id ?? '';
              setDraft({ ...draft, [typeKey]: newType, [idKey]: newId } as typeof draft);
            }}
            className={inputClasses}
          >
            {(Object.keys(REF_TYPE_LABEL) as RefType[]).map((t) => (
              <option key={t} value={t}>
                {REF_TYPE_LABEL[t]}
              </option>
            ))}
          </select>
        </Field>
        <Field label={`${label} resource`}>
          <select
            value={draft[idKey]}
            onChange={(e) => setDraft({ ...draft, [idKey]: e.target.value } as typeof draft)}
            className={inputClasses}
          >
            {refIndex[type].length === 0 ? (
              <option value="">(none configured)</option>
            ) : (
              refIndex[type].map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))
            )}
          </select>
        </Field>
      </div>
    );
  };

  return (
    <div className="space-y-3 p-4 border border-stone-200 rounded bg-white mb-6">
      <div className="flex items-center justify-between">
        <div className="text-xs text-stone-400">
          {isNew ? 'New relationship definition' : `id: ${initial!.id.slice(0, 8)}`}
        </div>
        {!isNew && (
          <DangerButton
            onClick={() => {
              if (confirm(`Delete relationship "${initial!.name}"?`)) del.mutate();
            }}
          >
            Delete
          </DangerButton>
        )}
      </div>
      <Field label="Name">
        <input
          value={draft.name}
          onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          className={inputClasses}
        />
      </Field>
      <Field label="Description">
        <textarea
          value={draft.description}
          onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          rows={2}
          className={inputClasses}
        />
      </Field>
      <RefPicker label="Source" typeKey="source_ref_type" idKey="source_ref_id" />
      <RefPicker label="Target" typeKey="target_ref_type" idKey="target_ref_id" />
      <Field label="Cardinality">
        <select
          value={draft.cardinality}
          onChange={(e) =>
            setDraft({ ...draft, cardinality: e.target.value as 'one' | 'many' })
          }
          className={inputClasses}
        >
          <option value="one">one</option>
          <option value="many">many</option>
        </select>
      </Field>
      <Field label="Extraction guidance" hint="read by the relationship extraction pass">
        <textarea
          value={draft.extraction_guidance}
          onChange={(e) => setDraft({ ...draft, extraction_guidance: e.target.value })}
          rows={3}
          className={textareaClasses}
        />
      </Field>
      <Field label="Discovery guidance" hint="read by relationship_discovery_agent">
        <textarea
          value={draft.discovery_guidance}
          onChange={(e) => setDraft({ ...draft, discovery_guidance: e.target.value })}
          rows={3}
          className={textareaClasses}
        />
      </Field>
      <Field label="Creation mode" hint="who may add new edges of this definition">
        <div className="space-y-1">
          {(['open', 'review', 'closed'] as CreationMode[]).map((m) => (
            <label key={m} className="flex items-start gap-2 text-sm">
              <input
                type="radio"
                name="rel_creation_mode"
                value={m}
                checked={draft.creation_mode === m}
                onChange={() => setDraft({ ...draft, creation_mode: m })}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium">{MODE_LABEL[m]}</span>{' '}
                <span className="text-stone-500 text-xs">— {MODE_HINT[m]}</span>
              </span>
            </label>
          ))}
        </div>
      </Field>
      <ErrorBanner message={error} />
      <div>
        <PrimaryButton
          onClick={() => save.mutate()}
          disabled={
            save.isPending ||
            !draft.name.trim() ||
            !draft.source_ref_id ||
            !draft.target_ref_id
          }
        >
          {save.isPending ? 'Saving…' : isNew ? 'Create' : 'Save'}
        </PrimaryButton>
      </div>
    </div>
  );
}

function StatesEditor({ defId }: { defId: string }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const list = useQuery({
    queryKey: ['relationship-states', defId],
    queryFn: () =>
      api<RelationshipState[]>(
        `/config/relationship-definitions/${defId}/states`,
      ),
  });

  return (
    <div className="p-4 border border-stone-200 rounded bg-white">
      <div className="flex items-center justify-between mb-3">
        <div className="font-medium">States</div>
        <SecondaryButton
          onClick={() => {
            setAdding(true);
            setEditing(null);
          }}
        >
          Add state
        </SecondaryButton>
      </div>
      <div className="space-y-2">
        {(list.data ?? []).map((s) =>
          editing === s.id ? (
            <StateForm
              key={s.id}
              defId={defId}
              initial={s}
              onSaved={() => {
                qc.invalidateQueries({ queryKey: ['relationship-states', defId] });
                setEditing(null);
              }}
              onDeleted={() => {
                qc.invalidateQueries({ queryKey: ['relationship-states', defId] });
                setEditing(null);
              }}
              onCancel={() => setEditing(null)}
            />
          ) : (
            <button
              key={s.id}
              onClick={() => setEditing(s.id)}
              className="block w-full text-left p-3 border border-stone-200 rounded hover:border-stone-300"
            >
              <div className="flex items-baseline justify-between">
                <div className="text-sm font-medium">{s.name}</div>
                <div className="text-xs text-stone-500">
                  {s.counts_as_active ? 'active' : 'inactive'}
                </div>
              </div>
              {s.description && (
                <div className="text-xs text-stone-500 mt-1">{s.description}</div>
              )}
            </button>
          ),
        )}
        {adding && (
          <StateForm
            defId={defId}
            onSaved={() => {
              qc.invalidateQueries({ queryKey: ['relationship-states', defId] });
              setAdding(false);
            }}
            onCancel={() => setAdding(false)}
          />
        )}
        {list.data && list.data.length === 0 && !adding && (
          <div className="text-stone-500 text-sm py-6 text-center border border-dashed border-stone-300 rounded">
            No states yet.
          </div>
        )}
      </div>
    </div>
  );
}

function StateForm({
  defId,
  initial,
  onSaved,
  onCancel,
  onDeleted,
}: {
  defId: string;
  initial?: RelationshipState;
  onSaved: () => void;
  onCancel: () => void;
  onDeleted?: () => void;
}) {
  const isNew = !initial;
  const [draft, setDraft] = useState({
    name: initial?.name ?? '',
    description: initial?.description ?? '',
    counts_as_active: initial?.counts_as_active ?? true,
  });
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => {
      const path = isNew
        ? `/config/relationship-definitions/${defId}/states`
        : `/config/relationship-states/${initial!.id}`;
      return api(path, {
        method: isNew ? 'POST' : 'PUT',
        body: JSON.stringify(draft),
      });
    },
    onSuccess: () => {
      setError(null);
      onSaved();
    },
    onError: (err) => setError(err instanceof Error ? err.message : 'save failed'),
  });

  const del = useMutation({
    mutationFn: () =>
      api(`/config/relationship-states/${initial!.id}`, { method: 'DELETE' }),
    onSuccess: () => onDeleted?.(),
    onError: (err) => setError(err instanceof Error ? err.message : 'delete failed'),
  });

  return (
    <div className="p-3 border border-forest-500 bg-forest-50 rounded space-y-2">
      <Field label="Name">
        <input
          value={draft.name}
          onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          className={inputClasses}
        />
      </Field>
      <Field label="Description">
        <input
          value={draft.description}
          onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          className={inputClasses}
        />
      </Field>
      <label className="flex items-center gap-1.5 text-sm">
        <input
          type="checkbox"
          checked={draft.counts_as_active}
          onChange={(e) => setDraft({ ...draft, counts_as_active: e.target.checked })}
        />
        Counts as active
      </label>
      <ErrorBanner message={error} />
      <div className="flex gap-2 justify-between">
        <div className="flex gap-2">
          <PrimaryButton
            onClick={() => save.mutate()}
            disabled={save.isPending || !draft.name.trim()}
          >
            {save.isPending ? 'Saving…' : isNew ? 'Create' : 'Save'}
          </PrimaryButton>
          <SecondaryButton onClick={onCancel}>Cancel</SecondaryButton>
        </div>
        {!isNew && (
          <DangerButton
            onClick={() => {
              if (confirm(`Delete state "${initial!.name}"?`)) del.mutate();
            }}
          >
            Delete
          </DangerButton>
        )}
      </div>
    </div>
  );
}
