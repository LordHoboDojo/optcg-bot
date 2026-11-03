import { useEffect, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useSession } from "../session";

const NAV = [
  ["/play", "Play"],
  ["/analysis", "Analysis"],
  ["/bots", "Your bots"],
  ["/docs/bot-api", "Bot API"],
] as const;

export function initials(name: string | null | undefined) {
  return (name || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
}

export default function Layout() {
  const { me } = useSession();
  const { pathname } = useLocation();
  useEffect(() => window.scrollTo(0, 0), [pathname]);
  const user = me?.user;
  return (
    <div className="shell">
      <header className="topbar">
        <div className="wrap">
          <Link to="/" className="brand"><img src="/favicon.svg" alt="" /><span className="label">OPTCG Bot Arena</span></Link>
          <nav className="nav" aria-label="Main">
            {NAV.map(([to, label]) => <NavLink key={to} to={to}>{label}</NavLink>)}
          </nav>
          <div className="who">
            {user ? (
              <Link to="/account" className="row" style={{ gap: 8, color: "var(--ink)" }} title="Account">
                <span className="name small">{user.name || user.email}</span>
                <span className="avatar" aria-hidden>{initials(user.name || user.email)}</span>
              </Link>
            ) : me?.signin ? (
              <a className="btn small" href="/login">Sign in</a>
            ) : null}
          </div>
        </div>
      </header>
      <main><div className="wrap"><Outlet /></div></main>
      <footer>
        <div className="wrap">
          <span>OPTCG Bot Arena — a fan-made rules engine and bots for the ONE PIECE Card Game.</span>
          <span className="row" style={{ gap: 16 }}>
            <Link to="/docs/bot-api">Bot API</Link>
            <Link to="/analysis">Analysis</Link>
            <Link to="/account">Account</Link>
          </span>
        </div>
      </footer>
    </div>
  );
}

export function useTitle(title: string) {
  useEffect(() => { document.title = title ? `${title} · OPTCG Bot Arena` : "OPTCG Bot Arena"; }, [title]);
}

export function PageHead({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  useTitle(title);
  return (
    <div className="page-head">
      <div><h1>{title}</h1>{children && <p>{children}</p>}</div>
      {actions && <div className="row">{actions}</div>}
    </div>
  );
}

export function Loading({ height = 200 }: { height?: number }) {
  return <div className="skeleton" style={{ height }} aria-label="Loading" />;
}

export function SignInPrompt({ what }: { what: string }) {
  const { me } = useSession();
  return (
    <div className="card empty">
      <h3>Sign in to {what}</h3>
      <p className="muted" style={{ marginBottom: 18 }}>Playing in the browser never needs an account.</p>
      {me?.signin ? <a className="btn primary" href="/login">{me.signin === "google" ? "Sign in with Google" : "Sign in (dev)"}</a>
        : <p className="tiny">Sign-in isn't set up on this server.</p>}
    </div>
  );
}
