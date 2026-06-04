export default function NotFoundPage(props: { onNavigate: (path: string) => void }) {
  return (
    <div className="stack">
      <h1 className="pageTitle">404</h1>
      <p className="muted">Page not found.</p>
      <button className="btn btnGhost" type="button" onClick={() => props.onNavigate("/")}>Go back home</button>
    </div>
  );
}
