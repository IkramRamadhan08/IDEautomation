import type { ReactNode } from "react";
import ThemeToggle from "./ui/ThemeToggle";

type NavItem = [string, string] | { path: string; label: string } | { href: string; label: string };

function navItemParts(item: NavItem) {
  if (Array.isArray(item)) {
    return { href: item[0], label: item[1] };
  }
  return { href: "path" in item ? item.path : item.href, label: item.label };
}

export default function AppShell(props: {
  title: string;
  description: string;
  navItems: NavItem[];
  currentPath: string;
  onNavigate: (path: string) => void;
  children: ReactNode;
}) {
  const { title, description, navItems, currentPath, onNavigate, children } = props;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brandTitle">{title}</div>
          <div className="brandSub">{description}</div>
        </div>
        <nav className="nav">
          {navItems.map((item) => {
            const { href, label } = navItemParts(item);
            return (
              <a
                key={href}
                href={href}
                className={"navLink" + (currentPath === href ? " active" : "")}
                onClick={(event) => {
                  event.preventDefault();
                  onNavigate(href);
                }}
              >
                {label}
              </a>
            );
          })}
        </nav>
        <div className="topbarActions">
          <ThemeToggle />
        </div>
      </header>

      <main className="container">{children}</main>

      <footer className="footer">
        <div className="footerInner">
          <span className="muted">{new Date().getFullYear()} {title}</span>
          <button className="footerLink" type="button" onClick={() => onNavigate("/")}>
            Back to overview
          </button>
        </div>
      </footer>
    </div>
  );
}
