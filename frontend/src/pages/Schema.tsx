import { useState } from 'react';
import { PageHeader } from '@/components/PageHeader';
import DocumentClassesPage from './DocumentClasses';
import EntityTypesPage from './EntityTypes';
import RelationshipsPage from './Relationships';
import DossierClassesPage from './DossierClasses';
import PlaybooksPage from './Playbooks';
import PackagesPage from './Packages';

type Tab =
  | 'document_classes'
  | 'entity_types'
  | 'relationships'
  | 'dossier_classes'
  | 'playbooks'
  | 'packages';

const TABS: { value: Tab; label: string; sub: string }[] = [
  { value: 'document_classes', label: 'Document classes', sub: 'Kinds of documents SGR indexes' },
  { value: 'entity_types', label: 'Entity types', sub: 'Things extracted from documents' },
  { value: 'relationships', label: 'Relationships', sub: 'Edges between docs / entities / dossiers' },
  { value: 'dossier_classes', label: 'Dossier classes', sub: 'Optional document containers' },
  { value: 'playbooks', label: 'Playbooks', sub: 'Markdown skills agents load on demand' },
  { value: 'packages', label: 'Packages', sub: 'Import / export the full domain config as a YAML file' },
];

export default function SchemaPage() {
  const [tab, setTab] = useState<Tab>('document_classes');
  const active = TABS.find((t) => t.value === tab)!;

  return (
    <div>
      <PageHeader
        title="Schema"
        description="The deployment's domain model — classes, entities, relationships, dossiers, and the playbooks agents load at runtime."
      />

      <nav className="flex flex-wrap gap-1 border-b border-stone-200 mb-2">
        {TABS.map((t) => (
          <button
            key={t.value}
            onClick={() => setTab(t.value)}
            className={`px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${
              tab === t.value
                ? 'border-primary-600 text-primary-700 font-medium'
                : 'border-transparent text-stone-600 hover:text-stone-900'
            }`}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <div className="text-xs text-stone-500 mb-6">{active.sub}</div>

      {tab === 'document_classes' && <DocumentClassesPage embedded />}
      {tab === 'entity_types' && <EntityTypesPage embedded />}
      {tab === 'relationships' && <RelationshipsPage embedded />}
      {tab === 'dossier_classes' && <DossierClassesPage embedded />}
      {tab === 'playbooks' && <PlaybooksPage embedded />}
      {tab === 'packages' && <PackagesPage embedded />}
    </div>
  );
}
