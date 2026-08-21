import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, tokens } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';

interface Document {
  id: string;
  filename: string;
  summary: string | null;
  document_class_id: string | null;
  classification_confidence: number | null;
  staged: boolean;
}

interface DocumentCounts {
  total: number;
  unclassified: number;
  staged: number;
  by_class: Record<string, number>;
}

type View = 'live' | 'staged';

export default function DocumentsPage() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUploaded, setLastUploaded] = useState<string | null>(null);
  const [stageNext, setStageNext] = useState(false);
  const [view, setView] = useState<View>('live');

  const { data } = useQuery({
    queryKey: ['documents', view],
    queryFn: () =>
      api<Document[]>(view === 'staged' ? '/documents?staged_only=true' : '/documents'),
    refetchInterval: lastUploaded ? 3000 : false,
  });
  const counts = useQuery({
    queryKey: ['documents-counts'],
    queryFn: () => api<DocumentCounts>('/documents/counts'),
    refetchInterval: lastUploaded ? 3000 : false,
  });

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const fd = new FormData();
      fd.append('file', file);
      if (stageNext) fd.append('staged', 'true');
      // No Content-Type header — let the browser set the multipart boundary.
      const access = tokens.access;
      const res = await fetch('/api/v1/uploads', {
        method: 'POST',
        body: fd,
        headers: access ? { Authorization: `Bearer ${access}` } : {},
      });
      if (!res.ok) {
        throw new Error(`${res.status}: ${await res.text()}`);
      }
      return res.json();
    },
    onSuccess: (_, file) => {
      setLastUploaded(file.name);
      setError(null);
      void qc.invalidateQueries({ queryKey: ['documents'] });
      void qc.invalidateQueries({ queryKey: ['documents-counts'] });
    },
    onError: (err) => {
      setError(err instanceof Error ? err.message : 'upload failed');
      setLastUploaded(null);
    },
  });

  const onFiles = async (files: FileList | File[]) => {
    setBusy(true);
    setError(null);
    try {
      for (const f of Array.from(files)) {
        await upload.mutateAsync(f);
      }
    } finally {
      setBusy(false);
    }
  };

  const promoteStaged = useMutation({
    mutationFn: () =>
      api('/ingestion/runs', {
        method: 'POST',
        body: JSON.stringify({
          // parts omitted = the full pipeline
          filter: { staged_only: true },
          dry_run: false,
        }),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['documents'] });
      void qc.invalidateQueries({ queryKey: ['documents-counts'] });
    },
  });

  return (
    <div>
      <PageHeader
        title="Documents"
        description="Drop files to ingest. Uploads land in the Sinas sgr/documents collection and trigger ingestion automatically — unless staged."
        actions={
          <button
            onClick={() => fileRef.current?.click()}
            className="px-3 py-1.5 rounded bg-forest-600 text-white text-sm hover:bg-forest-700"
          >
            Upload
          </button>
        }
      />
      <input
        ref={fileRef}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => e.target.files && onFiles(e.target.files)}
      />

      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (e.dataTransfer.files.length) void onFiles(e.dataTransfer.files);
        }}
        className={`mb-3 px-6 py-10 text-center border-2 border-dashed rounded transition-colors ${
          dragOver
            ? 'border-forest-500 bg-forest-50'
            : 'border-stone-300 bg-white text-stone-500'
        }`}
      >
        {busy ? 'Uploading…' : 'Drop files here, or click Upload'}
      </div>

      <label className="mb-6 flex items-start gap-2 text-sm text-stone-700">
        <input
          type="checkbox"
          checked={stageNext}
          onChange={(e) => setStageNext(e.target.checked)}
          className="mt-0.5"
        />
        <span>
          <span className="font-medium">Stage for discovery</span>
          <span className="block text-xs text-stone-500">
            Park the file without running the classifier or extractors. Use when you're
            still designing the schema — discovery and front-matter scans can read staged
            docs. Promote them with one click once your config is ready.
          </span>
        </span>
      </label>

      {counts.data && (counts.data.staged > 0 || view === 'staged') && (
        <div className="mb-4 flex items-center gap-3 p-3 rounded border border-amber-200 bg-amber-50">
          <div className="text-sm text-amber-900">
            <b>{counts.data.staged}</b> staged doc{counts.data.staged === 1 ? '' : 's'} —
            parked from the auto-pipeline.
          </div>
          <div className="ml-auto flex gap-2">
            <button
              onClick={() => setView(view === 'staged' ? 'live' : 'staged')}
              className="px-2 py-1 text-xs rounded border border-amber-300 text-amber-900 hover:bg-amber-100"
            >
              {view === 'staged' ? 'Show live docs' : 'Show staged'}
            </button>
            {counts.data.staged > 0 && (
              <button
                onClick={() => promoteStaged.mutate()}
                disabled={promoteStaged.isPending}
                className="px-3 py-1 text-xs rounded bg-forest-600 text-white hover:bg-forest-700 disabled:opacity-50"
              >
                {promoteStaged.isPending ? 'Starting…' : 'Promote staged → run pipeline'}
              </button>
            )}
          </div>
        </div>
      )}

      {lastUploaded && (
        <div className="mb-4 text-sm text-forest-700 bg-forest-50 border border-forest-100 rounded px-3 py-2">
          Uploaded <span className="font-mono">{lastUploaded}</span>. Ingestion is running in the
          background — refresh in a few seconds to see it.
        </div>
      )}
      {error && (
        <div className="mb-4 text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
          {error}
        </div>
      )}

      <div className="space-y-2">
        {(data ?? []).map((d) => (
          <Link
            key={d.id}
            to={`/documents/${d.id}`}
            className="block p-4 border border-stone-200 rounded bg-white hover:border-forest-500"
          >
            <div className="flex items-baseline justify-between">
              <div className="font-medium flex items-center gap-2">
                {d.filename}
                {d.staged && (
                  <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 border border-amber-300">
                    Staged
                  </span>
                )}
              </div>
              <div className="text-xs text-stone-400">
                {d.staged ? (
                  <span className="text-amber-700">not indexed</span>
                ) : d.document_class_id ? (
                  d.classification_confidence != null ? (
                    <>conf {d.classification_confidence.toFixed(2)}</>
                  ) : (
                    'classified'
                  )
                ) : (
                  <span className="text-amber-600">awaiting classification</span>
                )}
              </div>
            </div>
            {d.summary && (
              <div className="text-sm text-stone-600 mt-1 line-clamp-2">{d.summary}</div>
            )}
          </Link>
        ))}
        {data && data.length === 0 && (
          <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
            No documents yet. Upload some to get started.
          </div>
        )}
      </div>
    </div>
  );
}
