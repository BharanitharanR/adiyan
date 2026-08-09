---
name: push-changes
description: Stage, commit, and push all current changes in the Adiyan repo to GitHub. Use whenever the user asks to push, ship, sync, save, or commit their work.
---

# Push changes

This skill takes the repo from "has uncommitted work" to "pushed to origin/main" in one go. Once invoked, carry it through to a push — don't stop partway to ask "should I commit?" unless a guardrail below says to stop.

Repo root: `/Users/bharani/Desktop/aiAgentCompaction/Adiyan`

## Steps

1. `git -C /Users/bharani/Desktop/aiAgentCompaction/Adiyan status --short`. If there's nothing to commit, say so and stop — don't create an empty commit.
2. Review the diff for anything that shouldn't be pushed, before staging:
   - Hardcoded secrets or API keys — the OpenWA key follows the pattern `owa_k1_...` and has leaked into test files before (`grep -rn "owa_k1_"` across changed files is a fast check).
   - `.env` files, `node_modules/`, `__pycache__/`, `penwa/` (nested third-party clone with its own `.git`) — all of these should already be excluded by `.gitignore`; if `git status` shows one of them anyway, stop and fix `.gitignore` first rather than committing it.
   If something suspicious turns up, stop and flag it to the user instead of committing it.
3. Stage the relevant files with `git add` — prefer explicit paths over `git add -A` when the diff includes anything unexpected, so nothing untracked slips in silently.
4. Write a commit message explaining *why* the change was made, not just what changed — one to three sentences. Match the tone of prior commits (`git log --oneline` for reference: e.g. "Fix: only respond in 1:1 chats, never in groups").
5. Commit, ending the message with:
   ```
   Co-Authored-By: Claude <noreply@anthropic.com>
   ```
6. `git -C /Users/bharani/Desktop/aiAgentCompaction/Adiyan push`.
   - If the push is rejected because history diverged, `git pull --rebase` first, resolve any conflicts, then push again.
   - Never force-push (`--force` / `--force-with-lease`) unless the user explicitly asks for it in this same request.
7. Confirm success: show `git log --oneline -3` and the pushed commit hash so the user can see exactly what landed.

## Guardrails

- Never commit `Adiyan/penwa/` or any `node_modules/` directory.
- If `gh auth status` reports not logged in and the push needs auth that isn't already configured via git credentials, tell the user to run `gh auth login` and stop rather than guessing at credentials.
- If a pre-commit/pre-push hook fails, fix the underlying issue and create a new commit — don't bypass with `--no-verify`.
