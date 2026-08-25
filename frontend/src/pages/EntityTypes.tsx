import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';
import {
  DangerButton,
  ErrorBanner,
  Field,
  PrimaryButton,
  inputClasses,
  textareaClasses,
} from '@/components/Form';

type CreationMode = 'open' | 'review' | 'closed';

interface EntityType {
  id: string;
  name: string;
  description: string | null;
  guidance: string | null;
  creation_mode: CreationMode;
}

const MODE_LABEL: Record<CreationMode, string> = {
  open: 'Allow',
  review: 'Review',
  closed: 'Lock',
};
const MODE_HINT: Record<CreationMode, string> = {
  open: 'Agent may auto-create new entities of this type.',
  review: 'New entities are held as proposals until a human approves them.',
  closed: 'Only entities already in the table can match. Unknown mentions are parked for review.',
};
const MODE_BADGE: Record<CreationMode, string> = {
  open: 'bg-stone-100 text-stone-700',
  review: 'bg-amber-100 text-amber-800',
  closed: 'bg-red-100 text-red-800',
};

export default function EntityTypesPage({ embedded = false }: { embedded?: boolean } = {}) {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useQuery({
    queryKey: ['entity-types'],
    queryFn: () => api<EntityType[]>('/config/entity-types'),
  });

  const selected = list.data?.find((e) => e.id === selectedId) ?? null;

  return (
    <div>
      {!embedded && (
        <PageHeader
          title="Entity types"
          description="Canonical kinds of entities the entity_extractor_agent looks for inside documents."
          actions={
            <PrimaryButton
              onClick={() => {
                setSelectedId(null);
                setCreating(true);
              }}
            >
              New entity type
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
            New entity type
          </PrimaryButton>
        </div>
      )}
      <div className="grid grid-cols-12 gap-6">
        <div className="col-span-4 space-y-2">
          {(list.data ?? []).map((et) => (
            <button
              key={et.id}
              onClick={() => {
                setSelectedId(et.id);
                setCreating(false);
              }}
              className={`block w-full text-left p-3 border rounded ${
                selectedId === et.id
                  ? 'border-primary-500 bg-primary-50'
                  : 'border-stone-200 bg-white hover:border-stone-300'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="font-medium text-sm">{et.name}</div>
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded uppercase tracking-wider ${MODE_BADGE[et.creation_mode]}`}
                >
                  {MODE_LABEL[et.creation_mode]}
                </span>
              </div>
              {et.description && (
                <div className="text-xs text-stone-500 mt-1 line-clamp-2">{et.description}</div>
              )}
            </button>
          ))}
          {list.data && list.data.length === 0 && (
            <div className="text-stone-500 text-sm py-8 text-center border border-dashed border-stone-300 rounded">
              No entity types yet.
            </div>
          )}
        </div>
        <div className="col-span-8">
          {creating && (
            <Editor
              key="new"
              onSaved={(saved) => {
                qc.invalidateQueries({ queryKey: ['entity-types'] });
                setCreating(false);
                setSelectedId(saved.id);
              }}
            />
          )}
          {!creating && selected && (
            <Editor
              key={selected.id}
              initial={selected}
              onSaved={() => qc.invalidateQueries({ queryKey: ['entity-types'] })}
              onDeleted={() => {
                qc.invalidateQueries({ queryKey: ['entity-types'] });
                setSelectedId(null);
              }}
            />
          )}
          {!creating && !selected && (
            <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
              Select an entity type to edit, or create a new one.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Editor({
  initial,
  onSaved,
  onDeleted,
}: {
  initial?: EntityType;
  onSaved: (saved: EntityType) => void;
  onDeleted?: () => void;
}) {
  const isNew = !initial;
  const [draft, setDraft] = useState({
    name: initial?.name ?? '',
    description: initial?.description ?? '',
    guidance: initial?.guidance ?? '',
    creation_mode: (initial?.creation_mode ?? 'open') as CreationMode,
  });
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () =>
      api<EntityType>(
        isNew ? '/config/entity-types' : `/config/entity-types/${initial!.id}`,
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
      api(`/config/entity-types/${initial!.id}`, { method: 'DELETE' }),
    onSuccess: () => onDeleted?.(),
    onError: (err) => setError(err instanceof Error ? err.message : 'delete failed'),
  });

  return (
    <div className="space-y-3 p-4 border border-stone-200 rounded bg-white">
      <div className="flex items-center justify-between">
        <div className="text-xs text-stone-400">
          {isNew ? 'New entity type' : `id: ${initial!.id.slice(0, 8)}`}
        </div>
        {!isNew && (
          <DangerButton
            onClick={() => {
              if (confirm(`Delete entity type "${initial!.name}"?`)) del.mutate();
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
      <Field label="Guidance" hint="read by entity_extractor_agent">
        <textarea
          value={draft.guidance}
          onChange={(e) => setDraft({ ...draft, guidance: e.target.value })}
          rows={4}
          className={textareaClasses}
        />
      </Field>
      <Field label="Creation mode" hint="what happens when the extractor mentions a new value">
        <div className="space-y-1">
          {(['open', 'review', 'closed'] as CreationMode[]).map((m) => (
            <label key={m} className="flex items-start gap-2 text-sm">
              <input
                type="radio"
                name="creation_mode"
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
          disabled={save.isPending || !draft.name.trim()}
        >
          {save.isPending ? 'Saving…' : isNew ? 'Create' : 'Save'}
        </PrimaryButton>
      </div>
    </div>
  );
}
