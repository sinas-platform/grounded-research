import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
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

interface DocumentClass {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  classification_hints: string | null;
  summarization_guidance: string | null;
}

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 64);
}

interface DocumentClassProperty {
  id: string;
  document_class_id: string;
  name: string;
  description: string | null;
  schema: Record<string, unknown>;
  guidance: string | null;
  manual: boolean;
  required: boolean;
  cardinality: 'one' | 'many';
}

const EMPTY = {
  name: '',
  description: '',
  classification_hints: '',
  summarization_guidance: '',
};

export default function DocumentClassesPage({ embedded = false }: { embedded?: boolean } = {}) {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useQuery({
    queryKey: ['document-classes'],
    queryFn: () => api<DocumentClass[]>('/config/document-classes'),
  });

  const selected = list.data?.find((c) => c.id === selectedId) ?? null;

  return (
    <div>
      {!embedded && (
        <PageHeader
          title="Document classes"
          description="Define the kinds of documents SGR indexes. Each class has its own properties and entity-extraction rules."
          actions={
            <PrimaryButton
              onClick={() => {
                setSelectedId(null);
                setCreating(true);
              }}
            >
              New class
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
            New class
          </PrimaryButton>
        </div>
      )}

      <div className="grid grid-cols-12 gap-6">
        <div className="col-span-4 space-y-2">
          {(list.data ?? []).map((c) => (
            <button
              key={c.id}
              onClick={() => {
                setSelectedId(c.id);
                setCreating(false);
              }}
              className={`block w-full text-left p-3 border rounded ${
                selectedId === c.id
                  ? 'border-forest-500 bg-forest-50'
                  : 'border-stone-200 bg-white hover:border-stone-300'
              }`}
            >
              <div className="flex items-baseline justify-between">
                <div className="font-medium text-sm">{c.name}</div>
                <div className="text-xs font-mono text-stone-400">{c.slug}</div>
              </div>
              {c.description && (
                <div className="text-xs text-stone-500 mt-1 line-clamp-2">{c.description}</div>
              )}
            </button>
          ))}
          {list.data && list.data.length === 0 && (
            <div className="text-stone-500 text-sm py-8 text-center border border-dashed border-stone-300 rounded">
              No document classes yet.
            </div>
          )}
        </div>

        <div className="col-span-8">
          {creating && (
            <ClassEditor
              key="new"
              onSaved={(c) => {
                qc.invalidateQueries({ queryKey: ['document-classes'] });
                setCreating(false);
                setSelectedId(c.id);
              }}
            />
          )}
          {!creating && selected && (
            <>
              <ClassEditor
                key={selected.id}
                initial={selected}
                onSaved={() => qc.invalidateQueries({ queryKey: ['document-classes'] })}
                onDeleted={() => {
                  qc.invalidateQueries({ queryKey: ['document-classes'] });
                  setSelectedId(null);
                }}
              />
              <PropertiesEditor classId={selected.id} />
            </>
          )}
          {!creating && !selected && (
            <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
              Select a class to edit, or create a new one.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ClassEditor({
  initial,
  onSaved,
  onDeleted,
}: {
  initial?: DocumentClass;
  onSaved: (saved: DocumentClass) => void;
  onDeleted?: () => void;
}) {
  const isNew = !initial;
  const [draft, setDraft] = useState(
    initial
      ? {
          name: initial.name,
          description: initial.description ?? '',
          classification_hints: initial.classification_hints ?? '',
          summarization_guidance: initial.summarization_guidance ?? '',
        }
      : EMPTY,
  );
  // Slug is only mutable on create; on edit we display the existing slug read-only.
  const [slug, setSlug] = useState(initial?.slug ?? '');
  // Track whether the user has manually edited the slug; if not, autofill from name.
  const [slugTouched, setSlugTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () =>
      api<DocumentClass>(
        isNew
          ? '/config/document-classes'
          : `/config/document-classes/${initial!.id}`,
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
      api(`/config/document-classes/${initial!.id}`, { method: 'DELETE' }),
    onSuccess: () => onDeleted?.(),
    onError: (err) => setError(err instanceof Error ? err.message : 'delete failed'),
  });

  return (
    <div className="space-y-3 p-4 border border-stone-200 rounded bg-white mb-6">
      <div className="flex items-center justify-between">
        <div className="text-xs text-stone-400">
          {isNew ? 'New document class' : `id: ${initial!.id.slice(0, 8)}`}
        </div>
        {!isNew && (
          <DangerButton
            onClick={() => {
              if (confirm(`Delete document class "${initial!.name}"?`)) del.mutate();
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
      <Field label="Classification hints" hint="read by classifier_agent">
        <textarea
          value={draft.classification_hints}
          onChange={(e) => setDraft({ ...draft, classification_hints: e.target.value })}
          rows={4}
          className={textareaClasses}
        />
      </Field>
      <Field label="Summarization guidance" hint="read by summarizer_agent">
        <textarea
          value={draft.summarization_guidance}
          onChange={(e) => setDraft({ ...draft, summarization_guidance: e.target.value })}
          rows={4}
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

function PropertiesEditor({ classId }: { classId: string }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const list = useQuery({
    queryKey: ['document-class-properties', classId],
    queryFn: () =>
      api<DocumentClassProperty[]>(`/config/document-classes/${classId}/properties`),
  });

  return (
    <div className="p-4 border border-stone-200 rounded bg-white">
      <div className="flex items-center justify-between mb-3">
        <div className="font-medium">Properties</div>
        <SecondaryButton
          onClick={() => {
            setAdding(true);
            setEditing(null);
          }}
        >
          Add property
        </SecondaryButton>
      </div>
      <div className="space-y-2">
        {(list.data ?? []).map((p) =>
          editing === p.id ? (
            <PropertyForm
              key={p.id}
              classId={classId}
              initial={p}
              onSaved={() => {
                qc.invalidateQueries({ queryKey: ['document-class-properties', classId] });
                setEditing(null);
              }}
              onCancel={() => setEditing(null)}
              onDeleted={() => {
                qc.invalidateQueries({ queryKey: ['document-class-properties', classId] });
                setEditing(null);
              }}
            />
          ) : (
            <button
              key={p.id}
              onClick={() => setEditing(p.id)}
              className="block w-full text-left p-3 border border-stone-200 rounded hover:border-stone-300"
            >
              <div className="flex items-baseline justify-between">
                <div className="text-sm font-medium">{p.name}</div>
                <div className="text-xs text-stone-500">
                  {p.cardinality}
                  {p.required && ' · required'}
                  {p.manual && ' · manual'}
                </div>
              </div>
              {p.description && (
                <div className="text-xs text-stone-500 mt-1">{p.description}</div>
              )}
            </button>
          ),
        )}
        {adding && (
          <PropertyForm
            classId={classId}
            onSaved={() => {
              qc.invalidateQueries({ queryKey: ['document-class-properties', classId] });
              setAdding(false);
            }}
            onCancel={() => setAdding(false)}
          />
        )}
        {list.data && list.data.length === 0 && !adding && (
          <div className="text-stone-500 text-sm py-6 text-center border border-dashed border-stone-300 rounded">
            No properties yet.
          </div>
        )}
      </div>
    </div>
  );
}

function PropertyForm({
  classId,
  initial,
  onSaved,
  onCancel,
  onDeleted,
}: {
  classId: string;
  initial?: DocumentClassProperty;
  onSaved: () => void;
  onCancel: () => void;
  onDeleted?: () => void;
}) {
  const isNew = !initial;
  const [draft, setDraft] = useState({
    name: initial?.name ?? '',
    description: initial?.description ?? '',
    schema_text: JSON.stringify(initial?.schema ?? { type: 'string' }, null, 2),
    guidance: initial?.guidance ?? '',
    manual: initial?.manual ?? false,
    required: initial?.required ?? false,
    cardinality: initial?.cardinality ?? ('one' as 'one' | 'many'),
  });
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => {
      let schema: Record<string, unknown>;
      try {
        schema = JSON.parse(draft.schema_text);
      } catch (e) {
        throw new Error(`schema is not valid JSON: ${(e as Error).message}`);
      }
      const body = {
        name: draft.name,
        description: draft.description,
        schema,
        guidance: draft.guidance,
        manual: draft.manual,
        required: draft.required,
        cardinality: draft.cardinality,
      };
      const path = isNew
        ? `/config/document-classes/${classId}/properties`
        : `/config/document-classes/properties/${initial!.id}`;
      return api(path, { method: isNew ? 'POST' : 'PUT', body: JSON.stringify(body) });
    },
    onSuccess: () => {
      setError(null);
      onSaved();
    },
    onError: (err) => setError(err instanceof Error ? err.message : 'save failed'),
  });

  const del = useMutation({
    mutationFn: () =>
      api(`/config/document-classes/properties/${initial!.id}`, { method: 'DELETE' }),
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
      <Field label="JSON Schema" hint="value shape — e.g. {&quot;type&quot;: &quot;string&quot;}">
        <textarea
          value={draft.schema_text}
          onChange={(e) => setDraft({ ...draft, schema_text: e.target.value })}
          rows={4}
          className={textareaClasses}
        />
      </Field>
      <Field label="Guidance" hint="read by property_extractor_agent">
        <textarea
          value={draft.guidance}
          onChange={(e) => setDraft({ ...draft, guidance: e.target.value })}
          rows={3}
          className={textareaClasses}
        />
      </Field>
      <div className="flex gap-4 text-sm">
        <label className="flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={draft.required}
            onChange={(e) => setDraft({ ...draft, required: e.target.checked })}
          />
          Required
        </label>
        <label className="flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={draft.manual}
            onChange={(e) => setDraft({ ...draft, manual: e.target.checked })}
          />
          Manual
        </label>
        <label className="flex items-center gap-1.5">
          Cardinality
          <select
            value={draft.cardinality}
            onChange={(e) =>
              setDraft({ ...draft, cardinality: e.target.value as 'one' | 'many' })
            }
            className="border border-stone-300 rounded px-2 py-0.5 text-sm"
          >
            <option value="one">one</option>
            <option value="many">many</option>
          </select>
        </label>
      </div>
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
              if (confirm(`Delete property "${initial!.name}"?`)) del.mutate();
            }}
          >
            Delete
          </DangerButton>
        )}
      </div>
    </div>
  );
}

