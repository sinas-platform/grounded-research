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

interface DossierClass {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  guidance: string | null;
  summarization_guidance: string | null;
  classification_hints: string | null;
}

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 64);
}

export default function DossierClassesPage({ embedded = false }: { embedded?: boolean } = {}) {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useQuery({
    queryKey: ['dossier-classes'],
    queryFn: () => api<DossierClass[]>('/config/dossier-classes'),
  });

  const selected = list.data?.find((c) => c.id === selectedId) ?? null;

  return (
    <div>
      {!embedded && (
        <PageHeader
          title="Dossier classes"
          description="Optional. Defines kinds of document containers (cases, transactions, investigations). Leave empty for flat repositories."
          actions={
            <PrimaryButton
              onClick={() => {
                setSelectedId(null);
                setCreating(true);
              }}
            >
              New dossier class
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
            New dossier class
          </PrimaryButton>
        </div>
      )}
      <div className="grid grid-cols-12 gap-6">
        <div className="col-span-4 space-y-2">
          {(list.data ?? []).map((dc) => (
            <button
              key={dc.id}
              onClick={() => {
                setSelectedId(dc.id);
                setCreating(false);
              }}
              className={`block w-full text-left p-3 border rounded ${
                selectedId === dc.id
                  ? 'border-forest-500 bg-forest-50'
                  : 'border-stone-200 bg-white hover:border-stone-300'
              }`}
            >
              <div className="flex items-baseline justify-between">
                <div className="font-medium text-sm">{dc.name}</div>
                <div className="text-xs font-mono text-stone-400">{dc.slug}</div>
              </div>
              {dc.description && (
                <div className="text-xs text-stone-500 mt-1 line-clamp-2">{dc.description}</div>
              )}
            </button>
          ))}
          {list.data && list.data.length === 0 && (
            <div className="text-stone-500 text-sm py-8 text-center border border-dashed border-stone-300 rounded">
              No dossier classes — SGR operates as a flat document repository.
            </div>
          )}
        </div>
        <div className="col-span-8">
          {creating && (
            <Editor
              key="new"
              onSaved={(saved) => {
                qc.invalidateQueries({ queryKey: ['dossier-classes'] });
                setCreating(false);
                setSelectedId(saved.id);
              }}
            />
          )}
          {!creating && selected && (
            <Editor
              key={selected.id}
              initial={selected}
              onSaved={() => qc.invalidateQueries({ queryKey: ['dossier-classes'] })}
              onDeleted={() => {
                qc.invalidateQueries({ queryKey: ['dossier-classes'] });
                setSelectedId(null);
              }}
            />
          )}
          {!creating && !selected && (
            <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
              Select a dossier class to edit, or create a new one.
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
  initial?: DossierClass;
  onSaved: (saved: DossierClass) => void;
  onDeleted?: () => void;
}) {
  const isNew = !initial;
  const [draft, setDraft] = useState({
    name: initial?.name ?? '',
    description: initial?.description ?? '',
    guidance: initial?.guidance ?? '',
    summarization_guidance: initial?.summarization_guidance ?? '',
    classification_hints: initial?.classification_hints ?? '',
  });
  const [slug, setSlug] = useState(initial?.slug ?? '');
  const [slugTouched, setSlugTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () =>
      api<DossierClass>(
        isNew ? '/config/dossier-classes' : `/config/dossier-classes/${initial!.id}`,
        {
          method: isNew ? 'POST' : 'PUT',
          body: JSON.stringify(isNew ? { ...draft, slug: slug || slugify(draft.name) } : draft),
        },
      ),
    onSuccess: (saved) => {
      setError(null);
      onSaved(saved);
    },
    onError: (err) => setError(err instanceof Error ? err.message : 'save failed'),
  });

  const del = useMutation({
    mutationFn: () =>
      api(`/config/dossier-classes/${initial!.id}`, { method: 'DELETE' }),
    onSuccess: () => onDeleted?.(),
    onError: (err) => setError(err instanceof Error ? err.message : 'delete failed'),
  });

  return (
    <div className="space-y-3 p-4 border border-stone-200 rounded bg-white">
      <div className="flex items-center justify-between">
        <div className="text-xs text-stone-400">
          {isNew ? 'New dossier class' : `id: ${initial!.id.slice(0, 8)}`}
        </div>
        {!isNew && (
          <DangerButton
            onClick={() => {
              if (confirm(`Delete dossier class "${initial!.name}"?`)) del.mutate();
            }}
          >
            Delete
          </DangerButton>
        )}
      </div>
      <Field label="Name">
        <input
          value={draft.name}
          onChange={(e) => {
            setDraft({ ...draft, name: e.target.value });
            if (isNew && !slugTouched) setSlug(slugify(e.target.value));
          }}
          className={inputClasses}
        />
      </Field>
      <Field
        label="Slug"
        hint={isNew ? 'immutable — used in permission strings' : 'immutable'}
      >
        <input
          value={slug}
          onChange={(e) => {
            setSlug(e.target.value);
            setSlugTouched(true);
          }}
          disabled={!isNew}
          placeholder="autofilled from name"
          className={`${inputClasses} font-mono ${!isNew ? 'bg-stone-100 text-stone-500' : ''}`}
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
      <Field label="Guidance">
        <textarea
          value={draft.guidance}
          onChange={(e) => setDraft({ ...draft, guidance: e.target.value })}
          rows={3}
          className={textareaClasses}
        />
      </Field>
      <Field label="Classification hints" hint="read by dossier_assigner_agent">
        <textarea
          value={draft.classification_hints}
          onChange={(e) => setDraft({ ...draft, classification_hints: e.target.value })}
          rows={3}
          className={textareaClasses}
        />
      </Field>
      <Field label="Summarization guidance">
        <textarea
          value={draft.summarization_guidance}
          onChange={(e) => setDraft({ ...draft, summarization_guidance: e.target.value })}
          rows={3}
          className={textareaClasses}
        />
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
