# Log Clock Alignment

The ticket gives an incident time; the log files print their own clock. Decide how many minutes
must be **added to the ticket time to get the time printed in the logs** (`shift_minutes`).

You are given `03-log-span.md`: the incident window as written in the ticket, the zone the ticket
states (if any), the UTC offset of Jira's own timestamps, the first/last timestamp of every log
file, attachment upload times and a table of candidate alignments that says whether each shifted
window falls inside a log file's time range. You may also re-read the ticket (`01-jira.md`).

Reason only from that evidence:

- A candidate whose window is **not** inside any log file cannot be right.
- Timestamps that carry a zone (`Z`, `+09:00`) settle the log's clock directly.
- Device logs (logcat, DLT, dmesg, journal) usually print device-local time or UTC; server logs
  often print UTC. Treat this as a weak hint, never as proof.
- A log uploaded shortly after the incident normally *ends* shortly before its upload time. Compare
  the log's last timestamp with the upload time (Jira clock) to see which offset makes them agree.
- Whole-hour (or 30/45-minute) UTC offsets only. Do not propose an arbitrary shift to force a match.

If the evidence does not single out one alignment, return `null` — the application then keeps the
ticket time as written and falls back to the full log range when that window is empty.

End your answer with exactly one fenced JSON block:

```json
{
  "shift_minutes": "integer (e.g. -540) or null",
  "log_utc_offset": "+HH:MM or null",
  "confidence": "high | medium | low | none",
  "reason": "one or two sentences naming the evidence used"
}
```
