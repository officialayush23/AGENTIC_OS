// Voice: speech-to-text (browser speech recognition, or recorded audio sent to the AI provider) and text-to-speech.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api";

type Rec = any;

function recognitionCtor(): any {
  const w = window as any;
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

export async function transcribe(blob: Blob): Promise<string> {
  const { rawUrl } = await import("../api");
  const headers: Record<string, string> = { "X-Agentic-OS": "1", "Content-Type": blob.type || "audio/webm" };
  const tok = (await import("../api")).currentToken();
  if (tok) headers.Authorization = `Bearer ${tok}`;
  const res = await fetch(rawUrl("/api/voice/transcribe"), { method: "POST", body: blob, headers, credentials: tok ? "omit" : "include" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data?.error?.code ?? "VOICE", data?.error?.message ?? "Transcription failed");
  return data.text ?? "";
}

/** Push-to-talk. Uses on-device speech recognition when the browser has it, otherwise records and transcribes. */
export function useVoice(onText: (text: string) => void) {
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<Rec | null>(null);
  const media = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const mode = recognitionCtor() ? "browser" : typeof MediaRecorder !== "undefined" ? "record" : "none";

  const stop = useCallback(() => {
    try { rec.current?.stop(); } catch { /* already stopped */ }
    try { if (media.current?.state === "recording") media.current.stop(); } catch { /* ignore */ }
  }, []);

  const start = useCallback(async () => {
    setError(null);
    setInterim("");
    window.speechSynthesis?.cancel();
    const Ctor = recognitionCtor();
    if (Ctor) {
      const r = new Ctor();
      r.lang = navigator.language || "en-US";
      r.interimResults = true;
      r.continuous = false;
      let final = "";
      r.onresult = (e: any) => {
        let t = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          if (e.results[i].isFinal) final += e.results[i][0].transcript; else t += e.results[i][0].transcript;
        }
        setInterim(final + t);
      };
      r.onerror = (e: any) => { setError(e.error === "not-allowed" ? "Microphone access was blocked." : `Speech recognition: ${e.error}`); };
      r.onend = () => { setListening(false); setInterim(""); if (final.trim()) onText(final.trim()); };
      rec.current = r;
      r.start();
      setListening(true);
      return;
    }
    if (mode === "record") {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const m = new MediaRecorder(stream);
        chunks.current = [];
        m.ondataavailable = (e) => chunks.current.push(e.data);
        m.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop());
          setListening(false);
          setInterim("Transcribing…");
          try {
            const text = await transcribe(new Blob(chunks.current, { type: m.mimeType || "audio/webm" }));
            setInterim("");
            if (text.trim()) onText(text.trim());
          } catch (e: any) { setInterim(""); setError(e.message); }
        };
        media.current = m;
        m.start();
        setListening(true);
      } catch {
        setError("Microphone access was blocked.");
      }
      return;
    }
    setError("This window has no microphone support.");
  }, [mode, onText]);

  useEffect(() => () => stop(), [stop]);
  return { listening, interim, error, start, stop, mode };
}

export function speak(text: string, enabled: boolean) {
  if (!enabled || !("speechSynthesis" in window)) return;
  const clean = text.replace(/https?:\/\/\S+/g, "link").replace(/[*_`#>]/g, "").slice(0, 900);
  const u = new SpeechSynthesisUtterance(clean);
  u.rate = 1.03;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(u);
}
