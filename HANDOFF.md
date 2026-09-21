# Handoff / Progress Log

This file tracks live build progress — what's actually done, not just what's planned. Read this before starting any task; update it before finishing one.

## Current Phase
Phase 0 — Setup (see docs/spec/10-roadmap.html)

## This Phase — Task Checklist
- [x] GitHub repo created, cloned, and configured with the GitHub origin
- [x] Initial README-only commit pushed
- [ ] Application starter scaffold created
- [ ] Render account created, connected to GitHub
- [ ] Vercel account created, connected to GitHub
- [ ] Supabase project created, connection string saved
- [x] Codex available and reading AGENTS.md / CLAUDE.md correctly
- [x] Python 3.10+ confirmed installed (Python 3.14.7)
- [ ] Node.js confirmed installed
- [ ] FPL Team ID saved somewhere accessible

## Completed Phases
(none yet)

## Deviations From Spec
Anything implemented differently than `docs/spec/` says goes here immediately. Small deviations stay logged here; anything significant enough to change an actual spec decision gets reflected back into the real spec file, not just noted here.

- none yet

## Known Issues / Gotchas
Things the next session should know before touching this code.

- Node.js and npm are not currently available on this machine; they must be installed before the Next.js scaffold is created.

## Tool Log
Which tool did which piece of work — useful for knowing where to look first if something breaks.

- Codex — inspected the repository and specification, then completed the approved Phase 0 repository-initialization cleanup (`.gitignore`, `.env.example`, synchronized agent guidance, and current-state documentation).
