# Ticket Understanding Instruction

You are a senior software engineer preparing to read runtime logs for a Jira issue.
You are given the Jira ticket and the clock range actually present in each log file.

1. Understand the ticket: symptom, reproduction steps, affected feature/component, build/version,
   and every time reference (reproduction time, attached screenshots/videos, comments such as
   "issue happened at 14:32", log excerpts pasted in the ticket).
2. Decide the time window in which the issue most likely happened **in the log clock**.
   - Prefer explicit times from the ticket or pasted log lines; map them onto the log clock range
     given below (logs may be in a different timezone or device clock than the reporter).
   - Add a safety margin: about 2 minutes before and 1 minute after a precise moment, or the whole
     reproduction session when only a rough time is known.
   - If the ticket gives no usable time, or the times do not fall inside any log range,
     return null for both times (the whole log will be used).
3. Pick search keywords that are likely to appear in the logs for this issue: component/tag names,
   error codes, API names, exception names, state names. 3-12 short literal strings, no regex.
4. List likely log services/tags (may be empty).

Write a short Markdown understanding first (Summary, Symptom, Component, Timeline, Unknowns).
Then end your answer with exactly one fenced JSON block:

```json
{
  "time_start": "HH:MM:SS or null",
  "time_end": "HH:MM:SS or null",
  "confidence": "high | medium | low | none",
  "reason": "one sentence explaining how the window was chosen",
  "keywords": ["..."],
  "services": ["..."]
}
```
