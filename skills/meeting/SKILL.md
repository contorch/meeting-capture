---
name: meeting
description: In-meeting copilot — while a live meeting is being captured (contorch / meeting-capture in MEETING_CAPTURE_MODE=live), read the live transcript and help the user respond using their past-meeting memory. Use when the user types /meeting, asks "what did they just ask", "help me answer that", "what did we decide about X" during a call, or wants Claude to watch the meeting. Pairs with `/loop 15s /meeting` for continuous watching.
---

# In-meeting copilot

A live meeting is being transcribed to a feed by meeting-capture (live mode). Your
job is to be the silent copilot: surface what the user needs to answer the other
side **right now**, grounded in their memory — never invented.

The feed reader (finalized utterances only; "them" = the other side, "me" = the user):

```
~/.claude/skills/meeting/bin/feed recent [N]   # last N lines of the conversation
~/.claude/skills/meeting/bin/feed question      # the most recent question from "them"
~/.claude/skills/meeting/bin/feed since <ts>    # new "them" lines after a unix ts (loop mode)
~/.claude/skills/meeting/bin/feed status        # feed path + counts
```

## When invoked on-demand (`/meeting`, "help me answer that", "what did they ask?")

1. Run `feed recent 12` to see the live conversation. If the user named a specific
   question, use that; otherwise take the latest unanswered question from "them"
   (`feed question`).
2. Search the user's memory for the answer with **`mcp__context-orchestrator__search`**
   — this covers all past meetings, notes, tasks, and repo knowledge, not just the
   current call. Use the question's key nouns as the query.
3. Reply with a **whisper**: one or two short sentences the user can act on — the
   decision, number, name, or commitment — and cite where it came from (the meeting
   or source). Lead with the answer. Example:
   > 💡 Rate limit is 50 req/s, hard cap enforced on their side. *(client call, Jun 12)*
4. If memory has nothing genuinely useful, say so in one line — do not guess or
   pad. Silence beats a wrong whisper in a live call.

Keep it tight. The user is mid-conversation and reading fast.

## When run under `/loop` (continuous watching)

- Track the timestamp of the last "them" line you acted on (the loop carries state
  across ticks via your prompt). Each tick: `feed since <last_ts>` for new questions.
- For each genuinely answerable new question, emit one whisper (steps 2–3 above).
  For everything else, stay silent — most turns should produce no output.
- Never repeat a whisper for a question you already answered.

## Notes

- This is the Claude-brained, full-memory version of the standalone
  `meeting-capture copilot` (which uses Gemini + transcript keyword search). Prefer
  this inside a Claude Code session; the standalone is for non-Claude-Code users.
- If `feed status` shows no feed or the daemon isn't running in live mode, tell the
  user to start it: `MEETING_CAPTURE_MODE=live meeting-capture run`.
- Ground every claim in `search` results or the live transcript. Do not use general
  knowledge to answer meeting-specific questions.
