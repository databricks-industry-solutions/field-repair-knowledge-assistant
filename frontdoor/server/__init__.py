"""Front-door FastAPI backend package (Phase 06).

Thin identity-forwarding + timeout-handling tier over the warm
`mas-f5fc28b0-endpoint` Multi-Agent Supervisor. All retrieval / reasoning
lives in the reused MAS; this package only forwards the end-user OBO token and
survives the 120s Apps proxy limit via submit/poll.
"""
