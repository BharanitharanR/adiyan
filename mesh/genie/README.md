# Genie

The Daily Practice tablet app's helper inside Adiyan. Structured A2A calls only (no WhatsApp chat routing),
guarded by the family device key `GENIE_DEVICE_KEY` (vault) sent in the request metadata. Reached through the
gateway at `http://<this-mac>:8081/agents/genie/` with the `A2A-Version: 1.0` header.

| Skill | What it does |
| --- | --- |
| `socratic_nudge` | One short guiding question for a practice question, never the answer (checked, retried once, else refused) |
| `listen_and_check` | Whisper (Voicebox, via AdiyanReader's pipeline) + a check: `reading` compares a read-aloud with the passage word by word; `explanation` checks a spoken "how I solved it" against the problem-solving steps |
| `parent_verify_start` / `parent_verify_confirm` | Verifies a parent's WhatsApp number with a 6-digit code |
| `notify_parent` / `parent_status` / `parent_remove` | WhatsApp updates to verified numbers only (rate-limited) |
| `ping` / `peers` / `announce` | The family mesh (below) |

## The family mesh

Like `mesh/compute_share` (peer sharing, communitySearch), there's no central server: every family genie keeps
its own peer list, filled by two-way gossip (`announce`), and the tablet races known genies with `ping` and uses
the first free one. Unlike communitySearch, peers are only the family's own Adiyan installs:

- every peer knows the same `GENIE_DEVICE_KEY` (set it in each install's vault), and
- peer addresses must be on the tailnet (`100.64.0.0/10`, `*.ts.net`) or the home network - never the internet.

Reachability at home and away is Tailscale's job. A genie without its own WhatsApp link forwards parent
calls to a family peer that has one. Peers not heard from for 14 days are dropped.

```bash
python -m mesh.genie.add_peer --list
python -m mesh.genie.add_peer http://other-mac.tailXXXX.ts.net:8081/agents/genie/
```

## Settings (config dashboard)

`hint_prompt_template`, `wish_1_rule`, `wish_2_rule`, `max_words`, `max_chars`, `stt_model`,
`parent_app_name`, `family_self_url`; stage models `hint` and `check_approach` in `runtime_config.json`.
