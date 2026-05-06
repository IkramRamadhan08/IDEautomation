from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "voice-app"


def _join(root: str, rel: str) -> str:
    root = (root or ".").strip()
    rel = rel.strip().lstrip("/")
    return rel if root in {"", "."} else f"{root}/{rel}"


def _summarize_instruction(text: str) -> tuple[str, str]:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return ("Build a polished web experience", "A runnable web app prepared in hybrid mode.")

    short = clean[:80].strip()
    if len(clean) > 80:
        short += "…"
    description = clean[:180].strip()
    return (short, description)


def _project_title(project_name: str, instruction: str) -> str:
    short, _ = _summarize_instruction(instruction)
    if any(ch.isalpha() for ch in short):
        return short[:48]
    return project_name or "Hybrid Builder"


def _project_description(instruction: str) -> str:
    _short, description = _summarize_instruction(instruction)
    return description


BOOTSTRAP_MARKERS = (
    "vite + react",
    "count is",
    "edit src/app.tsx and save to test hmr",
)


REQUIRED_FILES = (
    "package.json",
    "index.html",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "vite.config.ts",
    "src/main.tsx",
    "src/App.tsx",
    "src/app.css",
)


def project_is_runnable(project_dir: Path) -> bool:
    package_json = project_dir / "package.json"
    if not package_json.exists():
        return False
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
        scripts = data.get("scripts") or {}
        if not isinstance(scripts, dict) or "dev" not in scripts:
            return False
    except Exception:
        return False

    return all((project_dir / rel).exists() for rel in ["index.html", "src/main.tsx", "src/App.tsx"])


def project_looks_like_bootstrap(project_dir: Path) -> bool:
    app_path = project_dir / "src" / "App.tsx"
    if not app_path.exists():
        return False
    try:
        text = app_path.read_text(encoding="utf-8").lower()
    except Exception:
        return False
    return any(marker in text for marker in BOOTSTRAP_MARKERS)


def should_seed_hybrid(project_dir: Path) -> bool:
    return (not project_is_runnable(project_dir)) or project_looks_like_bootstrap(project_dir)


def build_hybrid_seed(project_root: str, project_name: str, instruction: str) -> dict[str, str]:
    """Seed a preview-ready React+Vite app for FULL AGENT mode.

    This seed is intentionally "pleasantly overbuilt" so the agent can iterate
    without first fighting boilerplate, while keeping file count reasonable.
    """

    title = _project_title(project_name, instruction)
    description = _project_description(instruction)
    package_name = _slug(project_name or title)

    hint = (instruction or "").lower()
    wants_dashboard = any(k in hint for k in ["dashboard", "admin", "panel", "backoffice", "back-office", "crm", "analytics", "inventory", "billing", "operations"])
    wants_docs = any(k in hint for k in ["docs", "documentation", "wiki", "knowledge base", "knowledge", "blog", "changelog"])
    wants_landing = any(k in hint for k in ["landing page", "landing", "marketing", "homepage", "hero section", "promo", "campaign"])
    wants_app = any(k in hint for k in ["app", "platform", "workspace", "portal", "workflow", "automation", "tool", "product", "saas"])

    template = "dashboard" if wants_dashboard and not wants_landing else "docs" if wants_docs else "landing" if wants_landing else "app"

    if template == "app":
        nav_items = [
            ("/", "Overview"),
            ("/workspace", "Workspace"),
            ("/integrations", "Integrations"),
            ("/settings", "Settings"),
        ]
        home_highlights = [
            "Multi-view workspace shell",
            "Integrations surface + system status",
            "Settings and operational states",
            "Responsive app foundation with reusable UI",
        ]
        primary_cta = "Open workspace"
        secondary_cta = "Review integrations"
        features_title = "Workspace"
        features_items = [
            {"title": "Queues and states", "body": "Scaffold recent activity, pending work, and clear operational states instead of a static promo wall."},
            {"title": "Task surfaces", "body": "Give the app useful panels, cards, tables, or flows that feel like a real product workspace."},
            {"title": "Guided actions", "body": "Prefer intentional next actions over generic marketing filler when the brief sounds product-like."},
            {"title": "Extension points", "body": "Leave room for auth, billing, analytics, or role-based views without boxing the project in."},
        ]
        pricing_title = "Integrations"
        pricing_items = [
            {"name": "Content", "price": "Ready", "desc": "CMS, uploads, and rich content flows", "perks": ["Media handling", "Draft/publish flow", "Validation hooks"]},
            {"name": "Data", "price": "Ready", "desc": "Backends, RAG, and persistence layers", "perks": ["Structured data", "Supabase-ready", "Traceable retrieval"]},
            {"name": "Automation", "price": "Ready", "desc": "Agent and tool assisted workflows", "perks": ["Tool hooks", "Auditability", "Bounded autonomy"]},
        ]
    else:
        home_highlights = [
            "Multi-page routing scaffold",
            "Reusable UI components",
            "Light/dark theme with design tokens",
            "Responsive layout + accessible defaults",
        ]
        primary_cta = "Get started"
        secondary_cta = "See demo"
        features_title = "Features"
        features_items = [
            {"title": "Design system", "body": "CSS variables + consistent spacing, radii, shadows."},
            {"title": "Navigation", "body": "Router + active links + layout shell."},
            {"title": "States", "body": "Empty/loading/error patterns you can extend."},
            {"title": "Polish", "body": "Focus states, contrast, responsive grid."},
        ]
        pricing_title = "Pricing"
        pricing_items = [
            {"name": "Starter", "price": "$0", "desc": "For prototyping and demos", "perks": ["Basic pages", "Theme toggle", "Router"]},
            {"name": "Pro", "price": "$19", "desc": "For real products", "perks": ["Better UX", "More components", "Polish"]},
            {"name": "Team", "price": "$49", "desc": "For teams", "perks": ["Shared workflows", "Design tokens", "Scalable layout"]},
        ]
        nav_items = [
            ("/", "Home"),
            ("/features", "Features"),
            ("/pricing", "Pricing"),
        ]
        if template == "landing":
            nav_items.append(("/contact", "Contact"))
        elif template == "docs":
            nav_items.append(("/docs", "Docs"))
            features_title = "Guides"
            pricing_title = "Reference"
        elif template == "dashboard":
            nav_items.append(("/dashboard", "Dashboard"))
            features_title = "Metrics"
            pricing_title = "Operations"

    landing_sections: list[str] = []
    if template == "landing":
        section_keywords = [
            ("Testimonials", ["testimonial", "testimoni", "review pelanggan"]),
            ("FAQ", ["faq", "frequently asked", "pertanyaan"]),
            ("Contact", ["contact", "kontak", "contact form"]),
            ("Pricing", ["pricing", "harga", "plans"]),
            ("Feature grid", ["feature", "fitur", "benefit"]),
            ("CTA", ["cta", "call to action"]),
        ]
        for label, keywords in section_keywords:
            if any(keyword in hint for keyword in keywords) and label not in landing_sections:
                landing_sections.append(label)
        if not landing_sections:
            landing_sections = ["Feature grid", "Testimonials", "CTA"]

    nav_json = json.dumps(nav_items, ensure_ascii=False)
    home_highlights_json = json.dumps(home_highlights, ensure_ascii=False)
    landing_sections_json = json.dumps(landing_sections, ensure_ascii=False)
    features_items_json = json.dumps(features_items, ensure_ascii=False)
    pricing_items_json = json.dumps(pricing_items, ensure_ascii=False)

    files = {
        "package.json": f'''{{
  "name": "{package_name}",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {{
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview"
  }},
  "dependencies": {{
    "react": "^19.1.0",
    "react-dom": "^19.1.0",
    "react-router-dom": "^6.27.0"
  }},
  "devDependencies": {{
    "@types/react": "^19.1.2",
    "@types/react-dom": "^19.1.2",
    "@vitejs/plugin-react": "^5.0.4",
    "typescript": "~5.8.3",
    "vite": "^7.1.2"
  }}
}}''',
        "index.html": f'''<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#0b1220" />
    <title>{title}</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
''',
        "tsconfig.json": '''{
  "files": [],
  "references": [
    { "path": "./tsconfig.app.json" },
    { "path": "./tsconfig.node.json" }
  ]
}
''',
        "tsconfig.app.json": '''{
  "compilerOptions": {
    "target": "ES2022",
    "useDefineForClassFields": true,
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "Bundler",
    "allowImportingTsExtensions": false,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true
  },
  "include": ["src"]
}
''',
        "tsconfig.node.json": '''{
  "compilerOptions": {
    "target": "ES2023",
    "lib": ["ES2023"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "Bundler",
    "allowSyntheticDefaultImports": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true
  },
  "include": ["vite.config.ts"]
}
''',
        "vite.config.ts": '''import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
});
''',
        "src/main.tsx": '''import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./app.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
''',
        "src/App.tsx": f'''import {{ Route, Routes }} from "react-router-dom";
import AppShell from "./components/AppShell";

import HomePage from "./pages/Home";
{'import FeaturesPage from "./pages/Features";' if template != 'app' else ''}
{'import PricingPage from "./pages/Pricing";' if template != 'app' else ''}
{'import ContactPage from "./pages/Contact";' if template == 'landing' else ''}
{'import DocsPage from "./pages/Docs";' if template == 'docs' else ''}
{'import DashboardPage from "./pages/Dashboard";' if template == 'dashboard' else ''}
{'import WorkspacePage from "./pages/Workspace";' if template == 'app' else ''}
{'import IntegrationsPage from "./pages/Integrations";' if template == 'app' else ''}
{'import SettingsPage from "./pages/AppSettings";' if template == 'app' else ''}
import NotFoundPage from "./pages/NotFound";

const NAV_ITEMS: Array<[string, string]> = {nav_json} as any;

export default function App() {{
  return (
    <AppShell title={json.dumps(title)} description={json.dumps(description)} navItems={{NAV_ITEMS}}>
      <Routes>
        <Route path="/" element={{<HomePage />}} />
        {('<Route path="/workspace" element={<WorkspacePage />} />' if template == 'app' else '<Route path="/features" element={<FeaturesPage />} />')}
        {('<Route path="/integrations" element={<IntegrationsPage />} />' if template == 'app' else '<Route path="/pricing" element={<PricingPage />} />')}
        {('<Route path="/settings" element={<SettingsPage />} />' if template == 'app' else '')}
        {('<Route path="/contact" element={<ContactPage />} />' if template == 'landing' else '')}
        {('<Route path="/docs" element={<DocsPage />} />' if template == 'docs' else '')}
        {('<Route path="/dashboard" element={<DashboardPage />} />' if template == 'dashboard' else '')}
        <Route path="*" element={{<NotFoundPage />}} />
      </Routes>
    </AppShell>
  );
}}
''',
        "src/components/AppShell.tsx": '''import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import ThemeToggle from "./ui/ThemeToggle";

export default function AppShell(props: {
  title: string;
  description: string;
  navItems: Array<[string, string]>;
  children: ReactNode;
}) {
  const { title, description, navItems, children } = props;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brandTitle">{title}</div>
          <div className="brandSub">{description}</div>
        </div>
        <nav className="nav">
          {navItems.map(([href, label]) => (
            <NavLink key={href} to={href} className={({ isActive }) => "navLink" + (isActive ? " active" : "")}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="topbarActions">
          <ThemeToggle />
        </div>
      </header>

      <main className="container">{children}</main>

      <footer className="footer">
        <div className="footerInner">
          <span className="muted">Seeded template · React + Vite + TS</span>
          <a className="footerLink" href="https://vite.dev" target="_blank" rel="noreferrer">
            Vite
          </a>
        </div>
      </footer>
    </div>
  );
}
''',
        "src/components/ui/Button.tsx": '''import type { ReactNode } from "react";

export default function Button(props: {
  variant?: "primary" | "ghost";
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
}) {
  const { variant = "primary", children, onClick, type = "button" } = props;
  return (
    <button type={type} className={"btn " + (variant === "ghost" ? "btnGhost" : "btnPrimary")} onClick={onClick}>
      {children}
    </button>
  );
}
''',
        "src/components/ui/Card.tsx": '''import type { ReactNode } from "react";

export default function Card(props: {
  title: string;
  eyebrow?: string;
  children: ReactNode;
}) {
  const { title, eyebrow, children } = props;
  return (
    <section className="card">
      {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
      <h2 className="cardTitle">{title}</h2>
      <div className="cardBody">{children}</div>
    </section>
  );
}
''',
        "src/components/ui/ThemeToggle.tsx": '''import { useEffect, useMemo, useState } from "react";

function applyTheme(theme: "light" | "dark") {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("theme", theme);
  } catch {
    // ignore
  }
}

export default function ThemeToggle() {
  const initial = useMemo(() => {
    try {
      const saved = localStorage.getItem("theme");
      if (saved === "light" || saved === "dark") return saved;
    } catch {
      // ignore
    }
    return "dark";
  }, []);

  const [theme, setTheme] = useState<"light" | "dark">(initial as any);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  return (
    <button
      className="btn btnGhost"
      onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
      aria-label="Toggle theme"
      title="Toggle theme"
    >
      {theme === "dark" ? "Dark" : "Light"}
    </button>
  );
}
''',
        "src/pages/Home.tsx": f'''import Button from "../components/ui/Button";
import Card from "../components/ui/Card";

const highlights = {home_highlights_json} as string[];
const landingSections = {landing_sections_json} as string[];

export default function HomePage() {{
  return (
    <div className="stack">
      <section className="hero">
        <div className="heroInner">
          <div className="pill">Template: {template}</div>
          <h1 className="heroTitle">{title}</h1>
          <p className="heroLead">{description}</p>
          <div className="row">
            <Button>{primary_cta}</Button>
            <Button variant="ghost">{secondary_cta}</Button>
          </div>
        </div>
      </section>

      <div className="grid">
        {{highlights.map((h) => (
          <Card key={{h}} title={{h}} eyebrow="Ready">
            <p className="muted">Use the agent prompt to tailor content, sections, pages, and interactions.</p>
          </Card>
        ))}}
      </div>

      {{landingSections.length > 0 ? (
        <div className="grid">
          {{landingSections.map((section) => (
            <Card key={{section}} title={{section}} eyebrow="Requested section">
              <p className="muted">This scaffold keeps room for the sections the brief explicitly asked for, instead of collapsing everything into a generic hero only.</p>
            </Card>
          ))}}
        </div>
      ) : null}}
    </div>
  );
}}
''',
        "src/pages/Features.tsx": f'''import Card from "../components/ui/Card";

const items = {features_items_json} as Array<{{ title: string; body: string }}>;

export default function FeaturesPage() {{
  return (
    <div className="stack">
      <h1 className="pageTitle">{features_title}</h1>
      <div className="grid">
        {{items.map((it) => (
          <Card key={{it.title}} title={{it.title}}>
            <p className="muted">{{it.body}}</p>
          </Card>
        ))}}
      </div>
    </div>
  );
}}
''',
        "src/pages/Pricing.tsx": f'''import Card from "../components/ui/Card";
import Button from "../components/ui/Button";

const tiers = {pricing_items_json} as Array<{{ name: string; price: string; desc: string; perks: string[] }}>;

export default function PricingPage() {{
  return (
    <div className="stack">
      <h1 className="pageTitle">{pricing_title}</h1>
      <div className="grid">
        {{tiers.map((t) => (
          <Card key={{t.name}} title={{t.name}} eyebrow={{t.price}}>
            <p className="muted">{{t.desc}}</p>
            <ul className="list">
              {{t.perks.map((p) => (
                <li key={{p}}>{{p}}</li>
              ))}}
            </ul>
            <div style={{{{ paddingTop: 12 }}}}>
              <Button>{{t.name}}</Button>
            </div>
          </Card>
        ))}}
      </div>
    </div>
  );
}}
''',
        "src/pages/Docs.tsx": '''import Card from "../components/ui/Card";

const doc = `# Quick docs\n\nThis is a starter docs page.\n\n- Replace this with real content\n- Or render markdown if you want\n`;

export default function DocsPage() {
  return (
    <div className="stack">
      <h1 className="pageTitle">Docs</h1>
      <Card title="Getting started">
        <pre className="pre">{doc}</pre>
      </Card>
    </div>
  );
}
''',
        "src/pages/Dashboard.tsx": '''import { useMemo, useState } from "react";
import Card from "../components/ui/Card";

type Project = { id: string; name: string; status: "active" | "paused"; updated: string };

export default function DashboardPage() {
  const [query, setQuery] = useState("");
  const data: Project[] = useMemo(
    () => [
      { id: "p1", name: "Website refresh", status: "active", updated: "2h" },
      { id: "p2", name: "Design tokens", status: "active", updated: "1d" },
      { id: "p3", name: "Landing experiments", status: "paused", updated: "4d" },
    ],
    []
  );

  const filtered = data.filter((p) => p.name.toLowerCase().includes(query.toLowerCase()));

  return (
    <div className="stack">
      <h1 className="pageTitle">Dashboard</h1>

      <div className="toolbar">
        <input className="input" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search projects…" aria-label="Search" />
        <div className="muted">{filtered.length} items</div>
      </div>

      <div className="grid">
        {filtered.length === 0 ? (
          <Card title="No results">
            <p className="muted">Try a different query.</p>
          </Card>
        ) : (
          filtered.map((p) => (
            <Card key={p.id} title={p.name} eyebrow={p.status === "active" ? "Active" : "Paused"}>
              <p className="muted">Updated {p.updated} ago</p>
            </Card>
          ))
        )}
      </div>
    </div>
  );
}
''',
        "src/pages/Workspace.tsx": '''import Card from "../components/ui/Card";

const lanes = [
  { title: "Active work", body: "Surface the main workflows, queues, or entities the product revolves around." },
  { title: "Needs review", body: "Keep room for alerts, approvals, or blocked states so the scaffold feels operational." },
  { title: "Recent changes", body: "Show updates, activity, or collaboration context instead of a static brochure." },
  { title: "Next actions", body: "Guide the user toward the real jobs the product needs to support." },
];

export default function WorkspacePage() {
  return (
    <div className="stack">
      <h1 className="pageTitle">Workspace</h1>
      <div className="grid">
        {lanes.map((lane) => (
          <Card key={lane.title} title={lane.title}>
            <p className="muted">{lane.body}</p>
          </Card>
        ))}
      </div>
    </div>
  );
}
''',
        "src/pages/Integrations.tsx": '''import Card from "../components/ui/Card";

const integrations = [
  { title: "Auth + accounts", body: "Keep the scaffold ready for identity, members, roles, and session-aware flows." },
  { title: "Data + storage", body: "Prepare for Supabase, APIs, uploads, and retrieval without pretending the backend is already done." },
  { title: "Automation + tools", body: "Reserve space for agent actions, MCP, or background operations when the product brief needs them." },
  { title: "Notifications", body: "Model delivery points like inbox, alerts, digests, or operational feedback loops." },
];

export default function IntegrationsPage() {
  return (
    <div className="stack">
      <h1 className="pageTitle">Integrations</h1>
      <div className="grid">
        {integrations.map((item) => (
          <Card key={item.title} title={item.title}>
            <p className="muted">{item.body}</p>
          </Card>
        ))}
      </div>
    </div>
  );
}
''',
        "src/pages/AppSettings.tsx": '''import Card from "../components/ui/Card";

const settingGroups = [
  { title: "Workspace preferences", body: "Theme, density, default views, and operator-level defaults." },
  { title: "Access control", body: "Roles, collaborators, and guarded actions that real apps usually need." },
  { title: "Automation rules", body: "Hooks for reminders, sync jobs, or review loops instead of purely static content." },
  { title: "Quality controls", body: "Validation, audit trails, and system health surfaces for trustworthy product behavior." },
];

export default function SettingsPage() {
  return (
    <div className="stack">
      <h1 className="pageTitle">Settings</h1>
      <div className="grid">
        {settingGroups.map((group) => (
          <Card key={group.title} title={group.title}>
            <p className="muted">{group.body}</p>
          </Card>
        ))}
      </div>
    </div>
  );
}
''',
        "src/pages/Contact.tsx": '''import Card from "../components/ui/Card";

export default function ContactPage() {
  return (
    <div className="stack">
      <h1 className="pageTitle">Contact</h1>
      <Card title="Let people reach out">
        <p className="muted">Use this page for a contact form, booking flow, support CTA, or partnership inquiry instead of leaving marketing briefs half-finished.</p>
      </Card>
    </div>
  );
}
''',
        "src/pages/NotFound.tsx": '''import { Link } from "react-router-dom";

export default function NotFoundPage() {
  return (
    <div className="stack">
      <h1 className="pageTitle">404</h1>
      <p className="muted">Page not found.</p>
      <p>
        <Link to="/" className="link">Go back home</Link>
      </p>
    </div>
  );
}
''',
        "src/app.css": ''':root {
  --bg: #0b1220;
  --panel: rgba(255, 255, 255, 0.06);
  --border: rgba(255, 255, 255, 0.10);
  --text: rgba(255, 255, 255, 0.92);
  --muted: rgba(255, 255, 255, 0.70);
  --brandA: #8b5cf6;
  --brandB: #06b6d4;
  --shadow: 0 18px 70px rgba(0, 0, 0, 0.35);
  --radius: 18px;

  color-scheme: dark;
  font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, "Helvetica Neue", Arial;
  background: var(--bg);
  color: var(--text);
}

:root[data-theme="light"] {
  --bg: #f8fafc;
  --panel: rgba(15, 23, 42, 0.04);
  --border: rgba(15, 23, 42, 0.12);
  --text: rgba(15, 23, 42, 0.92);
  --muted: rgba(15, 23, 42, 0.70);
  --shadow: 0 16px 55px rgba(2, 6, 23, 0.12);
  color-scheme: light;
}

* { box-sizing: border-box; }

html, body, #root {
  height: 100%;
  margin: 0;
}

body {
  background:
    radial-gradient(circle at top left, color-mix(in srgb, var(--brandA) 35%, transparent), transparent 40%),
    radial-gradient(circle at top right, color-mix(in srgb, var(--brandB) 28%, transparent), transparent 38%),
    var(--bg);
}

a { color: inherit; }

.app { min-height: 100%; display: flex; flex-direction: column; }

.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  backdrop-filter: blur(14px);
  background: color-mix(in srgb, var(--bg) 80%, transparent);
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 16px;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
}

.brandTitle { font-weight: 900; letter-spacing: -0.02em; }
.brandSub { font-size: 12px; color: var(--muted); max-width: 520px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.nav { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; }
.navLink {
  font-size: 13px;
  padding: 8px 10px;
  border-radius: 999px;
  border: 1px solid transparent;
  text-decoration: none;
  color: var(--muted);
}
.navLink.active { color: var(--text); border-color: var(--border); background: var(--panel); }

.container { width: min(1120px, calc(100% - 28px)); margin: 0 auto; padding: 22px 0 52px; flex: 1; }

.footer { border-top: 1px solid var(--border); padding: 18px 0; }
.footerInner { width: min(1120px, calc(100% - 28px)); margin: 0 auto; display: flex; gap: 12px; justify-content: space-between; align-items: center; }
.footerLink { opacity: 0.8; text-decoration: none; }
.footerLink:hover { opacity: 1; text-decoration: underline; }

.btn {
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 10px 14px;
  background: var(--panel);
  color: var(--text);
  font-weight: 800;
  cursor: pointer;
}
.btn:focus-visible { outline: 3px solid color-mix(in srgb, var(--brandB) 65%, transparent); outline-offset: 2px; }
.btnPrimary { border: none; background: linear-gradient(135deg, var(--brandA), var(--brandB)); }
.btnGhost { background: transparent; }

.stack { display: grid; gap: 16px; }
.row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }

.hero { border: 1px solid var(--border); background: var(--panel); border-radius: calc(var(--radius) + 8px); box-shadow: var(--shadow); overflow: hidden; }
.heroInner { padding: 26px; }
.pill { display: inline-flex; padding: 6px 10px; border-radius: 999px; background: color-mix(in srgb, var(--brandA) 18%, transparent); border: 1px solid color-mix(in srgb, var(--brandA) 22%, transparent); font-size: 12px; font-weight: 800; margin-bottom: 10px; }
.heroTitle { margin: 0; font-size: clamp(2.1rem, 5vw, 3.4rem); letter-spacing: -0.03em; }
.heroLead { margin: 10px 0 0; color: var(--muted); line-height: 1.7; max-width: 62ch; }

.pageTitle { margin: 0; font-size: 1.8rem; letter-spacing: -0.02em; }

.card { border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); padding: 18px; }
.eyebrow { display: inline-flex; font-size: 12px; font-weight: 900; opacity: 0.85; }
.cardTitle { margin: 10px 0 8px; font-size: 1.05rem; }
.cardBody { color: var(--muted); line-height: 1.7; }

.toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; justify-content: space-between; }
.input {
  flex: 1;
  min-width: 240px;
  max-width: 520px;
  border-radius: 12px;
  border: 1px solid var(--border);
  padding: 10px 12px;
  background: color-mix(in srgb, var(--bg) 92%, transparent);
  color: var(--text);
}
.input:focus-visible { outline: 3px solid color-mix(in srgb, var(--brandA) 60%, transparent); outline-offset: 2px; }

.list { margin: 10px 0 0; padding-left: 18px; color: var(--muted); display: grid; gap: 6px; }
.link { color: var(--text); }
.link:hover { text-decoration: underline; }
.muted { color: var(--muted); }
.pre { white-space: pre-wrap; margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 13px; }
''',
    }

    # Remove pages that don't apply to the chosen template
    if template != "docs":
        files.pop("src/pages/Docs.tsx", None)
    if template != "dashboard":
        files.pop("src/pages/Dashboard.tsx", None)
    if template != "landing":
        files.pop("src/pages/Contact.tsx", None)
    if template != "app":
        files.pop("src/pages/Workspace.tsx", None)
        files.pop("src/pages/Integrations.tsx", None)
        files.pop("src/pages/AppSettings.tsx", None)
    else:
        files.pop("src/pages/Features.tsx", None)
        files.pop("src/pages/Pricing.tsx", None)
        files.pop("src/pages/Contact.tsx", None)

    return {_join(project_root, rel): content for rel, content in files.items()}


def merge_hybrid_seed(
    *,
    project_root: str,
    project_name: str,
    instruction: str,
    changes: list[dict[str, Any]],
    should_seed: bool,
) -> list[dict[str, str]]:
    out: dict[str, str] = {}
    for item in changes:
        rel = str(item.get("path") or "").strip()
        content = item.get("new_content")
        if rel and isinstance(content, str):
            out[rel] = content

    if should_seed:
        seed = build_hybrid_seed(project_root, project_name, instruction)
        for rel, content in seed.items():
            out.setdefault(rel, content)

        package_path = _join(project_root, "package.json")
        package_content = out.get(package_path)
        if package_content:
            try:
                data = json.loads(package_content)
                scripts = data.get("scripts") or {}
                if not isinstance(scripts, dict) or "dev" not in scripts:
                    raise ValueError("missing dev script")
            except Exception:
                out[package_path] = seed[package_path]

    return [{"path": rel, "new_content": content} for rel, content in out.items()]
