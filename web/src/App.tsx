import { Navigate, Route, Routes } from "react-router-dom";
import { configured } from "./lib/supabase";
import { useSession } from "./lib/hooks";
import { Busy, Page } from "./components/ui";
import SignIn from "./pages/SignIn";
import Home from "./pages/Home";
import Breakdown from "./pages/Breakdown";
import ShootPlan from "./pages/ShootPlan";
import Shoots from "./pages/Shoots";
import ProjectPage from "./pages/Project";
import ShootMode from "./pages/ShootMode";
import Footage from "./pages/Footage";
import RawProjectPage from "./pages/RawProject";
import Library from "./pages/Library";

export default function App() {
  const { session, loading } = useSession();
  if (!configured) {
    return (
      <Page title="Almost there">
        <p className="muted">Set <code>VITE_SUPABASE_URL</code> and <code>VITE_SUPABASE_ANON_KEY</code> (see
          <code> web/.env.example</code>) and reload.</p>
      </Page>
    );
  }
  if (loading) return <Busy title="Loading…" />;
  if (!session) return <SignIn />;
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/breakdown" element={<Breakdown />} />
      <Route path="/plan/:uploadId" element={<ShootPlan />} />
      <Route path="/shoots" element={<Shoots />} />
      <Route path="/project/:projectId" element={<ProjectPage />} />
      <Route path="/project/:projectId/shoot" element={<ShootMode />} />
      <Route path="/footage" element={<Footage />} />
      <Route path="/footage/:rawId" element={<RawProjectPage />} />
      <Route path="/library" element={<Library />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
