#!/usr/bin/env python3
"""Commit and push an exported ODMR result bundle to GitHub.

Run this deliberately, after checking the bundle looks right -- it makes a
commit and pushes it to the remote, which is not something to trigger by
accident from the acquisition GUI.

    python publish_results.py                       # newest bundle in results/
    python publish_results.py results/20260729_1200 # a specific one
    python publish_results.py --all                 # every unpublished bundle
    python publish_results.py -m "culet centre, 2 GPa"
    python publish_results.py --dry-run             # show what would happen

Raw datacubes are deliberately not published: a sweep can produce hundreds
of megabytes, GitHub warns above 50 MB and rejects above 100 MB, and a
repository that accumulates them becomes unusable to clone. The guard below
refuses oversized files rather than letting a push fail halfway.
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time

# GitHub warns at 50 MB per file and hard-rejects at 100 MB.
WARN_BYTES = 50 * 1024 * 1024
REJECT_BYTES = 100 * 1024 * 1024

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed:\n{result.stderr.strip()}")
    return result


def current_branch() -> str:
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def check_sizes(paths: list[str]) -> list[str]:
    """Reject anything too big for GitHub; warn about anything merely large."""
    problems = []
    for path in paths:
        for root, _, files in os.walk(path):
            for name in files:
                full = os.path.join(root, name)
                size = os.path.getsize(full)
                relative = os.path.relpath(full, REPO_ROOT)
                if size >= REJECT_BYTES:
                    problems.append(
                        f"{relative} is {size / 1e6:.0f} MB — GitHub rejects files "
                        "over 100 MB. Raw datacubes belong in ODMR/data/, which is "
                        "git-ignored; publish the exported bundle instead."
                    )
                elif size >= WARN_BYTES:
                    print(
                        f"  warning: {relative} is {size / 1e6:.0f} MB — large for "
                        "version control, but under GitHub's limit."
                    )
    return problems


def push_with_retry(branch: str, attempts: int = 5) -> None:
    """Push, retrying transient network failures with exponential backoff."""
    delay = 2
    for attempt in range(1, attempts + 1):
        result = run(["git", "push", "-u", "origin", branch], check=False)
        if result.returncode == 0:
            print(result.stdout.strip() or result.stderr.strip())
            return
        print(f"  push attempt {attempt}/{attempts} failed: {result.stderr.strip()}")
        if attempt == attempts:
            raise RuntimeError("Push failed after retries. Check the network and remote.")
        time.sleep(delay)
        delay *= 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", nargs="?", default=None, help="Bundle directory (default: newest)")
    parser.add_argument("--all", action="store_true", help="Publish every bundle in results/")
    parser.add_argument("-m", "--message", default=None, help="Extra text for the commit message")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done, change nothing")
    args = parser.parse_args()

    results_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    if args.all:
        bundles = sorted(d for d in glob.glob(os.path.join(results_root, "*")) if os.path.isdir(d))
    elif args.bundle:
        bundles = [os.path.abspath(args.bundle)]
    else:
        candidates = sorted(d for d in glob.glob(os.path.join(results_root, "*")) if os.path.isdir(d))
        bundles = candidates[-1:] if candidates else []

    if not bundles:
        print("No result bundles found. Press 'Export' in the ODMR app first.")
        return 1
    for bundle in bundles:
        if not os.path.isdir(bundle):
            print(f"Not a directory: {bundle}")
            return 1

    print("Publishing:")
    for bundle in bundles:
        print(f"  {os.path.relpath(bundle, REPO_ROOT)}")

    problems = check_sizes(bundles)
    if problems:
        print("\nRefusing to publish:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    branch = current_branch()
    names = ", ".join(os.path.basename(b) for b in bundles)
    message = f"Add ODMR measurement results: {names}"
    if args.message:
        message += f"\n\n{args.message}"

    if args.dry_run:
        print(f"\n[dry run] would commit to '{branch}' with message:\n{message}")
        return 0

    run(["git", "add", "--"] + bundles)
    staged = run(["git", "diff", "--cached", "--name-only"]).stdout.strip()
    if not staged:
        print("Nothing new to commit — these bundles are already published.")
        return 0
    print(f"\nStaged {len(staged.splitlines())} file(s).")

    run(["git", "commit", "-m", message])
    print(f"Committed to '{branch}'. Pushing…")
    push_with_retry(branch)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
