# Ticket Incident-Time Extraction

Read **only the attached Jira ticket**. Do not inspect logs or source code, infer a time from
ticket creation/update dates, or invent a timestamp.

Find an explicit clock time or time range that the ticket ties to when the reported issue
occurred (for example, a reproduction time in the description or a comment). The log processor
accepts a time-of-day window in the clock printed by the log, not a calendar date. Do not
convert timezones or assume the server's local time:

- For one precise incident time, return a window from about 2 minutes before to 1 minute after it.
- For an explicit incident interval, use that interval, with a small margin if its endpoints are
  point times.
- A date without a time, a ticket-edit timestamp, or an unrelated time is not usable.
- If there is no usable incident time, set **both** `time_start` and `time_end` to `null`. An
  empty window means LogSleuth processes the full time range of every supplied log file.

Write at most a few lines stating whether an explicit incident time was found and why. Then end
your answer with exactly one fenced JSON block:

```json
{
  "schema_version": 1,
  "time_basis": "log-local-clock",
  "time_start": "HH:MM:SS[.mmm] or null",
  "time_end": "HH:MM:SS[.mmm] or null",
  "confidence": "high | medium | low | none",
  "reason": "one sentence citing the ticket time reference, or stating that none was found",
  "keywords": [],
  "services": []
}
```

Use real JSON null (not the string "null") for absent times. Hours must be 00-23,
minutes/seconds 00-59. Keep fractional seconds when explicitly relevant (1-3 digits).
Windows crossing midnight are allowed. Keywords may be copied from the ticket; do not
guess service names from outside the ticket. The application validates and normalizes
this output into 02-focus.json with filter_mode = "window" or "full". If JSON or any
provided clock value is invalid, Auto uses the full catalog over the full log time range.
