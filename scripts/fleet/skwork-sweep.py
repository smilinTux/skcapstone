#!/usr/bin/env python3
"""Report repositories holding work that is not durable yet.

Why: agents repeatedly do good work and stop one step short of durability. In one
night this estate had a z.ai provider sitting untracked in a live checkout, 374
lines of CapAuth rotation untracked, the lifecycle module untracked on a checkout
behind origin, and two repositories emptied on disk with every tracked file staged
as a deletion. None of it was visible until someone went looking.

Read-only. Never commits, never pushes. Reports and, with --skmail, files a
message so the gap is on the record rather than in someone's memory.
"""
import argparse, glob, json, os, subprocess, sys, datetime

def git(repo, *a):
    r = subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""

def scan(roots):
    findings = []
    for root in roots:
        for d in sorted(glob.glob(os.path.expanduser(root))):
            if not os.path.isdir(os.path.join(d, ".git")):
                continue
            name = os.path.basename(d.rstrip("/"))
            porcelain = git(d, "status", "--porcelain")
            lines = [l for l in porcelain.split("\n") if l.strip()]
            modified = [l for l in lines if l[:2].strip() in ("M", "MM", "AM")]
            untracked = [l for l in lines if l.startswith("??")]
            deleted = [l for l in lines if l[:2].strip() in ("D", "AD")]
            branch = git(d, "rev-parse", "--abbrev-ref", "HEAD")
            unpushed = git(d, "log", "--oneline", "@{u}..") if git(d, "rev-parse", "--abbrev-ref", "@{u}") else "NO_UPSTREAM"
            behind = git(d, "rev-list", "--count", "HEAD..@{u}") if unpushed != "NO_UPSTREAM" else ""
            issues = []
            # every tracked file deleted is the signature of an emptied worktree
            tracked = git(d, "ls-files")
            n_tracked = len(tracked.split("\n")) if tracked else 0
            if deleted and n_tracked and len(deleted) >= max(5, int(n_tracked * 0.8)):
                issues.append("EMPTIED_WORKTREE: %d of %d tracked files deleted" % (len(deleted), n_tracked))
            if untracked:
                issues.append("untracked=%d" % len(untracked))
            if modified:
                issues.append("modified=%d" % len(modified))
            if unpushed and unpushed != "NO_UPSTREAM":
                issues.append("unpushed=%d" % len(unpushed.split("\n")))
            if unpushed == "NO_UPSTREAM" and branch not in ("main", "master"):
                issues.append("branch has no upstream")
            if behind and behind != "0":
                issues.append("behind_upstream=%s" % behind)
            if issues:
                findings.append({
                    "repo": name, "path": d, "branch": branch, "issues": issues,
                    "untracked_sample": [l[3:] for l in untracked[:5]],
                })
    return findings

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="*", default=["~/work/*/", "~/skgateway-codex/", "~/clawd/skcapstone-repos/*/"])
    ap.add_argument("--skmail", action="store_true", help="file a message when findings exist")
    ap.add_argument("--to", default="jarvis")
    a = ap.parse_args()

    host = os.uname().nodename
    findings = scan(a.roots)
    if not findings:
        print("%s: all repositories clean" % host)
        return 0

    print("%s: %d repositories holding non-durable work" % (host, len(findings)))
    crit = []
    for f in findings:
        flag = "  !! " if any("EMPTIED" in i for i in f["issues"]) else "     "
        print("%s%-22s [%s] %s" % (flag, f["repo"], f["branch"], ", ".join(f["issues"])))
        for s in f["untracked_sample"]:
            print("            ?? %s" % s)
        if any("EMPTIED" in i for i in f["issues"]):
            crit.append(f["repo"])

    if a.skmail:
        base = os.path.expanduser("~/.skcapstone/coordination/skmail.d")
        os.makedirs(base, exist_ok=True)
        body = ["Work-durability sweep on %s found %d repositories holding work that is not"
                % (host, len(findings)),
                "durable yet. Uncommitted or unpushed work is destroyed without trace by the",
                "next pull or checkout in a shared checkout.", ""]
        for f in findings:
            body.append("%s [%s]: %s" % (f["repo"], f["branch"], ", ".join(f["issues"])))
            for s in f["untracked_sample"]:
                body.append("    untracked: %s" % s)
        if crit:
            body += ["", "CRITICAL: %s look like EMPTIED WORKTREES (nearly every tracked file"
                     % ", ".join(crit),
                     "staged as deleted). Do NOT commit those. Restore with git checkout -- . and",
                     "find out what emptied them."]
        rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "from": "skwork-sweep@%s" % host, "to": a.to,
               "re": "WORK-DURABILITY-SWEEP-%s-%d-REPOS" % (host.upper(), len(findings)),
               "priority": "critical" if crit else "normal",
               "body": "\n".join(body)}
        with open(os.path.join(base, "%s.jsonl" % a.to), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print("  filed skmail to %s" % a.to)
    return 1

if __name__ == "__main__":
    sys.exit(main())
