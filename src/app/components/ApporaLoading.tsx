const astronautLoaderUrl = new URL("../../../Astronaut Illustration.webm", import.meta.url).href;

export function ApporaLoading({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="apporaLoadingShell" role="status" aria-live="polite">
      <div className="apporaLoadingOrbit" aria-hidden="true">
        <video
          className="apporaLoadingAstronaut"
          src={astronautLoaderUrl}
          autoPlay
          muted
          loop
          playsInline
          preload="auto"
        />
      </div>
      <div className="apporaLoadingCopy">
        <div className="apporaLoadingTitle">{title}</div>
        {subtitle ? <div className="apporaLoadingSubtitle">{subtitle}</div> : null}
      </div>
      <div className="apporaLoadingProgress" aria-hidden="true">
        <span />
      </div>
    </div>
  );
}
