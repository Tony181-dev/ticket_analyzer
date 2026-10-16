"""One call shape for both AI backends.

    ask(cfg, prompt, files, run_dir, session_id=None, small=False) -> str

* m365    : files are uploaded as attachments to the same M365 conversation
            (session_id keeps context across pipeline steps).
* copilot : files must be inside run_dir; the prompt lists them by relative
            path and the CLI agent reads them itself (read-only tools).
"""
import shutil
from pathlib import Path

from . import copilot
from .m365 import worker as m365_worker


def _inside(run_dir, file):
    run_dir, file = Path(run_dir).resolve(), Path(file).resolve()
    try:
        return file.relative_to(run_dir).as_posix()
    except ValueError:
        target = run_dir / "ctx" / file.name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(file, target)
        return target.relative_to(run_dir).as_posix()


def ask(cfg, prompt, files, run_dir, session_id=None, small=False):
    files = [Path(f) for f in files or [] if f]
    if cfg.get("ai_mode") == "copilot":
        rels = [_inside(run_dir, f) for f in files]
        listing = "\n".join(f"- {r}" for r in rels)
        text = prompt
        if rels:
            text = ("Read these files from the current directory first (read them completely):\n"
                    f"{listing}\n\n{prompt}\n\n"
                    "Reply with the requested output only. Do not describe what you read, "
                    "do not ask questions, do not modify any file.")
        model = cfg.get("refine_model") if small else cfg.get("copilot_model")
        return copilot.ask(text, run_dir, model or "")
    return m365_worker.chat(
        prompt, files, session_id=session_id,
        headless=cfg.get("m365_headless", True), model=cfg.get("m365_model"),
    )


def cancel():
    copilot.cancel()
    m365_worker.kill()
