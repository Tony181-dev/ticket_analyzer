"""Bounded local journal of public Copilot CLI reasoning/progress snapshots."""
import json
from pathlib import Path

NAME = "ai-reasoning.jsonl"
MAX_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 600


def read(folder):
    path = Path(folder) / NAME
    if not path.is_file():
        return []
    # Reads are bounded even if an old or externally modified file grew unexpectedly.
    with path.open("rb") as stream:
        size = path.stat().st_size
        if size > MAX_BYTES:
            stream.seek(size - MAX_BYTES)
            stream.readline()
        lines = stream.read(MAX_BYTES).decode("utf-8", "replace").splitlines()
    records = {}
    for line in lines:
        try:
            event = json.loads(line)
            if isinstance(event, dict) and isinstance(event.get("id"), str):
                records[event["id"]] = event
        except ValueError:
            continue
    return list(records.values())[-MAX_EVENTS:]


def append(folder, event):
    """Caller holds the pipeline lock, serializing appends, reads and workflow resets."""
    path = Path(folder) / NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    if path.exists() and path.stat().st_size + len(line.encode("utf-8")) > MAX_BYTES:
        records = read(folder)[-MAX_EVENTS // 2:]
        temporary = path.with_suffix(".tmp")
        encoded = [(json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8") for item in records]
        budget = MAX_BYTES - len(line.encode("utf-8"))
        size = sum(map(len, encoded))
        while encoded and size > budget:
            size -= len(encoded.pop(0))
        temporary.write_bytes(b"".join(encoded))
        temporary.replace(path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line)
