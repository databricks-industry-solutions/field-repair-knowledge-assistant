// Renders a single R&DTASK citation as a chip. Citations are the trust signal
// (CONTEXT Specific Ideas): each item in an answer's `citations[]` is a
// ServiceNow R&D task number like "R&DTASK0001006".
//
// A per-ticket URL IS knowable when the KA cites the source file — it arrives in
// the answer's footnote block and the backend harvests it into `sources` (see
// mas.extract_sources). When we have one, the chip is a link; when we don't, it
// stays a plain chip rather than a dead link.

interface CitationProps {
  id: string;
  url?: string;
}

export default function Citation({ id, url }: CitationProps) {
  if (url) {
    return (
      <a
        className="citation-chip citation-link"
        href={url}
        target="_blank"
        // noreferrer alongside noopener: the workspace URL should not leak the
        // app's referrer, and target=_blank without it is a tabnabbing vector.
        rel="noopener noreferrer"
        title={`Open source ticket ${id}`}
      >
        {id}
      </a>
    );
  }
  return (
    <span className="citation-chip" title={`ServiceNow R&D task ${id}`}>
      {id}
    </span>
  );
}
