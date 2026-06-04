import { useEffect, useMemo, useState } from "react";
import AppShell from "./components/AppShell";

import HomePage from "./pages/Home";





import WorkspacePage from "./pages/Workspace";
import IntegrationsPage from "./pages/Integrations";
import SettingsPage from "./pages/AppSettings";
import NotFoundPage from "./pages/NotFound";

type NavItem = [string, string] | { path: string; label: string } | { href: string; label: string };

const NAV_ITEMS: NavItem[] = [["/", "Overview"], ["/workspace", "Workspace"], ["/integrations", "Integrations"], ["/settings", "Settings"]];

function normalizePath(path: string) {
  const clean = (path || "/").split("#")[0].split("?")[0] || "/";
  return clean.startsWith("/") ? clean : "/" + clean;
}

export default function App() {
  const [path, setPath] = useState(() => normalizePath(window.location.pathname));

  useEffect(() => {
    const onPopState = () => setPath(normalizePath(window.location.pathname));
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const navigate = (nextPath: string) => {
    const normalized = normalizePath(nextPath);
    if (normalized !== path) {
      window.history.pushState(null, "", normalized);
      setPath(normalized);
    }
  };

  const routes = useMemo(() => [
    { path: "/", element: <HomePage /> },
    { path: "/workspace", element: <WorkspacePage /> },
    { path: "/integrations", element: <IntegrationsPage /> },
    { path: "/settings", element: <SettingsPage /> }
  ], []);

  const activeRoute = routes.find((route) => route.path === path);

  return (
    <AppShell title="Preview Blank Repair" description="Build a modern personal portfolio site with hero, selected projects, skills, process, about section, contact CTA, responsive project cards, accessible navigation, polished empty/lo" navItems={NAV_ITEMS} currentPath={path} onNavigate={navigate}>
      {activeRoute ? activeRoute.element : <NotFoundPage onNavigate={navigate} />}
    </AppShell>
  );
}
