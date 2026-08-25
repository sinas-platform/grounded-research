import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';

interface Playbook {
  id: string;
  kind: 'retrieval' | 'synthesis';
  name: string;
  description: string;
  content: string;
}

const KINDS: { value: 'retrieval' | 'synthesis'; label: string }[] = [
  { value: 'retrieval', label: 'Retrieval' },
  { value: 'synthesis', label: 'Synthesis' },
];

export default function PlaybooksPage({ embedded = false }: { embedded?: boolean } = {}) {
  const qc = useQueryClient();
  const [kind, setKind] = useState<'retrieval' | 'synthesis'>('retrieval');
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useQuery({
    queryKey: ['playbooks', kind],
    queryFn: () => api<Playbook[]>(`/playbooks/${kind}`),
  });

  useEffect(() => {
    setSelected(null);
    setCreating(false);
  }, [kind]);

  const onSaved = () => {
    void qc.invalidateQueries({ queryKey: ['playbooks', kind] });
    setCreating(false);
  };

  const onDeleted = () => {
    void qc.invalidateQueries({ queryKey: ['playbooks', kind] });
    setSelected(null);
  };

  const selectedPlaybook = list.data?.find((p) => p.name === selected) ?? null;

  const newButton = (
    <button
      onClick={() => {
        setSelected(null);
        setCreating(true);
      }}
      className="px-3 py-1.5 rounded bg-primary-600 text-white text-sm hover:bg-primary-700"
    >
      New playbook
    </button>
  );

  return (
    <div>
      {!embedded && (
        <PageHeader
          title="Playbooks"
          description="Markdown guidance the deep-search and synthesis agents fetch at runtime."
          actions={newButton}
        />
      )}
      {embedded && <div className="mb-4 flex justify-end">{newButton}</div>}
      <div className="flex gap-2 mb-4">
        {KINDS.map((k) => (
          <button
            key={k.value}
            onClick={() => setKind(k.value)}
            className={`px-3 py-1.5 rounded text-sm ${
              kind === k.value
                ? 'bg-primary-100 text-primary-700 font-medium'
                : 'text-stone-700 hover:bg-stone-100'
            }`}
          >
            {k.label}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-12 gap-6">
        <div className="col-span-4 space-y-2">
          {(list.data ?? []).map((p) => (
            <button
              key={p.name}
              onClick={() => {
                setSelected(p.name);
                setCreating(false);
              }}
              className={`block w-full text-left p-3 border rounded ${
                selected === p.name
                  ? 'border-primary-500 bg-primary-50'
                  : 'border-stone-200 bg-white hover:border-stone-300'
              }`}
            >
              <div className="font-mono text-sm text-primary-700">{p.name}</div>
              {p.description && (
                <div className="text-xs text-stone-500 mt-1 line-clamp-2">{p.description}</div>
              )}
            </button>
          ))}
          {list.data && list.data.length === 0 && (
            <div className="text-stone-500 text-sm py-8 text-center border border-dashed border-stone-300 rounded">
              No {kind} playbooks yet.
            </div>
          )}
        </div>

        <div className="col-span-8">
          {creating && <PlaybookEditor kind={kind} onSaved={onSaved} />}
          {!creating && selectedPlaybook && (
            <PlaybookEditor
              kind={kind}
              key={selectedPlaybook.name}
              initial={selectedPlaybook}
              onSaved={onSaved}
              onDeleted={onDeleted}
            />
          )}
          {!creating && !selectedPlaybook && (
            <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
              Select a playbook to edit, or create a new one.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function PlaybookEditor({
  kind,
  initial,
  onSaved,
  onDeleted,
}: {
  kind: 'retrieval' | 'synthesis';
  initial?: Playbook;
  onSaved: () => void;
  onDeleted?: () => void;
}) {
  const isNew = !initial;
  const [name, setName] = useState(initial?.name ?? '');
  const [description, setDescription] = useState(initial?.description ?? '');
  const [content, setContent] = useState(initial?.content ?? '');
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () =>
      api<Playbook>(`/playbooks/${kind}/${name}`, {
        method: 'PUT',
        body: JSON.stringify({ kind, name, description, content }),
      }),
    onSuccess: () => {
      setError(null);
      onSaved();
    },
    onError: (err) => setError(err instanceof Error ? err.message : 'save failed'),
  });

  const del = useMutation({
    mutationFn: () => api(`/playbooks/${kind}/${name}`, { method: 'DELETE' }),
    onSuccess: () => onDeleted?.(),
    onError: (err) => setError(err instanceof Error ? err.message : 'delete failed'),
  });

  return (
    <div className="space-y-3 p-4 border border-stone-200 rounded bg-white">
      <div className="flex items-center justify-between">
        <div className="font-mono text-xs text-stone-400">
          {kind} playbook
        </div>
        {!isNew && (
          <button
            onClick={() => {
              if (confirm(`Delete playbook ${name}?`)) del.mutate();
            }}
            className="px-2 py-1 rounded text-xs border border-red-300 text-red-700 hover:bg-red-50"
          >
            Delete
          </button>
        )}
      </div>
      <Field label="Name (kebab-case)">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={!isNew}
          className="w-full border border-stone-300 rounded px-2 py-1 text-sm font-mono disabled:bg-stone-50"
        />
      </Field>
      <Field label="Description (used by the LLM to pick this playbook)">
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          className="w-full border border-stone-300 rounded px-2 py-1 text-sm"
        />
      </Field>
      <Field label="Content (markdown)">
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={20}
          className="w-full border border-stone-300 rounded px-2 py-2 text-sm font-mono"
        />
      </Field>
      {error && (
        <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
          {error}
        </div>
      )}
      <div className="flex gap-2">
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending || !name.trim() || !description.trim()}
          className="px-3 py-1.5 rounded bg-primary-600 text-white text-sm hover:bg-primary-700 disabled:opacity-50"
        >
          {save.isPending ? 'Saving…' : isNew ? 'Create' : 'Save'}
        </button>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-xs font-medium text-stone-700 mb-1">{label}</div>
      {children}
    </label>
  );
}
