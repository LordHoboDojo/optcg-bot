import { Link } from "react-router-dom";
import { HakiBurst } from "../components/Haki";
import { useTitle } from "../components/Layout";

export default function NotFound() {
  useTitle("Not found");
  return (
    <div className="card empty lost" style={{ maxWidth: 520, margin: "40px auto" }}>
      <HakiBurst seed={404} bolts={16} />
      <h3>That page doesn't exist</h3>
      <p style={{ marginBottom: 18 }}>It may have moved, or the link is wrong.</p>
      <Link className="btn primary" to="/">Back home</Link>
    </div>
  );
}
