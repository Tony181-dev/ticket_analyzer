"""One call shape for both AI backends.

    ask(cfg, prompt, files, run_dir, session_id=None, small=False) -> str

* m365    : files are uploaded as attachments to the same M365 conversation
            (session_id keeps context across pipeline steps). M365 takes at
            most 3 attachments per prompt, see m365_attachments().
* copilot : files must be inside run_dir; the prompt lists them by relative
            path and the CLI agent reads them itself (read-only tools).
"""
import itertools
import shutil
from pathlib import Path

from . import copilot, stopflag
from .m365 import worker as m365_worker

M365_MAX_FILES = 3  # M365 Copilot accepts at most 3 attachments per prompt
BUNDLE_DIR = "m365-attachments"
_MARK = "===== FILE: {} ====="
_END = "===== END FILE: {} ====="


def _is_text(file):
    if Path(file).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
        return False  # an image need not contain a NUL in its first 8 KB
    try:
        with open(file, "rb") as handle:
            return b"\0" not in handle.read(8192)
    except OSError:
        return False


def _size(file):
    try:
        return Path(file).stat().st_size
    except OSError:
        return 0


def _split(files, parts):
    """Split `files` in order into `parts` contiguous groups with the smallest largest group (by bytes)."""
    sizes = [_size(f) for f in files]
    best = None
    for cuts in itertools.combinations(range(1, len(files)), parts - 1):
        bounds = (0, *cuts, len(files))
        groups = [files[a:b] for a, b in zip(bounds, bounds[1:])]
        worst = max(sum(sizes[a:b]) for a, b in zip(bounds, bounds[1:]))
        if best is None or worst < best[0]:
            best = (worst, groups)
    return best[1]


def m365_attachments(files, work_dir):
    """Fit the files into M365's limit of 3 attachments per prompt without dropping any evidence.

    Returns (files to attach, note for the prompt). Up to 3 files go as they are. Beyond that the
    instruction (always the last file of a pipeline call) and any non-text file stay files of their own,
    and the text evidence is combined, in order and size-balanced, into bundle files where each original
    file starts with "===== FILE: <name> =====". The note tells the model which file went where."""
    files = [Path(f) for f in files]
    if len(files) <= M365_MAX_FILES:
        return files, ""
    head, last = files[:-1], files[-1]
    alone = [f for f in head if not _is_text(f)]
    text = [f for f in head if f not in alone]
    slots = M365_MAX_FILES - 1 - len(alone)
    if slots < 0 or (slots == 0 and text):
        raise RuntimeError(f"M365 Copilot accepts at most {M365_MAX_FILES} attachments per prompt, and the "
                           f"instruction plus {len(alone)} non-text file(s) cannot share one: "
                           + ", ".join(f.name for f in files))
    folder = Path(work_dir) / BUNDLE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    attached, lines = [(head.index(f), f) for f in alone], []
    groups = _split(text, min(slots, len(text))) if text else []
    for group in groups:
        if len(group) == 1:
            attached.append((head.index(group[0]), group[0]))
            continue
        target = folder / f"attachments-{len(lines) + 1}.md"
        names = [f.name for f in group]
        body = [f"Combined attachment: {', '.join(names)}.",
                f"Each original file starts with the line '{_MARK.format('<name>')}' and ends with "
                f"'{_END.format('<name>')}'. Read each part as that file.", ""]
        for f in group:
            body += [_MARK.format(f.name), f.read_text(encoding="utf-8", errors="replace").rstrip("\n"),
                     _END.format(f.name), ""]
        target.write_text("\n".join(body), encoding="utf-8")
        attached.append((head.index(group[0]), target))
        lines.append(f"{target.name} = {', '.join(names)}")
    out = [f for _, f in sorted(attached, key=lambda item: item[0])] + [last]
    note = (f"(M365 Copilot accepts at most {M365_MAX_FILES} attachments per prompt, so some files are combined: "
            + "; ".join(lines) + f". Inside a combined file each original file starts with '{_MARK.format('<name>')}' "
            "- treat that part as the attached file of that name.)")
    return out, note


def _inside(run_dir, file):
    run_dir, file = Path(run_dir).resolve(), Path(file).resolve()
    try:
        return file.relative_to(run_dir).as_posix()
    except ValueError:
        target = run_dir / "ctx" / file.name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(file, target)
        return target.relative_to(run_dir).as_posix()


def ask(cfg, prompt, files, run_dir, session_id=None, small=False, effort=None, context=None, on_usage=None):
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
        return copilot.ask(text, run_dir, model or "", cancellable=True, effort=effort, context=context,
                   on_usage=on_usage)
    files, note = m365_attachments(files, run_dir)
    return m365_worker.chat(
        prompt + ("\n\n" + note if note else ""), files, session_id=session_id,
        headless=cfg.get("m365_headless", True), model=cfg.get("m365_model"),
    )


def ask_image(cfg, prompt, files, image_dir, session_id=None):
    """M365 only: ask for an image in the existing conversation; returns the saved image paths."""
    files, note = m365_attachments([Path(f) for f in files or [] if f], image_dir)
    m365_worker.chat(prompt + ("\n\n" + note if note else ""), files, session_id=session_id,
                     headless=cfg.get("m365_headless", True), model=cfg.get("m365_model"), image_dir=image_dir)
    return list(m365_worker.images)


def reset():
    stopflag.requested.clear()


def cancel():
    stopflag.requested.set()
    copilot.cancel()
    m365_worker.kill()
