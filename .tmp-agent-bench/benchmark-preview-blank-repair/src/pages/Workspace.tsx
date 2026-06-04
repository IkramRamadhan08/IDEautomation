import Card from "../components/ui/Card";

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
