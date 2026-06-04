import { useEffect, useMemo, useState } from "react";
import AppShell from "./components/AppShell";

import HomePage from "./pages/Home";
import FeaturesPage from "./pages/Features";
import PricingPage from "./pages/Pricing";
import ContactPage from "./pages/Contact";





import NotFoundPage from "./pages/NotFound";

type NavItem = [string, string] | { path: string; label: string } | { href: string; label: string };

const NAV_ITEMS: NavItem[] = [["/", "Home"], ["/features", "Features"], ["/pricing", "Pricing"], ["/contact", "Contact"]];

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
    { path: "/features", element: <FeaturesPage /> },
    { path: "/pricing", element: <PricingPage /> },
    { path: "/contact", element: <ContactPage /> }
  ], []);

  const activeRoute = routes.find((route) => route.path === path);

  return (
    <AppShell title="Nontechnical Landing" description="Build a landing page with hero, features, pricing, FAQ, contact CTA, SEO-ready copy, responsive sections, accessible navigation, polished empty/loading/error copy, and minimalist e" navItems={NAV_ITEMS} currentPath={path} onNavigate={navigate}>
      {activeRoute ? activeRoute.element : <NotFoundPage onNavigate={navigate} />}
    </AppShell>
  );
}
