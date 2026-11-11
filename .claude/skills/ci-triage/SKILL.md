---
name: ci-triage
description: Diagnose a failed GitHub Actions run in bill-ramos-rmoswi/data-diff without pulling its logs into the conversation — classify it as external, flaky or a real failure, and say what to do. Use when a PR or master run fails, or when the user runs /ci-triage.
---

# Triage of a failed CI run

Hand the logs to the `ci-triage` agent, which runs on a small model. A failed CI log is often thousands of lines, so it doesn't belong in the main conversation.

1. Find the run: the id the user gave, or `gh run list --repo bill-ramos-rmoswi/data-diff --status failure --limit 5`.
2. Start the `ci-triage` agent with the run id, and the branch and commit if they're known.
3. Relay its report:
   - **Class:**
     - external: runner, apt mirror, GHCR or Docker Hub
     - flaky: a timeout or lock that passes on re-run
     - real: a test or format failure caused by the change
   - **Evidence:** the failing job and step, and at most 5 log lines.
   - **Next step:**
     - external: re-run (`gh run rerun <id> --failed`, after asking) or a CI fix
     - flaky: re-run, and note the test
     - real: the test to reproduce locally with `/test`

Past cases, to recognize quickly:

- **External.** The Azure Ubuntu mirror didn't resolve on the runners. The fix was an `archive.ubuntu.com` fallback (PR #6).
- **Real.** A tag release fails when master is red, so fix master first and re-tag. Don't re-run.

## Examples

- `/ci-triage the failed master run after PR #5`
- `/ci-triage 43067879733`
