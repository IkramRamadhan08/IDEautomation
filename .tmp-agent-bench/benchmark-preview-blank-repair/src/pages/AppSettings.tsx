import Card from "../components/ui/Card";

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
