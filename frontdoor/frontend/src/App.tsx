import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import ChatMessage, { Message } from "./components/ChatMessage";

// The 5 archetype starter prompts (verbatim from agents/test_supervisor.py).
const STARTERS: { key: string; prompt: string }[] = [
  {
    key: "A1",
    prompt:
      "What does the acronym CA mean in these R&D tickets, and is it a roadside screening system or a software/controller term?",
  },
  {
    key: "A2a",
    prompt: "How many tasks are currently open or pending in New Mexico?",
  },
  {
    key: "A2b",
    prompt:
      "Who is the go-to engineer for AUR camera issues, and which prior cases back that up?",
  },
  {
    key: "A4",
    prompt:
      "Among our currently open tasks, which should the team prioritize right now, and why?",
  },
  {
    key: "A5",
    prompt:
      "What recurring problems keep coming back in New Mexico, and what should we do about them?",
  },
];

export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  async function send(question: string) {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setInput("");
    // Append the user turn + a transient "thinking" assistant turn.
    setMessages((prev) => [
      ...prev,
      { role: "user", text: q },
      { role: "assistant", text: "", thinking: true },
    ]);

    try {
      // Submit + poll (~1.5s) until done — the A4 turn can take ~130s, so the
      // UI shows a live "thinking" state instead of freezing on one request.
      const answer = await api.ask(q, (_status, progress) => {
        // Each poll tick carries the MAS routing stage; surface it on the
        // still-thinking bubble so the wait is legible.
        if (!progress) return;
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.thinking) {
            next[next.length - 1] = { ...last, progress };
          }
          return next;
        });
      });
      setMessages((prev) => {
        const next = [...prev];
        next[next.length - 1] = {
          role: "assistant",
          text: answer.answer,
          citations: answer.citations,
          sources: answer.sources,
        };
        return next;
      });
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      setMessages((prev) => {
        const next = [...prev];
        next[next.length - 1] = {
          role: "assistant",
          text: `Sorry — something went wrong: ${detail}`,
          error: true,
        };
        return next;
      });
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    send(input);
  }

  return (
    <div className="app">
      <header className="brand">
        <div className="logo">RKB</div>
        <div>
          <h1>Field Repair Knowledge Assistant</h1>
          <p className="subtitle">
            Ask about prior R&amp;D cases — answers are cited back to ServiceNow tasks.
          </p>
        </div>
      </header>

      <div className="starters">
        <div className="label">Try one of these:</div>
        <div className="chip-row">
          {STARTERS.map((s) => (
            <button
              key={s.key}
              className="starter-chip"
              disabled={busy}
              onClick={() => send(s.prompt)}
            >
              {s.prompt}
            </button>
          ))}
        </div>
      </div>

      <div className="messages" ref={listRef}>
        {messages.length === 0 ? (
          <div className="empty">
            Pick a starter question above, or type your own below.
          </div>
        ) : (
          messages.map((m, i) => <ChatMessage key={i} message={m} />)
        )}
      </div>

      <form className="composer" onSubmit={onSubmit}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about a WIM, AUR, HTS, or ALPR issue…"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !input.trim()}>
          {busy ? "Thinking…" : "Send"}
        </button>
      </form>
    </div>
  );
}
