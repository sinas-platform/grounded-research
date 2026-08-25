import { Navigate, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { useAuth } from './lib/auth';
import LoginPage from './pages/Login';

// Top-level pages
import DocumentsPage from './pages/Documents';
import DocumentDetailPage from './pages/DocumentDetail';
import RunsPage from './pages/Runs';
import SchemaPage from './pages/Schema';
import ActivityPage from './pages/Activity';

// Deeper / direct-link routes — still reachable, just not in the sidebar
import ResultsPage from './pages/Results';
import DocumentClassesPage from './pages/DocumentClasses';
import EntityTypesPage from './pages/EntityTypes';
import RelationshipsPage from './pages/Relationships';
import DossierClassesPage from './pages/DossierClasses';
import PlaybooksPage from './pages/Playbooks';
import PackagesPage from './pages/Packages';
import EntityReviewPage from './pages/EntityReview';
import IngestionRunsPage from './pages/IngestionRuns';
import DiscoveryPage from './pages/Discovery';
import SinasStatusPage from './pages/SinasStatus';

export default function App() {
  const { status } = useAuth();

  if (status === 'loading') {
    return (
      <div className="min-h-screen flex items-center justify-center text-stone-500">
        Loading…
      </div>
    );
  }
  if (status === 'unauthenticated') {
    return <LoginPage />;
  }

  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Navigate to="/runs" replace />} />

        {/* Sidebar pages */}
        <Route path="documents" element={<DocumentsPage />} />
        <Route path="documents/:id" element={<DocumentDetailPage />} />
        <Route path="runs" element={<RunsPage />} />
        <Route path="schema" element={<SchemaPage />} />
        <Route path="activity" element={<ActivityPage />} />

        {/* Direct-link / drilldown routes (not in sidebar) */}
        <Route path="results" element={<ResultsPage />} />
        <Route path="config/document-classes" element={<DocumentClassesPage />} />
        <Route path="config/entity-types" element={<EntityTypesPage />} />
        <Route path="config/relationships" element={<RelationshipsPage />} />
        <Route path="config/dossier-classes" element={<DossierClassesPage />} />
        <Route path="config/playbooks" element={<PlaybooksPage />} />
        <Route path="config/packages" element={<PackagesPage />} />
        <Route path="review/entities" element={<EntityReviewPage />} />
        <Route path="ingestion/runs" element={<IngestionRunsPage />} />
        <Route path="discovery" element={<DiscoveryPage />} />
        <Route path="sinas-status" element={<SinasStatusPage />} />
      </Route>
    </Routes>
  );
}
