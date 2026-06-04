import { useMemo, useState } from "react";
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
