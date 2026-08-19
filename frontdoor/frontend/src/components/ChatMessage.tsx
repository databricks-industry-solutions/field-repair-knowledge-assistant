import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Citation from "./Citation";

export interface Message {
  role: "user" | "assistant";
  text: string;
  // R&DTASK citation ids, present on assistant turns once done.
  citations?: string[];
  // ticket id -> workspace source URL. Sparse — a cited ticket may have no URL.
  sources?: Record<string, string>;
  // Transient assistant state while the poll loop is running.
  thinking?: boolean;
  // Live status label from the backend while thinking ("Querying ticket data").
  progress?: string;
  error?: boolean;
}

interface ChatMessageProps {
  message: Message;
}

export default function ChatMessage({ message }: ChatMessageProps) {
  const { role, text, citations, sources, thinking, progress, error } = message;

  if (thinking) {
    return (
      <div className="msg assistant">
        {/* The label tracks the MAS routing stage (backend `progress`), so a ~40s
            turn reads as work in progress rather than a hang. Falls back to the
            original copy if the backend sent no label. */}
        <div className="bubble thinking">
          {progress || "Searching prior R&D cases"}
          <span className="dots" />
        </div>
      </div>
    );
  }

  // The user's own text is shown verbatim — it is not markdown, and rendering it
  // as such would mangle a question that happens to contain * or _.
  const isMarkdown = role === "assistant" && !error;

  return (
    <div className={`msg ${role}`}>
      <div className={`bubble${error ? " error-bubble" : ""}`}>
        {isMarkdown ? (
          // MAS answers are markdown (## headers, **bold**, numbered lists). Rendered
          // via react-markdown, which does NOT pass raw HTML through unless the
          // rehype-raw plugin is added — so the T-06-10 no-XSS guarantee still holds.
          // Do not add rehype-raw here.
          <div className="markdown">
            <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
          </div>
        ) : (
          // Plain text — React escapes by default.
          text
        )}
        {citations && citations.length > 0 && (
          <div className="citations">
            <div className="label">Cited R&amp;D tasks</div>
            {citations.map((id) => (
              <Citation key={id} id={id} url={sources?.[id]} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
