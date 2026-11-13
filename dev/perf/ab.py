"""Compare two revisions with dev/perf/run_perf.py, alternating them, and judge the gain against the noise.

    poetry run python dev/perf/ab.py --base origin/master fact_2x fact_13x
    poetry run python dev/perf/ab.py --base origin/master --runs 5 --out .runs/<run-id>/ab fact_13x
    poetry run python dev/perf/ab.py --base HEAD fact_2x       # A/A: measures the noise alone

The base runs from a git worktree under --out, with PYTHONPATH pointing at it. The head is
the working tree you're in, uncommitted changes included. Runs alternate base, head, base,
head..., so slow drift on the machine hits both sides alike. PERF_PAIRS, PERF_ALGORITHM,
N_THREADS and SYBASE_CONFIG pass through to run_perf.py.

Progress goes to <out>/progress.log (dev/status.sh reads it). Each run's log and results are
in <out>, and the summary is <out>/summary.md and summary.json.

The verdict for each pair, table and scenario: the change in median seconds counts only if
it's more than twice the noise, where the noise is the larger (max - min) / median of the two
sides. A gain inside that is "noise", whatever its sign.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def git(*args, cwd=REPO) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd).decode().strip()


def log(out: str, line: str):
    line = f"{datetime.now():%H:%M:%S} {line}"
    print(line, flush=True)
    with open(os.path.join(out, "progress.log"), "a", encoding="utf-8") as file:
        file.write(line + "\n")


def load(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def noise(seconds: list) -> float:
    return (max(seconds) - min(seconds)) / statistics.median(seconds) if len(seconds) > 1 else 0.0


def summarize(results: dict, base_rev: str, head_rev: str) -> tuple:
    rows = []
    for key in sorted(results["base"].keys() | results["head"].keys()):
        base, head = results["base"].get(key, []), results["head"].get(key, [])
        if not base or not head:
            continue
        base_med, head_med = statistics.median(base), statistics.median(head)
        change = (head_med - base_med) / base_med
        threshold = 2 * max(noise(base), noise(head))
        if abs(change) <= threshold:
            verdict = "noise"
        else:
            verdict = "faster" if change < 0 else "slower"
        rows.append(
            {
                "pair": key[0],
                "table": key[1],
                "scenario": key[2],
                "base_median": round(base_med, 2),
                "head_median": round(head_med, 2),
                "change_pct": round(100 * change, 1),
                "noise_pct": round(100 * max(noise(base), noise(head)), 1),
                "verdict": verdict,
                "base_runs": [round(s, 2) for s in base],
                "head_runs": [round(s, 2) for s in head],
            }
        )
    lines = [
        f"# A/B: {base_rev} (base) against {head_rev} (head)",
        "",
        "| Pair | Table | Scenario | Base s | Head s | Change | Noise | Verdict |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['pair']} | {r['table']} | {r['scenario']} | {r['base_median']} | {r['head_median']} "
            f"| {r['change_pct']:+.1f}% | {r['noise_pct']:.0f}% | {r['verdict']} |"
        )
    if rows:
        lines += ["", f"Median noise across rows: {statistics.median(r['noise_pct'] for r in rows):.0f}%."]
    return rows, "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tables", nargs="+")
    p.add_argument("--base", default="origin/master", help="Revision to compare against (default origin/master)")
    p.add_argument("--runs", type=int, default=5, help="Runs per side (default 5)")
    p.add_argument("--out", default=None, help="Output directory (default .runs/ab-<time>)")
    args = p.parse_args()

    out = os.path.abspath(args.out or os.path.join(REPO, ".runs", f"ab-{datetime.now():%Y%m%d-%H%M%S}"))
    os.makedirs(out, exist_ok=True)
    base_rev = git("rev-parse", "--short", args.base)
    head_rev = git("rev-parse", "--short", "HEAD") + (
        "+dirty" if git("status", "--porcelain", "--untracked-files=no") else ""
    )
    worktree = os.path.join(out, f"base-{base_rev}")
    if not os.path.isdir(worktree):
        git("worktree", "add", "--detach", worktree, base_rev)
    rig_file = os.path.join(REPO, ".runs", "rig.json")

    sides = {"base": worktree, "head": REPO}
    results = {"base": defaultdict(list), "head": defaultdict(list)}
    total = 2 * args.runs
    started = time.monotonic()
    failed = []
    try:
        for i in range(1, args.runs + 1):
            for side, directory in sides.items():
                n = 2 * (i - 1) + (1 if side == "base" else 2)
                elapsed = (time.monotonic() - started) / 60
                eta = f", about {elapsed / (n - 1) * (total - n + 1):.0f} min left" if n > 1 else ""
                log(out, f"[{n}/{total}] start {side} run {i} ({elapsed:.1f} min in{eta})")
                results_file = os.path.join(out, f"{side}-{i}.jsonl")
                env = dict(
                    os.environ,
                    PYTHONPATH=directory,
                    PERF_RESULTS_FILE=results_file,
                    PERF_PROGRESS_FILE=os.path.join(out, "progress.log"),
                    PERF_RIG_FILE=rig_file,
                )
                with open(os.path.join(out, f"{side}-{i}.log"), "w", encoding="utf-8") as run_log:
                    code = subprocess.call(
                        [sys.executable, os.path.join(directory, "dev", "perf", "run_perf.py"), *args.tables],
                        cwd=directory,
                        env=env,
                        stdout=run_log,
                        stderr=subprocess.STDOUT,
                    )
                log(out, f"[{n}/{total}] {side} run {i} exit {code}")
                if code != 0:
                    failed.append(f"{side} run {i}")
                for row in load(results_file):
                    key = (f"{row['source_db']}:{row['target_db']}", row["src_table"], row["target_type"])
                    results[side][key].append(row["checksum_sec"])
    finally:
        subprocess.call(["git", "worktree", "remove", "--force", worktree], cwd=REPO)
        rows, text = summarize(results, base_rev, head_rev)
        if failed:
            text += (
                f"\nFailed runs, whose correctness checks or connections failed: {', '.join(failed)}. See their logs.\n"
            )
        with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as file:
            file.write(text)
        with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as file:
            json.dump({"base": base_rev, "head": head_rev, "failed": failed, "rows": rows}, file, indent=2)
        log(out, f"done: {out}/summary.md")
        print(text)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
