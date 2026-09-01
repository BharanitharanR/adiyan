# compute_share

Peer-to-peer compute sharing between Adiyan users: if your own machine is
busy, offload a single inference to someone else's Adiyan instance, get
the result back, without exposing anything beyond the exact prompt being
run. Platform infrastructure, not something an agent author touches
directly - see `mesh/lib/agent_sdk.py`'s `AdiyanAgent.ask()`, the one
caller.

## The plan, and what's actually built so far

**Phase 0, the trust boundary.** Only stateless inference gets offloaded,
a fully-built prompt in, a completion out. Never conversation history,
documents, memory, or identity. The requester's own instance builds the
prompt locally (with all private context already folded in) before
anything leaves the machine - which also means whatever context *was*
folded into that prompt leaves with it. `ask()`'s own `community`
parameter is the actual safety valve for this: a caller opts a specific
call into offload eligibility on purpose, never by default.

**Phase 1 - direct peer announce. Built, verified live.** Two instances,
one already knows the other's address, `offload` routes a real prompt to
`run_inference`, gets a real completion back.

**Phase 2 - gossip-based discovery. Built, verified live.** No shared
registry, no DHT - `announce_peer` is bidirectional (see its own
docstring): the caller's known-peer sample and the receiver's known-peer
sample get exchanged in the same round trip, the same shape as
BitTorrent's PEX extension. One manual bootstrap per new participant
(`mesh/tools/compute_share_bootstrap.py`, a magnet-link-style
`adiyan-peer://` string), then the address book spreads itself through
regular `gossip` rounds - see the Peer Exchange design doc for the full
mechanism and every hard problem it does and doesn't solve.

**Phase 3, trust hardening.** Treat a peer's completion as untrusted
input, the same scrutiny as a retrieved document, not a trusted local
model output. Reciprocity so it's not one-way exploitation - a peer who
contributes gets priority when they're the one overloaded later. Still
unbuilt.

## What's built here

- `run_inference` - the only skill a peer ever exposes to someone else.
  Fully-built prompt in, completion out. Nothing else reachable.
- `announce_peer` - two-way: records the caller, merges any peers the
  caller already knew that this instance didn't, and hands back a sample
  of what this instance itself knows. The actual discovery mechanism,
  not just a registration call.
- `gossip` - this instance's own outbound half of peer exchange: re-
  announces to a few known peers on a timer, self-recurring via
  `cron_trigger` the same way `mesh/adiyan_reader/skills/read_next_page.py`
  re-registers its own nightly recurrence.
- `offload` - "my machine is busy, find someone and route to them."
  Picks the freshest-fitting known peer (`db.pick_peer()` skips anyone
  not heard from recently), calls their `run_inference` over real A2A,
  returns the completion plus who served it.
- `mesh/tools/compute_share_bootstrap.py` - `--mine` prints this
  instance's own bootstrap string; consuming someone else's seeds it as
  a known peer and kicks off the first gossip round immediately.
- **No peer authentication**, by design, for `run_inference` and
  `announce_peer` specifically (`agent_executor.py`'s `PUBLIC_SKILLS`) -
  a genuinely different Adiyan install signs its own tokens with its own
  `PERMISSIONS_JWT_SECRET`, which this instance has no way to verify at
  all. The trust boundary is what's exposed (a narrow prompt-in/text-out
  door, or a write to this instance's own local peer table), not who's
  calling - the same stance BitTorrent takes (verify content, not
  identity). `offload` and `gossip` stay behind the normal permission
  check - neither is meant to be triggered by an arbitrary stranger.
- `compute_share_client` tier in `mesh/lib/permissions_config.json` -
  the AdiyanAgent SDK's own fixed internal identity when `ask()` falls
  back to a peer, never the calling agent's own tier.

## Verified live, not just read over

Every mechanism below was confirmed against real, separate processes -
not a mock, not read off the code and assumed correct:

1. **Two-way announce.** Bob announces to Alice; Alice's response
   carries her own known-peer sample back. A peer introduced to Bob but
   unknown to Alice (Carol) showed up in Alice's *next* response to Bob
   with zero direct introduction between Bob and Carol - the actual
   gossip propagation, not just a registration acknowledgment.
2. **Self-recurring gossip.** A `gossip` round registered its own next
   fire with `cron_trigger` and (after a live bug - a missing token -
   was caught and fixed) that registration was confirmed present in
   `cron_trigger`'s own job store, not just assumed from a "success"
   return value.
3. **Bootstrap end to end.** `--mine` on one instance, fed to another via
   the consuming CLI, correctly seeded the peer and completed a real
   first gossip round.
4. **The actual point of all of it.** `AdiyanAgent.ask()`, with local
   Ollama deliberately pointed at an unreachable address and
   `community='communitySearch'` passed, returned a real completion
   ('hello') - served by a second, separate compute_share process, not
   a fallback string or a mock.
5. **The opt-in boundary holds.** The same forced-local-failure call
   *without* `community` raised the original local error directly - no
   silent fallback, no attempt to reach a peer, confirmed by inspecting
   which exception type came back, not assumed from reading the code.

## What's still genuinely unsolved

- **NAT traversal at scale.** A real deployment needs peers reachable
  across separate home networks - an overlay network (Tailscale/
  WireGuard) handles this underneath compute_share without either
  needing to know about the other; see the Tailscale Compute Sharing
  diagram for exactly which layer does which job.
- **A public bootstrap seed.** Someone needs to run at least one
  always-on instance at a stable address for a brand-new user with zero
  contacts to join without a person-to-person introduction. An
  operational commitment, not a code change.
- **Output integrity.** A completion is generative, not fixed content -
  there's no hash to verify it against the way a torrent piece is
  verified. A malicious peer can return a plausible-but-wrong or
  injected completion, and nothing here catches it structurally. Stays a
  "treat it as untrusted input" problem downstream.
- **Incentive/abuse/Sybil resistance.** Nothing stops a peer from taking
  work and never reciprocating, being hammered by one greedy requester,
  or one person running many fake identities to dominate the peer pool.
  Phase 3's tit-for-tat idea is unbuilt; BitTorrent's own DHT has the
  same Sybil weakness, for what it's worth.

## BitTorrent, briefly

Gossip-based peer exchange maps onto BitTorrent's own PEX extension -
peers that already found each other trade address books instead of
re-querying a tracker every time. A swarm maps onto "everyone currently
willing to share compute right now." Tit-for-tat maps onto the actual
answer to "why would a stranger run my inference" - reciprocity, not
altruism, and still unbuilt here. Piece-based parallel distribution maps
well onto a *batch* of independent work (e.g. a nightly reading queue
with many pages across many users, scattered across willing peers the
way a torrent's chunks come from different seeders), much better than it
maps onto a single request. Piece hash verification does **not** map: a
torrent client checks each piece against a fixed hash from the original
file; an LLM completion has no such fixed answer to check against.
