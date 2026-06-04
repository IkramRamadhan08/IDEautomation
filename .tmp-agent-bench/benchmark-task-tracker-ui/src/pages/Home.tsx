import Button from "../components/ui/Button";
import Card from "../components/ui/Card";

const highlights = ["Multi-page routing scaffold", "Reusable UI components", "Light/dark theme with design tokens", "Responsive layout + accessible defaults"] as string[];
const landingSections = [] as string[];

export default function HomePage() {
  return (
    <div className="stack">
      <section className="hero">
        <div className="heroInner">
          <div className="pill">Template: dashboard</div>
          <h1 className="heroTitle">Bikin dashboard task tracker profesional untuk t</h1>
          <p className="heroLead">Bikin dashboard task tracker profesional untuk tim produk. Harus ada daftar task, prioritas, owner, status progress, ringkasan metrik, state kosong yang masuk akal, dan jalankan va</p>
          <div className="row">
            <Button>Get started</Button>
            <Button variant="ghost">See demo</Button>
          </div>
        </div>
      </section>

      <div className="grid">
        {highlights.map((h) => (
          <Card key={h} title={h} eyebrow="Ready">
            <p className="muted">Use the agent prompt to tailor content, sections, pages, and interactions.</p>
          </Card>
        ))}
      </div>

      {landingSections.length > 0 ? (
        <div className="grid">
          {landingSections.map((section) => (
            <Card key={section} title={section} eyebrow="Requested section">
              <p className="muted">This scaffold keeps room for the sections the brief explicitly asked for, instead of collapsing everything into a generic hero only.</p>
            </Card>
          ))}
        </div>
      ) : null}
    </div>
  );
}
