import { useState } from "react";
import { post } from "../api";

export function PairView({ onPaired }: { onPaired: () => void }) {
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await post("/api/auth/pair", { code: code.trim() });
      onPaired();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="center-screen">
      <form className="card narrow" onSubmit={submit}>
        <h1>Pair this browser</h1>
        <p className="muted">Enter the pairing code printed in the terminal where Agentic OS is running.</p>
        <input autoFocus className="code-input" placeholder="ABCD-EFGH" value={code}
               onChange={(e) => setCode(e.target.value.toUpperCase())} />
        {err && <p className="error">{err}</p>}
        <button className="primary" disabled={busy || code.trim().length < 4}>Pair</button>
      </form>
    </div>
  );
}
