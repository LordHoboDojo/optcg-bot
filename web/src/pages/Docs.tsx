import { marked } from "marked";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Loading, useTitle } from "../components/Layout";

export default function Docs() {
  useTitle("Bot API");
  const [html, setHtml] = useState<string | null>(null);
  useEffect(() => {
    // The doc is the project's own docs/bot-api.md, served by our server.
    api.docs().then((md) => setHtml(marked.parse(md, { async: false }) as string)).catch(() => setHtml("<p>Couldn't load the docs.</p>"));
  }, []);
  return (
    <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1fr)", gap: 24 }}>
      <div className="notice row">
        <span>Ready to connect? Register a bot and create an API key.</span>
        <span className="spacer" />
        <Link className="btn small primary" to="/bots">Your bots</Link>
      </div>
      {html === null ? <Loading height={600} /> : <article className="prose" dangerouslySetInnerHTML={{ __html: html }} />}
    </div>
  );
}
