import Card from "../components/ui/Card";

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
