import { useEffect, useMemo, useState } from "react";
import AppShell from "./components/AppShell";


import FeaturesPage from "./pages/Features";
import PricingPage from "./pages/Pricing";


import DashboardPage from "./pages/Dashboard";



import NotFoundPage from "./pages/NotFound";

type NavItem = [string, string] | { path: string; label: string } | { href: string; label: string };

const NAV_ITEMS: NavItem[] = [["/", "Home"], ["/features", "Features"], ["/pricing", "Pricing"], ["/dashboard", "Dashboard"]];

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
    { path: "/", element: <DashboardPage /> },
    { path: "/features", element: <FeaturesPage /> },
    { path: "/pricing", element: <PricingPage /> },
    { path: "/dashboard", element: <DashboardPage /> }
  ], []);

  const activeRoute = routes.find((route) => route.path === path);

  return (
    <AppShell title="Bikin dashboard task tracker profesional untuk t" description="Bikin dashboard task tracker profesional untuk tim produk. Harus ada daftar task, prioritas, owner, status progress, ringkasan metrik, state kosong yang masuk akal, dan jalankan va" navItems={NAV_ITEMS} currentPath={path} onNavigate={navigate}>
      {activeRoute ? activeRoute.element : <NotFoundPage onNavigate={navigate} />}
    </AppShell>
  );
}
