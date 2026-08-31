# compute_share (POC)

A proof of concept for peer-to-peer compute sharing between Adiyan
users: if your own machine is busy, offload a single inference to
someone else's Adiyan instance, get the result back, without exposing
anything beyond the exact prompt being run.

## The plan this POC proves the first phase of

**Phase 0, the trust boundary.** Only stateless inference gets offloaded,
a fully-built prompt in, a completion out. Never conversation history,
documents, memory, or identity. The requester's own instance builds the
prompt locally (with all private context already folded in) before
anything leaves the machine.

**Phase 1, this POC.** Two Adiyan instances, direct peer announce (no
shared registry yet), one offloads a real prompt to the other, gets a
real completion back, tagged with who served it.

**Phase 2, a real network.** Peers reachable over an overlay network
(Tailscale/WireGuard) instead of solving NAT traversal from scratch, and
a shared/public registry instead of a direct announce - closer to a
BitTorrent tracker than a phone book. See the `## BitTorrent, briefly`
section below for what maps and what doesn't.

**Phase 3, trust hardening.** Treat a peer's completion as untrusted
input, the same scrutiny as a retrieved document, not a trusted local
model output. Reciprocity so it's not one-way exploitation - a peer who
contributes gets priority when they're the one overloaded later.

## What's built here (Phase 1)

- `run_inference` - the only skill a peer ever exposes to someone else.
  Fully-built prompt in, completion out. Nothing else reachable.
- `announce_peer` - registers "this URL is willing to take work" in the
  caller's own local table. Stands in for a tracker announce.
- `offload` - "my machine is busy, find someone and route to them."
  Picks a known peer, calls their `run_inference` over real A2A with a
  token scoped to exactly that one skill, returns the completion plus
  who served it.
- A new `peer` permission tier in `mesh/lib/permissions_config.json`,
  allowed onto `compute_share.run_inference` and nothing else, on any
  agent.

## Verified live, not just read over

Two real, separate processes (not a mock), simulating two different
users' machines:

```bash
COMPUTE_SHARE_DISPLAY_NAME=alice COMPUTE_SHARE_PORT=8460 python3 -m mesh.compute_share.server
COMPUTE_SHARE_DISPLAY_NAME=bob   COMPUTE_SHARE_PORT=8461 python3 -m mesh.compute_share.server
```

Confirmed:
1. Bob announces himself to Alice's registry. Alice, "full," offloads a
   real prompt. Bob's own Ollama runs it. Alice gets back
   `{'served_by': 'http://127.0.0.1:8461', 'completion': 'The capital of
   France is Paris.'}` - a real answer from a real second process.
2. The trust boundary actually holds, not just declared in a config
   file: a `peer`-tier token was rejected calling `announce_peer`
   (`Not authorized for this`), rejected calling `offload` (same), and
   *allowed* calling `run_inference` - the one thing it should be able
   to do.

## What this POC deliberately does not solve

- **NAT traversal.** Both instances ran on localhost. A real deployment
  needs peers reachable across two home networks - Phase 2's job, not
  this one's.
- **Discovery at scale.** `announce_peer` is a direct call to one other
  instance. A real deployment needs a shared registry (or DHT) so peers
  don't need to already know each other's address.
- **Output integrity.** A completion is generative, not fixed content -
  there's no hash to verify it against the way a torrent piece is
  verified. A malicious peer can return a plausible-but-wrong or
  injected completion, and nothing here catches it structurally. This
  has to stay a "treat it as untrusted input" problem downstream, not a
  "verify it" problem solved here.
- **Incentive/abuse.** Nothing here stops a peer from taking work and
  never reciprocating, or from being hammered by one greedy requester.
  Phase 3's tit-for-tat idea is unbuilt.

## BitTorrent, briefly

A tracker/DHT maps onto the peer registry. A swarm maps onto "everyone
currently willing to share compute right now." Tit-for-tat maps onto the
actual answer to "why would a stranger run my inference" - reciprocity,
not altruism. Piece-based parallel distribution maps well onto a
*batch* of independent work (e.g. a nightly reading queue with many
pages across many users, scattered across willing peers the way a
torrent's chunks come from different seeders), much better than it maps
onto a single request. Piece hash verification does **not** map: a
torrent client checks each piece against a fixed hash from the original
file; an LLM completion has no such fixed answer to check against.
