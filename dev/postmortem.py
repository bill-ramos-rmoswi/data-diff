#!/usr/bin/env python3
"""Facts for a post-mortem: what each session and agent cost, and where the run waited.

Reads Claude Code transcripts and an orchestrated run's ledger. Changes nothing.

    python dev/postmortem.py 20261113-tuning             # sessions that mention the run id
    python dev/postmortem.py --session 3bdd9ba5 02e14b8e  # these sessions (id prefixes work)
    python dev/postmortem.py 20261113-tuning --gap 30     # ledger gaps over 30 minutes

Transcripts are in ~/.claude/projects/<workspace slug>/ (in WSL, ~/.claude-devcontainer/projects/),
or in CLAUDE_PROJECTS_DIR. Each session is <id>.jsonl, with its agents in <id>/subagents/.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Context the IDE and CLI add to a user message, which isn't what the user typed
WRAPPERS = re.compile(r"<(ide_\w+|local-command-\w+|system-reminder)>.*?</\1>", re.S)


def projects_dir() -> Path:
    if os.environ.get("CLAUDE_PROJECTS_DIR"):
        return Path(os.environ["CLAUDE_PROJECTS_DIR"])
    slug = str(REPO).replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def hm(d):
    return d.strftime("%m-%d %H:%M") if d else "-"


def dur(a, b):
    if not a or not b:
        return "-"
    m = int((b - a).total_seconds() // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def records(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def text_of(content):
    if isinstance(content, str):
        return content
    return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")


def summarize(path):
    """Turns, cache-read tokens, models, first and last time of one transcript."""
    s = {"turns": 0, "cache_read": 0, "output": 0, "models": set(), "first": None, "last": None}
    for d in records(path):
        t = ts(d.get("timestamp"))
        if t:
            s["first"] = s["first"] or t
            s["last"] = t
        if d.get("type") == "assistant":
            m = d.get("message", {})
            s["turns"] += 1
            if m.get("model") and not m["model"].startswith("<"):
                s["models"].add(m["model"].replace("claude-", ""))
            u = m.get("usage") or {}
            s["cache_read"] += u.get("cache_read_input_tokens", 0)
            s["output"] += u.get("output_tokens", 0)
    return s


def session_events(path):
    """User messages, questions with their waits, and push results in a main session."""
    asked, pushes, users, questions = {}, [], [], []
    for d in records(path):
        t = ts(d.get("timestamp"))
        msg = d.get("message") or {}
        content = msg.get("content")
        if d.get("type") == "assistant" and isinstance(content, list):
            for c in content:
                if c.get("type") == "tool_use" and c.get("name") in ("AskUserQuestion", "PushNotification"):
                    asked[c["id"]] = (c["name"], t, c.get("input", {}))
        elif d.get("type") == "user" and not d.get("isMeta"):
            if isinstance(content, list) and any(
                c.get("type") == "tool_result" for c in content if isinstance(c, dict)
            ):
                for c in content:
                    if c.get("type") == "tool_result" and c.get("tool_use_id") in asked:
                        name, t0, inp = asked[c["tool_use_id"]]
                        result = c.get("content")
                        result = text_of(result) if isinstance(result, list) else str(result)
                        if name == "PushNotification":
                            pushes.append((t0, inp.get("message", ""), result))
                        else:
                            q = (inp.get("questions") or [{}])[0]
                            answer = result.split('"=', 1)[-1].split('". You can', 1)[0].strip('"')
                            questions.append((t0, t, q.get("header", ""), q.get("question", ""), answer))
                continue
            txt = WRAPPERS.sub("", text_of(content)).strip()
            if not txt:
                continue
            if txt.startswith("<task-notification>"):
                kind = "task-notification"
            elif "<command-name>" in txt:
                kind = txt.split("<command-name>", 1)[1].split("<", 1)[0]
            else:
                kind = "message"
            users.append((t, kind, " ".join(txt.split())[:160]))
    return users, questions, pushes


def find_sessions(pdir: Path, run_id, prefixes):
    files = sorted(pdir.glob("*.jsonl"))
    if prefixes:
        return [f for f in files if any(f.stem.startswith(p) for p in prefixes)]
    needle = f".runs/{run_id}"
    return [f for f in files if needle in f.read_text(encoding="utf-8", errors="replace")]


def agent_label(meta_path: Path):
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "?", ""
    return meta.get("agentType") or meta.get("subagent_type") or "?", meta.get("description", "")


def ledger(run_id, gap_min):
    path = REPO / ".runs" / run_id / "ledger.jsonl"
    if not path.exists():
        return
    print(f"\n## Ledger {path.relative_to(REPO)}: waits of {gap_min}+ min between events")
    prev = None
    for d in records(path):
        t = ts(d.get("t"))
        label = f"{d.get('event')} {d.get('phase', d.get('job', d.get('stop', '')))}".strip()
        if prev and t and (t - prev[0]).total_seconds() >= gap_min * 60:
            print(f"- {hm(prev[0])} {prev[1]}  ->  {hm(t)} {label}: {dur(prev[0], t)}")
        if d.get("event") in ("stop", "resume"):
            why = d.get("why") or d.get("decision") or ""
            print(f"  {hm(t)} {d['event'].upper()}: {why}")
        prev = (t, label)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id", nargs="?", help="orchestrated run id, the name of a .runs/ folder")
    ap.add_argument("--session", nargs="+", help="session ids or prefixes, instead of searching for the run id")
    ap.add_argument("--exclude", nargs="+", default=[], help="session ids or prefixes to leave out (this session)")
    ap.add_argument("--gap", type=int, default=15, help="report ledger gaps of at least this many minutes")
    args = ap.parse_args()
    if not args.run_id and not args.session:
        ap.error("give a run id or --session")

    pdir = projects_dir()
    if not pdir.is_dir():
        sys.exit(f"No transcripts at {pdir}. Set CLAUDE_PROJECTS_DIR.")
    sessions = find_sessions(pdir, args.run_id, args.session)
    sessions = [f for f in sessions if not any(f.stem.startswith(p) for p in args.exclude)]
    if not sessions:
        sys.exit(f"No session in {pdir} mentions .runs/{args.run_id}")

    total = 0
    print("## Sessions and agents\n")
    print("| Session / agent | Model | Turns | Cache-read M | Output k | Start | End | Wall |")
    print("|---|---|---:|---:|---:|---|---|---|")
    for f in sessions:
        s = summarize(f)
        total += s["cache_read"]
        print(
            f"| **{f.stem[:8]}** | {','.join(sorted(s['models']))} | {s['turns']} | {s['cache_read'] / 1e6:.1f} "
            f"| {s['output'] / 1e3:.0f} | {hm(s['first'])} | {hm(s['last'])} | {dur(s['first'], s['last'])} |"
        )
        subs = []
        for a in sorted((f.parent / f.stem / "subagents").glob("*.jsonl")):
            kind, desc = agent_label(a.with_suffix(".meta.json"))
            subs.append((summarize(a), kind, desc))
        for sa, kind, desc in sorted(subs, key=lambda x: x[0]["first"] or datetime.max.astimezone()):
            total += sa["cache_read"]
            name = f"{kind}: {desc}" if desc else kind
            print(
                f"| &nbsp;&nbsp;{name[:48]} | {','.join(sorted(sa['models']))} | {sa['turns']} "
                f"| {sa['cache_read'] / 1e6:.1f} | {sa['output'] / 1e3:.0f} | {hm(sa['first'])} | {hm(sa['last'])} "
                f"| {dur(sa['first'], sa['last'])} |"
            )
    print(f"\nTotal cache-read: {total / 1e6:.1f} M tokens")

    for f in sessions:
        users, questions, pushes = session_events(f)
        print(f"\n## Session {f.stem[:8]}")
        kinds = {}
        for _, k, _ in users:
            kinds[k] = kinds.get(k, 0) + 1
        print("User inputs: " + (", ".join(f"{k} x{n}" for k, n in sorted(kinds.items())) or "none"))
        for t, k, txt in users:
            if k == "message":
                print(f"- {hm(t)} {txt}")
        for t0, t1, header, q, answer in questions:
            print(f"- QUESTION {hm(t0)} [{header}] waited {dur(t0, t1)}, answered {hm(t1)}: {answer[:120]}")
            print(f"    {q[:200]}")
        for t0, message, result in pushes:
            print(f"- PUSH {hm(t0)} {result[:60]!r}: {message[:100]}")

    if args.run_id:
        ledger(args.run_id, args.gap)


if __name__ == "__main__":
    main()
