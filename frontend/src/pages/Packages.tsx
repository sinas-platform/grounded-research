import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';
import { API_BASE, client } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';

interface ValidateResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

interface PackageDiff {
  created: string[];
  updated: string[];
  unchanged: string[];
  deleted: string[];
}

interface ImportResult {
  package: string;
  version: string;
  diff: PackageDiff;
  warnings: string[];
}

async function postYaml<T>(path: string): Promise<(body: string) => Promise<T>> {
  return async (body: string) => {
    const res = await client.fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-yaml' },
      body,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `${res.status} ${res.statusText}`);
    }
    return (await res.json()) as T;
  };
}

export default function PackagesPage({ embedded = false }: { embedded?: boolean } = {}) {
  const [yamlText, setYamlText] = useState('');
  const [prune, setPrune] = useState(true);
  const [validateResult, setValidateResult] = useState<ValidateResult | null>(null);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exportName, setExportName] = useState('');
  const [exportVersion, setExportVersion] = useState('0.1.0');

  const validate = useMutation({
    mutationFn: async () => {
      setError(null);
      setImportResult(null);
      const call = await postYaml<ValidateResult>('/packages/validate');
      return call(yamlText);
    },
    onSuccess: (r) => setValidateResult(r),
    onError: (err) => setError(err instanceof Error ? err.message : 'validate failed'),
  });

  const doImport = useMutation({
    mutationFn: async () => {
      setError(null);
      const call = await postYaml<ImportResult>(`/packages/import?prune=${prune}`);
      return call(yamlText);
    },
    onSuccess: (r) => {
      setImportResult(r);
      setValidateResult(null);
    },
    onError: (err) => setError(err instanceof Error ? err.message : 'import failed'),
  });

  const onFile = (file: File) => {
    const reader = new FileReader();
    reader.onload = () => setYamlText(String(reader.result ?? ''));
    reader.readAsText(file);
  };

  const doExport = async () => {
    setError(null);
    if (!exportName.trim()) {
      setError('Enter a package name to export.');
      return;
    }
    try {
      const res = await client.fetch(
        `${API_BASE}/packages/${encodeURIComponent(exportName)}/export?version=${encodeURIComponent(
          exportVersion,
        )}`,
      );
      if (!res.ok) throw new Error(await res.text());
      const text = await res.text();
      const blob = new Blob([text], { type: 'application/x-yaml' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${exportName}.yaml`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'export failed');
    }
  };

  return (
    <div>
      {!embedded && (
        <PageHeader
          title="Packages"
          description="Import a SGR configuration YAML (document classes, entities, relationships, dossiers, playbooks) or export the current deployment."
        />
      )}

      <section className="space-y-3 mb-10">
        <h2 className="text-sm font-semibold text-stone-700">Import</h2>
        <div className="flex items-center gap-3">
          <input
            type="file"
            accept=".yaml,.yml,application/x-yaml,text/yaml"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onFile(f);
            }}
            className="text-xs"
          />
          <label className="flex items-center gap-1.5 text-xs text-stone-700">
            <input
              type="checkbox"
              checked={prune}
              onChange={(e) => setPrune(e.target.checked)}
            />
            Prune (delete managed resources missing from the manifest)
          </label>
        </div>
        <textarea
          value={yamlText}
          onChange={(e) => setYamlText(e.target.value)}
          rows={20}
          placeholder="Paste SgrPackage YAML here, or use the file picker above."
          className="w-full border border-stone-300 rounded px-3 py-2 text-xs font-mono"
        />
        <div className="flex gap-2">
          <button
            onClick={() => validate.mutate()}
            disabled={!yamlText.trim() || validate.isPending}
            className="px-3 py-1.5 rounded bg-stone-200 text-stone-800 text-sm hover:bg-stone-300 disabled:opacity-50"
          >
            {validate.isPending ? 'Validating…' : 'Validate'}
          </button>
          <button
            onClick={() => doImport.mutate()}
            disabled={!yamlText.trim() || doImport.isPending}
            className="px-3 py-1.5 rounded bg-primary-600 text-white text-sm hover:bg-primary-700 disabled:opacity-50"
          >
            {doImport.isPending ? 'Importing…' : 'Import'}
          </button>
        </div>

        {validateResult && (
          <div
            className={`text-sm rounded border px-3 py-2 ${
              validateResult.valid
                ? 'border-green-300 bg-green-50 text-green-900'
                : 'border-red-300 bg-red-50 text-red-900'
            }`}
          >
            <div className="font-medium mb-1">
              {validateResult.valid ? 'Valid' : 'Invalid'}
            </div>
            {validateResult.errors.map((e, i) => (
              <div key={i} className="font-mono text-xs">• {e}</div>
            ))}
            {validateResult.warnings.map((w, i) => (
              <div key={i} className="font-mono text-xs text-amber-700">⚠ {w}</div>
            ))}
          </div>
        )}

        {importResult && (
          <div className="text-sm rounded border border-stone-200 bg-white px-3 py-2 space-y-2">
            <div className="font-medium">
              Imported {importResult.package} @ {importResult.version}
            </div>
            <DiffList title="Created" items={importResult.diff.created} color="text-green-700" />
            <DiffList title="Updated" items={importResult.diff.updated} color="text-blue-700" />
            <DiffList title="Deleted" items={importResult.diff.deleted} color="text-red-700" />
            <DiffList
              title="Unchanged"
              items={importResult.diff.unchanged}
              color="text-stone-500"
            />
            {importResult.warnings.length > 0 && (
              <div className="text-xs text-amber-700">
                {importResult.warnings.map((w, i) => (
                  <div key={i}>⚠ {w}</div>
                ))}
              </div>
            )}
          </div>
        )}

        {error && (
          <div className="text-sm rounded border border-red-300 bg-red-50 text-red-900 px-3 py-2 whitespace-pre-wrap">
            {error}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-stone-700">Export</h2>
        <div className="flex items-end gap-2">
          <label className="block">
            <div className="text-xs font-medium text-stone-700 mb-1">Package name</div>
            <input
              value={exportName}
              onChange={(e) => setExportName(e.target.value)}
              placeholder="e.g. myapp-sgr-config"
              className="border border-stone-300 rounded px-2 py-1 text-sm font-mono w-72"
            />
          </label>
          <label className="block">
            <div className="text-xs font-medium text-stone-700 mb-1">Version</div>
            <input
              value={exportVersion}
              onChange={(e) => setExportVersion(e.target.value)}
              className="border border-stone-300 rounded px-2 py-1 text-sm font-mono w-32"
            />
          </label>
          <button
            onClick={doExport}
            className="px-3 py-1.5 rounded bg-stone-200 text-stone-800 text-sm hover:bg-stone-300"
          >
            Download YAML
          </button>
        </div>
        <div className="text-xs text-stone-500">
          Exports all resources tagged <span className="font-mono">managed_by = pkg:&lt;name&gt;</span>{' '}
          plus their nested properties, states, links, and playbook content.
        </div>
      </section>
    </div>
  );
}

function DiffList({ title, items, color }: { title: string; items: string[]; color: string }) {
  if (items.length === 0) return null;
  return (
    <div>
      <div className={`text-xs font-semibold ${color}`}>{title} ({items.length})</div>
      <ul className="text-xs font-mono ml-3">
        {items.map((it) => (
          <li key={it}>{it}</li>
        ))}
      </ul>
    </div>
  );
}
