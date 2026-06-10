import { Toaster } from "sonner";
import { ArrowRight, Bot, Boxes, Code2, Database, Globe2, HelpCircle, Layers3, Moon, PlayCircle, Rocket, ShieldCheck, Sparkles, Sun, Terminal, Workflow } from "lucide-react";
import type { AppTheme } from "../appDefaults";

type LandingGateProps = {
  appTheme: AppTheme;
  projectSetupError: string;
  onOpenAppora: () => void;
  onToggleTheme: () => void;
};

export function LandingGate({ appTheme, projectSetupError, onOpenAppora, onToggleTheme }: LandingGateProps) {
    const heroNodes = [
      { label: "Prompt", detail: "Describe the app", icon: Sparkles },
      { label: "Agent", detail: "One coder handles build and repair", icon: Bot },
      { label: "Workspace", detail: "Editor-first or preview-first", icon: Code2 },
      { label: "Preview", detail: "Inspect in browser", icon: Globe2 },
      { label: "Memory", detail: "Project context stays", icon: Layers3 },
      { label: "Deploy", detail: "Vercel-ready output", icon: Rocket },
    ];
    const templateCards = [
      ["SaaS dashboard", "Auth, settings, billing-ready workspace"],
      ["AI landing page", "Conversion sections with provider routing"],
      ["Admin portal", "Tables, forms, permissions, activity"],
      ["Portfolio app", "Content pages, templates, deployment"],
    ];
    const providers = ["9Router", "free-forever", "Kiro", "OpenCode", "Vertex", "Claude Code", "Codex", "Copilot", "Cursor", "Supabase", "Vercel"];

    return (
    <div
      className="authLanding apporaLanding"
      id="top"
    >
      <Toaster position="top-right" richColors />
      <header className="apporaNav">
        <a className="splineBrandButton apporaBrand" href="#top" aria-label="Appora home">
          <span className="authBrandMark">A</span>
          <span>Appora</span>
        </a>
        <nav className="apporaNavLinks" aria-label="Landing navigation">
          <a href="#templates">Templates</a>
          <a href="#tutorial">Tutorial</a>
          <a href="#docs">Docs</a>
          <a href="#faq">FAQ</a>
        </nav>
        <div className="apporaNavActions">
          <button className="apporaThemeToggle" type="button" onClick={onToggleTheme} title={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`} aria-label={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`}>
            {appTheme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          </button>
          <button className="apporaNavCta" onClick={onOpenAppora}>
            Start Building
            <ArrowRight size={16} />
          </button>
        </div>
      </header>

      <main className="apporaMain">
        <section className="apporaHero" aria-label="Appora">
          <div className="apporaHeroCopy">
            <h1>Appora Studio</h1>
            <p>
              A focused coding workspace where one autonomous agent plans, edits, runs,
              repairs, and keeps the preview close to the code.
            </p>
            <div className="apporaHeroActions">
              <button className="apporaPrimaryButton" onClick={onOpenAppora}>
                Continue With Google
                <ArrowRight size={17} />
              </button>
              <a className="apporaSecondaryButton" href="#tutorial">
                Watch The Flow
                <PlayCircle size={17} />
              </a>
            </div>
            {projectSetupError ? (
              <div className="projectSetupInlineError landingAuthError" role="alert">
                <span>{projectSetupError}</span>
              </div>
            ) : null}
            <div className="apporaSignalRow" aria-label="Platform signals">
              <span>9Router routes</span>
              <span>Repair loop</span>
              <span>Saved projects</span>
            </div>
          </div>

          <div className="apporaHeroVisual" aria-label="Agent workflow preview">
            <div className="apporaOrbitGlow" />
            <div className="apporaCommandPanel">
              <div className="apporaPanelChrome">
                <span />
                <span />
                <span />
                <strong>appora://workspace</strong>
              </div>
              <div className="apporaPromptLine">Build a scheduling dashboard, wire the states, run build, and show the preview.</div>
              <div className="apporaAgentRows">
                <div><Bot size={17} /><strong>Appora Agent</strong><span>tools, memory, terminal, preview, validation, repair</span></div>
                <div><Terminal size={17} /><strong>Professional flow</strong><span>editor-first workspace, preview desk, project handoff</span></div>
              </div>
            </div>
            <div className="apporaNodeGrid">
              {heroNodes.map(({ label, detail, icon: Icon }) => (
                <div className="apporaNode" key={label}>
                  <Icon size={18} />
                  <strong>{label}</strong>
                  <span>{detail}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="apporaBand apporaMarqueeBand" aria-label="Supported providers">
          <div className="apporaMarquee">
            {[...providers, ...providers].map((provider, index) => (
              <span key={`${provider}-${index}`}>{provider}</span>
            ))}
          </div>
        </section>

        <section className="apporaSection apporaSplit" id="tutorial">
          <div className="apporaSectionCopy">
            <span className="apporaSectionIndex">01</span>
            <h2>From Chat To Working Project Without Switching Tools.</h2>
            <p>Start with normal language. The agent plans the project, edits files, runs terminal actions, previews the result, and keeps every action visible.</p>
          </div>
          <div className="apporaWorkflowStack">
            {[
              ["Ask", "Describe the product, audience, pages, and integrations."],
              ["Build", "Agent writes code with patch-based edits and command history."],
              ["Inspect", "Preview and browser checks catch broken UI before deploy."],
              ["Ship", "Hosted project state stays connected to Supabase and Vercel."],
            ].map(([title, copy], index) => (
              <article key={title}>
                <em>{String(index + 1).padStart(2, "0")}</em>
                <strong>{title}</strong>
                <span>{copy}</span>
              </article>
            ))}
          </div>
        </section>

        <section className="apporaSection" id="templates">
          <div className="apporaSectionHeader wide">
            <span className="apporaSectionIndex">02</span>
            <h2>Production-Shaped Starters For People Who Do Not Want A Blank Repo.</h2>
          </div>
          <div className="apporaTemplateGrid">
            {templateCards.map(([title, copy]) => (
              <article key={title}>
                <Boxes size={19} />
                <strong>{title}</strong>
                <span>{copy}</span>
              </article>
            ))}
          </div>
        </section>

        <section className="apporaSection apporaSplit" id="docs">
          <div className="apporaSectionCopy">
            <span className="apporaSectionIndex">03</span>
            <h2>One 9Router Key, Every Model Route Behind It.</h2>
            <p>Appora sends every request to the 9Router OpenAI-compatible gateway. Free models, subscription aliases, cheap API routes, and combo fallback stay inside 9Router.</p>
          </div>
          <div className="apporaDocsPanel">
            <div><ShieldCheck size={18} /><strong>Hosted settings</strong><span>The 9Router endpoint and API key stay in user-scoped provider secrets.</span></div>
            <div><Database size={18} /><strong>Supabase memory</strong><span>Project files, settings, job ledger, and memory chunks persist.</span></div>
            <div><Workflow size={18} /><strong>MCP/tool loop</strong><span>Actions are separated from assistant responses for clearer UX.</span></div>
          </div>
        </section>

        <section className="apporaSection apporaFaqSection" id="faq">
          <div className="apporaSectionHeader">
            <span className="apporaSectionIndex">04</span>
            <h2>Built For Hosted Web, Not A Local-Only Toy.</h2>
          </div>
          <div className="apporaFaqGrid">
            {[
              ["Can It Deploy?", "The app is shaped for Vercel serverless with Supabase as the durable backend."],
              ["Can Beginners Use It?", "The primary flow is prompt, preview, edit with agent help, then ship from a saved project."],
              ["Can I Use Free Models?", "Yes. Choose 9Router combos like free-forever or direct aliases like kr/claude-sonnet-4.5."],
              ["Where Do Actions Show?", "Agent actions belong in Live Interaction. Model conversation streams through the orb/chat surface."],
            ].map(([question, answer]) => (
              <article key={question}>
                <HelpCircle size={18} />
                <strong>{question}</strong>
                <span>{answer}</span>
              </article>
            ))}
          </div>
        </section>

        <section className="apporaFinalCta">
          <h2>Start With An Idea. Leave With A Project You Can Keep Improving.</h2>
          <button className="apporaPrimaryButton" onClick={onOpenAppora}>
            Open Appora
            <ArrowRight size={17} />
          </button>
        </section>
      </main>

      <footer className="apporaFooter">
        <div className="apporaFooterShell">
          <div className="apporaFooterBrand">
            <span className="authBrandMark">A</span>
            <div>
              <strong>Appora</strong>
              <span>Agentic web builder for hosted projects.</span>
            </div>
          </div>
          <div className="apporaFooterColumns">
            <nav aria-label="Footer product navigation">
              <strong>Product</strong>
              <a href="#templates">Templates</a>
              <a href="#tutorial">Tutorial</a>
            </nav>
            <nav aria-label="Footer platform navigation">
              <strong>Platform</strong>
              <a href="#docs">Docs</a>
              <a href="#faq">FAQ</a>
            </nav>
            <div className="apporaFooterSignal">
              <strong>Ready for</strong>
              <span>Supabase memory</span>
              <span>Vercel deploys</span>
            </div>
          </div>
          <div className="apporaFooterBottom">
            <span>Bring your own keys. Keep your projects saved.</span>
            <button className="apporaFooterCta" type="button" onClick={onOpenAppora}>
              Open Appora
              <ArrowRight size={15} />
            </button>
          </div>
        </div>
      </footer>
    </div>
  );
  };
