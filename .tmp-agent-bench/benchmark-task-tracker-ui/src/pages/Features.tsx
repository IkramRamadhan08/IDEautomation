import Card from "../components/ui/Card";

const items = [{"title": "Design system", "body": "CSS variables + consistent spacing, radii, shadows."}, {"title": "Navigation", "body": "Client-side pages, active links, and layout shell without extra router dependencies."}, {"title": "States", "body": "Empty/loading/error patterns you can extend."}, {"title": "Polish", "body": "Focus states, contrast, responsive grid."}] as Array<{ title: string; body: string }>;

export default function FeaturesPage() {
  return (
    <div className="stack">
      <h1 className="pageTitle">Metrics</h1>
      <div className="grid">
        {items.map((it) => (
          <Card key={it.title} title={it.title}>
            <p className="muted">{it.body}</p>
          </Card>
        ))}
      </div>
    </div>
  );
}
