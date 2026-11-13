"""Unattended runs from the command line (no browser, no questions):

    python -m app run ABC-1234 [ABC-1235 ...] [options]
    TicketAnalyzer.exe run ABC-1234 --log D:\\logs\\main.log --source D:\\src\\project --publish

Every ticket runs in Skip mode: the AI picks the log attachments, the time window and the log's
clock; a step that cannot be done is skipped and the analysis continues with what is available.
The exit code is 0 when every ticket produced its report, 1 otherwise. See docs/automation.md.
"""
import argparse
import json
import queue
import sys
import time

from . import events, paths, pipeline, settings
from .ai.m365 import worker as m365_worker

REQUIRED = [step for step in pipeline.ORDER if step not in pipeline.CONTINUABLE_ERROR_STEPS]


def _parser():
    p = argparse.ArgumentParser(prog="python -m app run", description="Analyze Jira tickets without the web UI.")
    p.add_argument("issues", nargs="+", metavar="ISSUE", help="Jira key or link (several allowed; they run one after another)")
    p.add_argument("--log", action="append", default=[], metavar="PATH", help="log file or archive to analyze (repeatable)")
    p.add_argument("--source", default="", metavar="DIR", help="source code folder (LogSleuth catalog + Code Graph)")
    p.add_argument("--no-attachments", action="store_true", help="do not download the ticket's own log attachments")
    p.add_argument("--no-graph", action="store_true", help="skip the Code Graph step")
    p.add_argument("--no-infographic", action="store_true", help="skip the infographic step")
    publish = p.add_mutually_exclusive_group()
    publish.add_argument("--publish", dest="publish", action="store_true", default=None, help="publish the report to Collab")
    publish.add_argument("--no-publish", dest="publish", action="store_false", help="stop with the report ready, unpublished")
    p.add_argument("--until", choices=pipeline.ORDER, metavar="STEP",
                   help="stop after this step: " + " | ".join(pipeline.ORDER))
    p.add_argument("--shift", type=float, metavar="HOURS",
                   help="log clock minus ticket clock in hours (e.g. -9 when the ticket is KST and the log UTC); default: detect")
    p.add_argument("--ai", choices=("m365", "copilot"), help="AI backend (saved as the app's setting)")
    p.add_argument("--json", action="store_true", help="print one JSON summary on stdout; progress goes to stderr")
    p.add_argument("--quiet", action="store_true", help="no progress lines")
    return p


def _summary(run, until):
    steps = {step: run["steps"][step]["status"] for step in pipeline.ORDER}
    errors = {step: run["steps"][step]["error"] for step in pipeline.ORDER if run["steps"][step]["error"]}
    folder = pipeline.run_dir(run["id"])
    target = until or "collab"
    failed = [step for step in REQUIRED if steps[step] == "error"]
    ok = not failed and steps[target] in ("done", "waiting", "skipped")
    files = {name: str(folder / name) for name in ("05-analysis.md", "06-report.xhtml", "06-report.preview.html")
             if (folder / name).exists()}
    return {"issue": run["issue"], "run": run["id"], "ok": ok, "folder": str(folder), "steps": steps, "errors": errors,
            "skipped_or_failed_optional": [s for s in pipeline.CONTINUABLE_ERROR_STEPS if steps[s] == "error"],
            "published": steps["collab"] == "done", "collab_url": (run.get("collab") or {}).get("url", ""),
            "files": files, "tokens": run.get("usage_total") or {}}


def _drain(q, run_id, out):
    while True:
        try:
            message = json.loads(q.get_nowait())
        except queue.Empty:
            return
        if out and message.get("type") == "log" and message.get("run_id") == run_id:
            print(message["line"], file=out, flush=True)


def main(argv=None):
    args = _parser().parse_args(argv)
    paths.ensure_dirs()
    if args.ai:
        settings.save({"ai_mode": args.ai})
    progress = None if args.quiet else (sys.stderr if args.json else sys.stdout)
    q = events.subscribe()
    results = []
    try:
        for issue in args.issues:
            try:
                run = pipeline.create(
                    issue, args.log, args.source, auto_attachments=not args.no_attachments, gate="skip",
                    use_codegraph=False if args.no_graph else None, use_infographic=False if args.no_infographic else None,
                    auto_publish_to_collab=args.publish,
                    log_shift_minutes=None if args.shift is None else round(args.shift * 60))
            except ValueError as exc:
                results.append({"issue": issue, "ok": False, "errors": {"input": str(exc)}})
                print(f"✗ {issue}: {exc}", file=sys.stderr)
                continue
            if progress:
                print(f"=== {run['issue']} → {pipeline.run_dir(run['id'])}", file=progress, flush=True)
            pipeline.executor.start(run["id"], until=args.until)
            while pipeline.executor.busy:
                _drain(q, run["id"], progress)
                time.sleep(0.2)
            _drain(q, run["id"], progress)
            results.append(_summary(pipeline.get(run["id"]), args.until))
    except KeyboardInterrupt:
        pipeline.executor.stop()
        print("Stopped.", file=sys.stderr)
        return 130
    finally:
        events.unsubscribe(q)
        m365_worker.shutdown()
    if args.json:
        print(json.dumps(results if len(results) > 1 else results[0], ensure_ascii=False, indent=2))
    else:
        for r in results:
            if "steps" not in r:
                continue
            done = sum(1 for s in r["steps"].values() if s == "done")
            where = r["collab_url"] or r["files"].get("06-report.preview.html") or r["files"].get("05-analysis.md") or r["folder"]
            mark = "✓" if r["ok"] else "✗"
            print(f"{mark} {r['issue']}: {done}/{len(r['steps'])} steps done"
                  + (", published" if r["published"] else "") + f" → {where}")
            for step, error in r["errors"].items():
                print(f"    {step}: {error}")
            if r["tokens"].get("total_tokens"):
                print(f"    tokens: {r['tokens']['total_tokens']:,}" + (" (estimated)" if r["tokens"].get("estimated") else ""))
    return 0 if results and all(r["ok"] for r in results) else 1
