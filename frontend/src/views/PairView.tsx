import { useState } from "react";
import { KeyRound } from "lucide-react";
import { post } from "../api";
import { Banner, ICON } from "../ui";

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
    <div className="center">
      <form className="ee-card pair" onSubmit={submit}>
        <span className="ee-mark" style={{ width: 48, height: 48 }} aria-hidden><KeyRound {...ICON} /></span>
        <div>
          <h1 className="page-title" style={{ margin: "0 0 8px" }}>Pair this browser</h1>
          <p className="ink2" style={{ margin: 0 }}>Type the pairing code shown in the window where Agentic OS is running.
            Only paired browsers on this computer can use it.</p>
        </div>
        <input autoFocus className="input code-input" aria-label="Pairing code" placeholder="ABCD-EFGH" value={code}
               onChange={(e) => setCode(e.target.value.toUpperCase())} />
        {err && <Banner tone="bad">{err}</Banner>}
        <button className="ee-btn primary lg" disabled={busy || code.trim().length < 4}>Pair</button>
      </form>
    </div>
  );
}
