import Card from "../components/ui/Card";
import Button from "../components/ui/Button";

const tiers = [{"name": "Starter", "price": "$0", "desc": "For prototyping and demos", "perks": ["Basic pages", "Theme toggle", "Client navigation"]}, {"name": "Pro", "price": "$19", "desc": "For real products", "perks": ["Better UX", "More components", "Polish"]}, {"name": "Team", "price": "$49", "desc": "For teams", "perks": ["Shared workflows", "Design tokens", "Scalable layout"]}] as Array<{ name: string; price: string; desc: string; perks: string[] }>;

export default function PricingPage() {
  return (
    <div className="stack">
      <h1 className="pageTitle">Operations</h1>
      <div className="grid">
        {tiers.map((t) => (
          <Card key={t.name} title={t.name} eyebrow={t.price}>
            <p className="muted">{t.desc}</p>
            <ul className="list">
              {t.perks.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
            <div style={{ paddingTop: 12 }}>
              <Button>{t.name}</Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
