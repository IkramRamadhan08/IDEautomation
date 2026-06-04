import Card from "../components/ui/Card";

const projects = [
  { title: "Studio dashboard", type: "Product design", result: "Clearer operating rhythm for a small team." },
  { title: "Launch system", type: "Frontend build", result: "Reusable marketing sections with fast iteration." },
  { title: "AI workflow", type: "Automation", result: "Prompt-to-result workspace for repeat client tasks." },
];

const skills = ["React", "Product UX", "Design systems", "Supabase", "Vercel", "AI workflows"];

export default function HomePage() {
  return (
    <div className="stack">
      <section className="portfolioHero">
        <div>
          <div className="pill">Portfolio starter</div>
          <h1 className="heroTitle">Preview Blank Repair</h1>
          <p className="heroLead">A polished portfolio base for showing selected work, product thinking, and a clear path for people to contact you.</p>
          <div className="row">
            <button className="btn btnPrimary">View work</button>
            <button className="btn btnGhost">Contact me</button>
          </div>
        </div>
        <div className="portfolioPortrait" aria-label="Portfolio identity card">
          <span>P</span>
          <strong>Available for thoughtful product work</strong>
        </div>
      </section>

      <section className="templatePanel">
        <div className="templatePanelHeader">
          <div>
            <h2>Selected work</h2>
            <p>Replace these cards with real projects, screenshots, metrics, and links.</p>
          </div>
          <span className="templateBadge">3 case studies</span>
        </div>
        <div className="portfolioProjectGrid">
          {projects.map((project) => (
            <Card key={project.title} title={project.title} eyebrow={project.type}>
              <p className="muted">{project.result}</p>
            </Card>
          ))}
        </div>
      </section>

      <div className="templateSplit">
        <Card title="Skills" eyebrow="Toolkit">
          <div className="templateList">
            {skills.map((skill) => <span key={skill}>{skill}</span>)}
          </div>
        </Card>
        <Card title="Process" eyebrow="How I work">
          <p className="muted">Discovery, interface direction, build, preview review, and launch polish. Keep this section honest and specific.</p>
        </Card>
      </div>
    </div>
  );
}
