from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api.hybrid import build_hybrid_seed


@dataclass(frozen=True)
class ProjectTemplate:
    id: str
    name: str
    category: str
    description: str
    best_for: str
    prompt: str
    tags: tuple[str, ...]


TEMPLATES: tuple[ProjectTemplate, ...] = (
    ProjectTemplate(
        id="saas-dashboard",
        name="SaaS Dashboard",
        category="Dashboard",
        description="Auth-ready product workspace with sidebar-style flows, metrics, tables, and operational states.",
        best_for="Internal tools, SaaS MVPs, analytics products, and customer portals.",
        prompt=(
            "Build a SaaS dashboard app shell with dashboard, metrics, project/activity table, settings-ready structure, "
            "loading state, empty state, error/retry state, responsive layout, and clean minimalist elegant styling."
        ),
        tags=("dashboard", "saas", "auth-ready", "metrics"),
    ),
    ProjectTemplate(
        id="landing-pricing",
        name="Landing + Pricing",
        category="Marketing",
        description="Conversion-focused landing site with product story, feature grid, pricing, FAQ, and contact CTA.",
        best_for="Product launches, agency offers, waitlists, SaaS landing pages, and portfolio offers.",
        prompt=(
            "Build a landing page with hero, features, pricing, FAQ, contact CTA, SEO-ready copy, responsive sections, "
            "accessible navigation, polished empty/loading/error copy, and minimalist elegant visual direction."
        ),
        tags=("landing", "pricing", "marketing", "seo"),
    ),
    ProjectTemplate(
        id="admin-crud",
        name="Admin CRUD",
        category="Operations",
        description="Operational admin surface with records, filters, create/edit states, validation copy, and table-first UX.",
        best_for="Back-office panels, CRM-lite workflows, moderation tools, and inventory/order management.",
        prompt=(
            "Build an admin dashboard CRUD app with searchable records, filters, table-first layout, create/edit/detail states, "
            "empty/loading/error states, validation copy, and restrained professional UI for repeated daily work."
        ),
        tags=("admin", "crud", "table", "operations"),
    ),
    ProjectTemplate(
        id="ai-tool-app",
        name="AI Tool App",
        category="AI Product",
        description="Prompt-driven app shell with workspace, generated result area, history, integrations, and settings surface.",
        best_for="AI utilities, prompt tools, content generators, assistants, and workflow automation MVPs.",
        prompt=(
            "Build an AI tool app workspace with prompt panel, result area, history, integrations page, settings page, "
            "usage state, loading/error/empty states, and focused IDE-like minimalist layout."
        ),
        tags=("ai", "workspace", "history", "settings"),
    ),
)


def list_project_templates() -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "name": item.name,
            "category": item.category,
            "description": item.description,
            "best_for": item.best_for,
            "tags": list(item.tags),
        }
        for item in TEMPLATES
    ]


def get_project_template(template_id: str | None) -> ProjectTemplate | None:
    requested = (template_id or "").strip()
    if not requested or requested == "blank":
        return None
    for item in TEMPLATES:
        if item.id == requested:
            return item
    return None


def render_project_template(*, template_id: str | None, project_root: str, project_name: str) -> dict[str, str]:
    template = get_project_template(template_id)
    if not template:
        return {}

    seeded = build_hybrid_seed(project_root=project_root, project_name=project_name, instruction=template.prompt)
    prefix = f"{project_root.strip('/')}/"
    files: dict[str, str] = {}
    for path, content in seeded.items():
        rel = path[len(prefix):] if path.startswith(prefix) else path
        files[rel] = content

    files["README.md"] = _template_readme(template=template, project_name=project_name)
    files[".voiceide/memory/project.md"] = _template_memory(template=template, project_name=project_name)
    _apply_template_polish(files, template=template, project_name=project_name)
    return files


def _apply_template_polish(files: dict[str, str], *, template: ProjectTemplate, project_name: str) -> None:
    if template.id == "saas-dashboard":
        files["src/pages/Dashboard.tsx"] = _saas_dashboard_page(project_name)
    elif template.id == "admin-crud":
        files["src/pages/Dashboard.tsx"] = _admin_crud_page(project_name)
    elif template.id == "ai-tool-app":
        files["src/pages/Dashboard.tsx"] = _ai_tool_page(project_name)
    elif template.id == "landing-pricing":
        files["src/pages/Home.tsx"] = _landing_home_page(project_name)

    files["src/app.css"] = files.get("src/app.css", "") + _template_extra_css()


def _template_readme(*, template: ProjectTemplate, project_name: str) -> str:
    return f"""# {project_name}

Started from the **{template.name}** Voice IDE template.

## Template Intent

{template.description}

Best for: {template.best_for}

## Development

```bash
npm install
npm run dev
npm run build
```

## Agent Notes

This project is intended to stay production-oriented: keep responsive layout, accessible labels, loading/empty/error states, and clean copy in place as the app evolves.
"""


def _template_memory(*, template: ProjectTemplate, project_name: str) -> str:
    tags = ", ".join(template.tags)
    return f"""# Project Memory: {project_name}

- Template: {template.name}
- Category: {template.category}
- Tags: {tags}
- Product direction: {template.description}
- Best for: {template.best_for}
- Build guidance: keep this starter production-ready, responsive, accessible, and easy for a non-coder to iterate through Clara/Raka.
- UX guidance: preserve loading, empty, error, and success states when adding features.
- Deployment guidance: keep the project Vercel-friendly and avoid local-only assumptions.
"""


def _saas_dashboard_page(project_name: str) -> str:
    return f'''import Card from "../components/ui/Card";

const metrics = [
  {{ label: "Active workspaces", value: "128", delta: "+14%" }},
  {{ label: "Monthly revenue", value: "$42.8k", delta: "+8.2%" }},
  {{ label: "Activation rate", value: "64%", delta: "+5.1%" }},
];

const accounts = [
  {{ name: "Northstar Labs", plan: "Scale", status: "Healthy", owner: "Maya", lastSeen: "12m ago" }},
  {{ name: "Atlas Studio", plan: "Team", status: "Needs onboarding", owner: "Raka", lastSeen: "2h ago" }},
  {{ name: "Orbit Ops", plan: "Starter", status: "Trial risk", owner: "Clara", lastSeen: "1d ago" }},
];

export default function DashboardPage() {{
  return (
    <div className="stack">
      <div className="templateHero">
        <div>
          <div className="pill">SaaS operating console</div>
          <h1 className="pageTitle">{project_name}</h1>
          <p className="heroLead">A production-ready dashboard starter with account health, revenue signals, and operational follow-up states.</p>
        </div>
        <button className="btn btnPrimary">Invite teammate</button>
      </div>

      <div className="templateMetrics">
        {{metrics.map((item) => (
          <Card key={{item.label}} title={{item.value}} eyebrow={{item.label}}>
            <span className="templateDelta">{{item.delta}} this month</span>
          </Card>
        ))}}
      </div>

      <section className="templatePanel">
        <div className="templatePanelHeader">
          <div>
            <h2>Customer health</h2>
            <p>Prioritized accounts for retention, onboarding, and expansion.</p>
          </div>
          <span className="templateBadge">3 segments</span>
        </div>
        <div className="templateTable">
          {{accounts.map((item) => (
            <div className="templateTableRow" key={{item.name}}>
              <strong>{{item.name}}</strong>
              <span>{{item.plan}}</span>
              <span>{{item.status}}</span>
              <span>{{item.owner}}</span>
              <em>{{item.lastSeen}}</em>
            </div>
          ))}}
        </div>
      </section>
    </div>
  );
}}
'''


def _admin_crud_page(project_name: str) -> str:
    return f'''import {{ useMemo, useState }} from "react";
import Card from "../components/ui/Card";

type RecordStatus = "Active" | "Pending" | "Blocked";

const records: Array<{{ id: string; name: string; owner: string; status: RecordStatus; value: string }}> = [
  {{ id: "ORD-1042", name: "Enterprise onboarding", owner: "Nadia", status: "Active", value: "$12,400" }},
  {{ id: "ORD-1043", name: "Vendor approval", owner: "Dimas", status: "Pending", value: "$3,850" }},
  {{ id: "ORD-1044", name: "Compliance review", owner: "Sari", status: "Blocked", value: "$8,900" }},
];

export default function DashboardPage() {{
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => records.filter((item) => `${{item.id}} ${{item.name}} ${{item.owner}}`.toLowerCase().includes(query.toLowerCase())), [query]);

  return (
    <div className="stack">
      <div className="templateHero">
        <div>
          <div className="pill">Admin CRUD starter</div>
          <h1 className="pageTitle">{project_name}</h1>
          <p className="heroLead">A table-first operations surface with search, status visibility, and create/edit flow placeholders.</p>
        </div>
        <button className="btn btnPrimary">Create record</button>
      </div>

      <div className="toolbar">
        <input className="input" value={{query}} onChange={{(event) => setQuery(event.target.value)}} placeholder="Search ID, name, or owner..." aria-label="Search records" />
        <span className="templateBadge">{{filtered.length}} records</span>
      </div>

      <section className="templatePanel">
        <div className="templateTable">
          {{filtered.length === 0 ? (
            <Card title="No records found" eyebrow="Empty state">
              <p className="muted">Try another query or create the first record from the primary action.</p>
            </Card>
          ) : filtered.map((item) => (
            <div className="templateTableRow" key={{item.id}}>
              <strong>{{item.id}}</strong>
              <span>{{item.name}}</span>
              <span>{{item.owner}}</span>
              <span className={{"statusPill " + item.status.toLowerCase()}}>{{item.status}}</span>
              <em>{{item.value}}</em>
            </div>
          ))}}
        </div>
      </section>
    </div>
  );
}}
'''


def _ai_tool_page(project_name: str) -> str:
    return f'''import {{ useState }} from "react";
import Card from "../components/ui/Card";

const history = ["Landing page copy", "Email sequence", "Research summary"];

export default function DashboardPage() {{
  const [prompt, setPrompt] = useState("Summarize customer feedback into product opportunities");

  return (
    <div className="stack">
      <div className="templateHero">
        <div>
          <div className="pill">AI tool workspace</div>
          <h1 className="pageTitle">{project_name}</h1>
          <p className="heroLead">Prompt input, generated result, usage history, and settings-ready structure for an AI product MVP.</p>
        </div>
        <button className="btn btnPrimary">Run prompt</button>
      </div>

      <div className="templateSplit">
        <Card title="Prompt" eyebrow="Input">
          <textarea className="templateTextarea" value={{prompt}} onChange={{(event) => setPrompt(event.target.value)}} aria-label="Prompt" />
          <p className="muted">Add tone, audience, constraints, and desired output format before sending.</p>
        </Card>
        <Card title="Generated result" eyebrow="Preview">
          <div className="templateResult">
            <strong>3 product opportunities</strong>
            <p>Improve onboarding clarity, add saved presets, and expose export history for repeat workflows.</p>
          </div>
        </Card>
      </div>

      <section className="templatePanel">
        <div className="templatePanelHeader">
          <div>
            <h2>Recent generations</h2>
            <p>Use this area for saved outputs, retry states, and usage limits.</p>
          </div>
          <span className="templateBadge">Usage: 42%</span>
        </div>
        <div className="templateList">
          {{history.map((item) => <span key={{item}}>{{item}}</span>)}}
        </div>
      </section>
    </div>
  );
}}
'''


def _landing_home_page(project_name: str) -> str:
    return f'''import Card from "../components/ui/Card";

const features = ["Launch-ready sections", "Pricing and FAQ", "Accessible responsive layout"];
const plans = ["Starter", "Team", "Scale"];

export default function HomePage() {{
  return (
    <div className="stack">
      <section className="hero">
        <div className="heroInner">
          <div className="pill">Landing + pricing starter</div>
          <h1 className="heroTitle">{project_name}</h1>
          <p className="heroLead">A polished marketing surface for explaining the offer, collecting demand, and giving Clara a strong base to iterate from.</p>
          <div className="row">
            <button className="btn btnPrimary">Start free</button>
            <button className="btn btnGhost">View pricing</button>
          </div>
        </div>
      </section>

      <div className="grid">
        {{features.map((item) => (
          <Card key={{item}} title={{item}} eyebrow="Included">
            <p className="muted">Ready for sharper copy, stronger proof, and conversion-focused polish.</p>
          </Card>
        ))}}
      </div>

      <section className="templatePanel">
        <div className="templatePanelHeader">
          <div>
            <h2>Pricing signal</h2>
            <p>Starter pricing cards are included so the agent can refine packaging instead of starting cold.</p>
          </div>
          <span className="templateBadge">3 plans</span>
        </div>
        <div className="templateMetrics">
          {{plans.map((plan) => (
            <Card key={{plan}} title={{plan}} eyebrow="Plan">
              <p className="muted">Add limits, proof, CTA, and objections for this segment.</p>
            </Card>
          ))}}
        </div>
      </section>
    </div>
  );
}}
'''


def _template_extra_css() -> str:
    return '''

.templateHero {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--panel);
  box-shadow: var(--shadow);
}
.templateMetrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
}
.templateDelta {
  display: inline-flex;
  color: #22c55e;
  font-weight: 800;
}
.templatePanel {
  display: grid;
  gap: 14px;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--panel);
  box-shadow: var(--shadow);
}
.templatePanelHeader {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.templatePanelHeader h2 {
  margin: 0;
  font-size: 1.1rem;
}
.templatePanelHeader p {
  margin: 4px 0 0;
  color: var(--muted);
}
.templateBadge,
.statusPill {
  display: inline-flex;
  align-items: center;
  width: fit-content;
  border-radius: 999px;
  padding: 5px 8px;
  background: color-mix(in srgb, var(--brandA) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--brandA) 24%, transparent);
  color: var(--text);
  font-size: 12px;
  font-weight: 800;
}
.statusPill.blocked { background: color-mix(in srgb, #ef4444 16%, transparent); border-color: color-mix(in srgb, #ef4444 28%, transparent); }
.statusPill.pending { background: color-mix(in srgb, #f59e0b 16%, transparent); border-color: color-mix(in srgb, #f59e0b 28%, transparent); }
.templateTable {
  display: grid;
  gap: 8px;
}
.templateTableRow {
  display: grid;
  grid-template-columns: 1.1fr 1.5fr 1fr 1fr 0.8fr;
  gap: 10px;
  align-items: center;
  padding: 11px 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: color-mix(in srgb, var(--panel) 88%, transparent);
}
.templateTableRow span,
.templateTableRow em {
  color: var(--muted);
  font-style: normal;
}
.templateSplit {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}
.templateTextarea {
  width: 100%;
  min-height: 150px;
  resize: vertical;
  border-radius: 12px;
  border: 1px solid var(--border);
  padding: 12px;
  background: color-mix(in srgb, var(--bg) 92%, transparent);
  color: var(--text);
}
.templateResult {
  display: grid;
  gap: 8px;
  min-height: 150px;
  align-content: start;
  padding: 12px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--brandA) 9%, transparent);
}
.templateList {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.templateList span {
  padding: 8px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--muted);
}
@media (max-width: 760px) {
  .templateHero,
  .templatePanelHeader {
    flex-direction: column;
  }
  .templateSplit,
  .templateTableRow {
    grid-template-columns: 1fr;
  }
}
'''
