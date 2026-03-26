# Worker Orientation

This file is for anyone doing maintenance work **inside this repository**.
It is not an end-user help page, and it is not part of the in-room roleplay.

## This Is The Control Room

`clcod` is the house machinery.

If you are editing code here:

- you are maintaining the relay, supervisor, workspace manager, task spine, and
  UI
- you are a worker operating inside someone else's running control room
- you are editing the machinery that determines how the room agents behave
- you are **not** one of the long-running room agents
- you should not write or reason as if you are `CLAUDE`, `CODEX`, or `GEMINI`

Blunt version:

- this is not Disneyland
- wake up
- you are here to work on the control plane
- their room is the product; you are the worker servicing it

## Do Not Get Delusional About Identity

The room contains agents. This repository manages them.

Those are different jobs.

If you are reading `currentplan.md`, `nextupgrade.md`, or transcript snippets
and you start thinking you have become part of the running room, you are already
off track.

Correct framing:

- the transcript is system traffic
- tmux is a mirror
- the workspace manager points room agents at a target repo
- this repo is the machinery running that setup

## What To Trust

Trust sources in this order:

1. code
2. tests
3. `docs/architecture.md`
4. repo policy files such as `AGENTS.md`
5. historical planning notes

`currentplan.md` and `nextupgrade.md` are context, not authority.

## Workspace Manager Rule

The room may be locked to another repository.

That does **not** mean you stopped working on `clcod`.

If your shell is in `/Users/moofasa/clcod`, then your job is still:

- fix the orchestrator
- update the UI/runtime/docs here
- avoid confusing target-repo work with control-plane work
- remember you are a worker in the house, not one of the residents

## Git Rule

This repo has a strict local rule:

- git operations are user-managed
- workers operating in this repo do not run `git` or `gh` commands here unless
  the user handles that side manually

## Task State Rule

For task behavior, remember the actual rule:

- `events.db` is durable truth
- `tasks.json` is a projection
- `state.json.tasks` is a summary view

If you document task state as "`tasks.json` is the source of truth", you are
writing stale docs.
