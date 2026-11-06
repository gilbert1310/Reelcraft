import { Link } from "react-router-dom";
import { Page, SignOut } from "../components/ui";

export default function Home() {
  return (
    <Page actions={<SignOut />}>
      <h1>What are we making?</h1>
      <p className="muted">Start from a reel you love, or from footage you already have.</p>
      <div className="stack" style={{ marginTop: 20 }}>
        <Link className="option" to="/breakdown">
          <span className="icon" aria-hidden>🎬</span>
          <span><strong>Break down a reel</strong><span className="muted small">Upload or paste a link. Get a shoot plan in setups, not cuts.</span></span>
        </Link>
        <Link className="option" to="/shoots">
          <span className="icon" aria-hidden>📋</span>
          <span><strong>My shoots</strong><span className="muted small">Guided shooting, on-set checks and your finished edits.</span></span>
        </Link>
        <Link className="option" to="/footage">
          <span className="icon" aria-hidden>✂️</span>
          <span><strong>Edit my own footage</strong><span className="muted small">Upload raw clips and get three edit styles to choose from.</span></span>
        </Link>
        <Link className="option" to="/library">
          <span className="icon" aria-hidden>📚</span>
          <span><strong>Style library</strong><span className="muted small">Every reel analysed teaches ReelCraft a new move.</span></span>
        </Link>
      </div>
    </Page>
  );
}
