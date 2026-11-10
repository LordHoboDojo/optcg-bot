import { Link, useSearchParams } from "react-router-dom";
import { PageHead } from "../components/Layout";
import { useSession } from "../session";

export default function SignIn() {
  const { me } = useSession();
  const [params] = useSearchParams();
  return (
    <div style={{ maxWidth: 460, margin: "24px auto" }}>
      <PageHead title="Sign in" />
      <div className="card pad stack">
        {params.get("failed") && <div className="notice bad">Signing in didn't finish. Try again.</div>}
        {me?.user ? (
          <><p>You're signed in as <b>{me.user.name || me.user.email}</b>.</p><Link className="btn primary" to="/account">Go to your account</Link></>
        ) : me?.signin === "dev" ? (
          // /login sends you here when Google isn't set up; the dev login only needs a name
          <form className="stack" method="get" action="/login/dev">
            <p className="muted">Development sign-in: pick any name. Never enable this on a public server.</p>
            <input name="name" aria-label="Name" placeholder="Name" required minLength={1} maxLength={32} />
            <button className="btn primary big" type="submit" style={{ width: "100%" }}>Sign in (dev)</button>
          </form>
        ) : me?.signin ? (
          <>
            <p className="muted">Sign in to register bots, get API keys and keep your game history. Playing doesn't need an account.</p>
            <a className="btn primary big" href="/login" style={{ width: "100%" }}>{me.signin === "google" ? "Continue with Google" : "Sign in (dev)"}</a>
            <p className="tiny">We only ask Google for your name and email.</p>
          </>
        ) : <p className="muted">Sign-in isn't set up on this server. You can still <Link to="/play">play</Link>.</p>}
      </div>
    </div>
  );
}
