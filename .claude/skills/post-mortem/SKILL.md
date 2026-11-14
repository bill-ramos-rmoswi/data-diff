---
name: post-mortem
description: Write a post-mortem of an orchestrated run, a phased task or a session — what it cost, where it waited, what went well and what could be better — and turn each lesson into a proposed change to the skills, agents or CLAUDE.md. Use when a run or release has finished, when the user asks for a post-mortem, retro or lessons learned, or runs /post-mortem.
---

# Writing a post-mortem

A post-mortem is for the user and for the next run. It turns what happened into changes to the skills, agents, plan template and CLAUDE.md. It doesn't change code. Earlier ones are [retro-2026-11-11.md](../../../docs/plan/retro-2026-11-11.md), [postmortem-fewer-casts-2026-11-12.md](../../../docs/plan/postmortem-fewer-casts-2026-11-12.md) and [postmortem-tuning-2026-11-13.md](../../../docs/plan/postmortem-tuning-2026-11-13.md). Read the latest one first, so you don't repeat a lesson that has already been acted on, and can say whether its changes worked.

## 1. Collect the facts

```shell
python3 dev/postmortem.py <run-id> --exclude <this session's id>     # an orchestrated run
python3 dev/postmortem.py --session <id> <id>                        # sessions without a run
```

It prints, from the transcripts and `.runs/<run-id>/ledger.jsonl`:

- each session and agent, with its model, turns, cache-read tokens and wall time
- each question to the user, with how long it waited and the answer
- each push notification's result
- the user's own messages, and how often they asked `/status`
- the ledger's gaps

The transcripts are in `~/.claude/projects/-workspaces-data-diff/` (in WSL, `~/.claude-devcontainer/projects/`). Only change that with `CLAUDE_PROJECTS_DIR` if it isn't found.

Then collect the rest, reading only what you need:

- `.runs/<run-id>/phase-*.md`: their summaries, returned values, findings and "not measured" lists. Don't read whole reports.
- `.runs/<run-id>/ab-*/summary.md`: the before and after tables.
- `gh pr view <n> --repo bill-ramos-rmoswi/data-diff` and `gh run list --repo bill-ramos-rmoswi/data-diff --limit 15`: the PRs, the CI cycles and their times.
- `git log --oneline <base>..<head>`: the commits, and any that fixed an earlier commit of the same run.
- The plan the run followed, for what it predicted (expected gains, the order of phases).

## 2. Find the lessons

Look for each of these, with a number:

| Question | Where it shows |
|---|---|
| Did the user act as the scheduler? | User messages that aren't STOP answers. `/model` switches. Pasted prompts |
| How long did STOPs wait, and could the plan have decided them in advance? | The questions' waits. Whether the recommended option was chosen |
| Did notifications reach the user? | Push results ("not sent") |
| Did a job look stuck? | `/status` calls, "what's happening?" messages, jobs with no progress lines |
| Which agent cost the most, and did its work need that model? | The agents table. The phase it ran. Resumptions |
| Did an estimate miss? Why? | The design's expected gain against the A/B result. The plan's "assumed" items |
| Was any work done twice? | Re-runs after fixes. Later commits fixing a commit from the same run |
| What did a later phase find that an earlier one should have? | Review findings. Implementer findings that contradict the design |
| How long did shipping and release take, and how many CI cycles? | `gh run list`, the release session |

Keep what you observed apart from what you infer. Say "assumed" where you didn't check.

## 3. Write the document

`docs/plan/postmortem-<slug>-<yyyy-mm-dd>.md`, with these sections:

1. One paragraph: the run, its plan, its result and its PR. Say where the figures come from.
2. **What happened:** a table per phase, with agent, model, turns, cache-read tokens, wall time and result, plus a total row and a comparison with the last post-mortem.
3. **What went well:** each point with its evidence.
4. **What could have gone better:** numbered, each with its evidence and its cost (time, tokens or a second run).
5. **What changes:** a table mapping each numbered problem to the change in a skill, agent, template, CLAUDE.md or tool.
6. **Open findings, carried forward:** bugs and questions the run found but didn't fix.

Write for the user, who knows the project. Use short paragraphs and lists, and give a number where you have one.

## 4. Propose the changes, then make them

- List the proposed edits as file, change and the problem it answers. Ask once with AskUserQuestion before editing skills, agents, CLAUDE.md, the template or workflows. Writing the document needs no permission.
- Prefer a rule in the skill or agent that makes the mistake, over a general note in CLAUDE.md.
- Prefer a tool, a check or a default to a sentence that says "remember to".
- If a lesson repeats one from an earlier post-mortem, say that the earlier change didn't work, and why.

## 5. Hand off

- Open findings that someone should act on go into a `handoff-<slug>` project memory, as `/shutting-down` describes, with the next step and its model.
- Commit the document and the changes on a `docs/` or `chore/` branch, and open a PR with `/ship` when the user asks.

## Examples

- `/post-mortem 20261113-tuning`
- `/post-mortem session 3bdd9ba5 02e14b8e`: a run plus its release session
- `/post-mortem the v0.15.0 release`: find the sessions with `--session`, from the dates in `gh release view`
