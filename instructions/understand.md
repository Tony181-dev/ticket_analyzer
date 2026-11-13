# Ticket Triage: incident time, time zone and which logs to fetch

Read **only the attached Jira ticket** (`01-jira.md`) and, when supplied, the attachment list
(`01-attachments.md`). Do not inspect log contents or source code and do not invent anything:
every value you return must be traceable to the ticket text, its comments or the attachment list.

You decide three things for the log processor. When the ticket does not let you decide one of
them, say so with `null` / an empty list — never guess to fill a field.

## 1. When did it happen (`time_start`, `time_end`, `incident_date`)

Find the clock time or time range the ticket ties to the reported problem (a reproduction time in
the description, a "happened at 14:30" in a comment, a time visible in a pasted log excerpt).

- One precise time: return a window from about 2 minutes before to 1 minute after it.
- An explicit interval: use it, with a small margin when its endpoints are point times.
- Several occurrences: use the one the ticket describes in most detail; mention the others in `reason`.
- A date without a time, the ticket's created/updated time, a build time or an unrelated time is
  **not** an incident time. If nothing usable exists, set both `time_start` and `time_end` to `null`.
- Write the time **exactly as the ticket states it**. Do not convert between time zones; the
  application aligns the window to the log's clock later, when it can see the log files.
- `incident_date` is the calendar date of the incident as `YYYY-MM-DD` when the ticket states it,
  else `null`.

## 2. Which clock is that (`ticket_utc_offset`)

Give the UTC offset of the time you extracted, as `"+09:00"` / `"-05:00"` / `"+00:00"`:

- the ticket names a zone or offset next to the time ("14:30 KST", "05:30 UTC", "UTC+7");
- or the time was copied from a pasted log line / tool output whose zone is stated.

If the ticket gives no zone, return `null`. Do not assume the reporter's country, the server's
zone or the offset of Jira's own timestamps.

## 3. Which attachments are worth downloading (`attachments`)

Only when `01-attachments.md` is supplied. Pick the files that can contain the log evidence for
**this** incident; list them most useful first, each with a short reason.

- Prefer logs and log archives: names with `log`, `logcat`, `dlt`, `dmesg`, `journal`, `syslog`,
  `trace`, `dump`, `bugreport`, extensions like `.log .txt .dlt .gz .zip .7z .tar .tgz`, rotated
  files (`app.log.1`). A split archive (`x.7z.001`, `.002`, …) counts as one item: list its first part.
- Use the ticket text: a file the reporter points at ("see attached main.log") comes first.
- Use upload time: when several log sets exist, prefer the one uploaded closest after the
  incident; for re-uploads with the same name prefer the newest.
- Skip screenshots, videos, documents, binaries and files clearly about something else.
- Respect the download limit stated at the top of `01-attachments.md`: stop adding files once the
  listed sizes would exceed it, keeping the most relevant ones.
- If no attachment looks like a log for this incident, return an empty list `[]`.

## 4. Search hints (`keywords`, `services`)

`keywords`: up to 10 distinctive strings copied from the ticket that should appear in the log
(error codes, message fragments, identifiers). `services`: component/module names the ticket
itself names. Do not add names from outside the ticket.

## Output

Write a few lines explaining what you found (time, zone, chosen files). Then end your answer with
exactly one fenced JSON block:

```json
{
  "schema_version": 2,
  "time_start": "HH:MM:SS[.mmm] or null",
  "time_end": "HH:MM:SS[.mmm] or null",
  "incident_date": "YYYY-MM-DD or null",
  "ticket_utc_offset": "+HH:MM or null",
  "confidence": "high | medium | low | none",
  "reason": "one sentence citing the ticket's time reference, or stating that none was found",
  "keywords": [],
  "services": [],
  "attachments": [{"filename": "exact name from 01-attachments.md", "reason": "why this file"}]
}
```

Use real JSON `null` (not the string "null"). Hours 00-23, minutes/seconds 00-59; keep fractional
seconds only when the ticket has them. Windows crossing midnight are allowed. Omit the
`attachments` key entirely when `01-attachments.md` was not supplied. The application validates
this output; an invalid clock value disables the time window (the full log range is then used).
