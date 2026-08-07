// Chat client for the FIS R&D Knowledge Agent front door.
//
// SECURITY (T-06-09 / RESEARCH Pitfall 5): this client calls the app's OWN
// backend over SAME-ORIGIN RELATIVE `/api` paths ONLY. It NEVER calls the
// Databricks workspace host or the model-serving invocations path directly,
// and it never
// sees or handles the end-user OBO token — that stays server-side. Do not
// introduce absolute workspace URLs here.

export interface ChatAnswer {
  answer: string;
  citations: string[];
  // ticket id -> workspace source URL. Sparse: not every cited ticket has one.
  sources: Record<string, string>;
}

// The backend poll shape (06-03): running | done{answer,citations,sources} | error{detail}.
export type PollStatus = "running" | "done" | "error";

export interface PollResult {
  status: PollStatus;
  // Human status label while running ("Searching prior R&D cases", ...).
  progress?: string;
  answer?: string;
  citations?: string[];
  sources?: Record<string, string>;
  detail?: string;
}

async function req<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

/** Submit a question. Returns a job_id immediately (backend is async). */
async function submit(question: string): Promise<{ job_id: string }> {
  return req<{ job_id: string }>("/api/chat", {
    method: "POST",
    body: JSON.stringify({ question }),
  });
}

/** Poll a job by id. Returns running | done | error. */
async function poll(job_id: string): Promise<PollResult> {
  return req<PollResult>(`/api/chat/${job_id}`);
}

const POLL_INTERVAL_MS = 1500;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Submit a question and poll until done/error.
 *
 * The A4 fan-out can take ~130s, so we submit then poll on an interval
 * (~1.5s) rather than blocking on a single long request (the backend
 * runs the MAS off the request path — 06-03). `onUpdate` is called on
 * each poll tick with the backend's live `progress` label, so the UI can show
 * which stage the agent is in rather than a mute spinner.
 */
async function ask(
  question: string,
  onUpdate?: (status: PollStatus, progress?: string) => void
): Promise<ChatAnswer> {
  const { job_id } = await submit(question);
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const result = await poll(job_id);
    onUpdate?.(result.status, result.progress);
    if (result.status === "done") {
      return {
        answer: result.answer ?? "",
        citations: result.citations ?? [],
        sources: result.sources ?? {},
      };
    }
    if (result.status === "error") {
      throw new Error(result.detail ?? "The agent returned an error.");
    }
    await delay(POLL_INTERVAL_MS);
  }
}

export const api = { submit, poll, ask };
