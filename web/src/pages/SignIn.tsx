import { useState, type FormEvent } from "react";
import { supabase } from "../lib/supabase";
import { ErrorBox, Page } from "../components/ui";

export default function SignIn() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const { error } = await supabase.auth.signInWithOtp({
      email: email.trim(), options: { emailRedirectTo: window.location.origin },
    });
    setBusy(false);
    if (error) setError(error.message);
    else setSent(true);
  }

  return (
    <Page>
      <div className="stack" style={{ marginTop: "12vh" }}>
        <h1>Shoot like the reels you love.</h1>
        <p className="muted">ReelCraft breaks a reel into a simple shoot plan, checks every take on set, and
          cuts your footage to match.</p>
        {sent ? (
          <div className="notice good" role="status">
            Check <strong>{email}</strong> for a sign-in link. You can close this tab.
          </div>
        ) : (
          <form className="stack" onSubmit={submit}>
            <label className="stack" style={{ gap: 6 }}>
              <span className="small muted">Email</span>
              <input type="email" required autoComplete="email" value={email}
                onChange={(e) => setEmail(e.target.value)} placeholder="you@studio.com" />
            </label>
            <button className="btn primary big block" disabled={busy}>{busy ? "Sending…" : "Email me a sign-in link"}</button>
            <ErrorBox>{error}</ErrorBox>
          </form>
        )}
      </div>
    </Page>
  );
}
