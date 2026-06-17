# Cold Call Analysis — Expectations & Rubric (living doc)

This is the **standard the AI judges every call against**. The Gemini prompt
(`prompt.py`) should reflect what's written here. We update this as we learn —
nothing is "ad hoc"; decisions get noted below.

> Status: 🟡 draft — sections marked **[NEEDS INPUT]** are waiting on Enout's
> definition. Until filled, analysis uses generic sales best-practice.

---

## Decisions (locked)

| Date | Decision |
|------|----------|
| 2026-06-17 | **All feedback in English.** Coaching/suggestions/summaries are written in English even though calls are Hinglish. Only verbatim quotes (`objection`, `rep_response`) stay in the original spoken language. |
| 2026-06-17 | Transcription + analysis both run on Gemini (Hugging Face dropped — it no longer serves Whisper). |
| 2026-06-17 | Direction: move from "just scores" → **pattern-based, expectation-aligned coaching**; scores become a secondary signal. *(pending final confirm)* |

---

## A. What Enout is  **[NEEDS INPUT]**
So Gemini can judge the pitch against reality, not guess.
- One-line: _what does Enout sell?_
- Top 2–3 value props: _…_
- Typical prospect (industry / role): _…_

## B. What a "great Enout cold call" looks like  **[NEEDS INPUT]**
This is the expectation feedback is written against.
- Opening / hook: _…_
- Discovery — must-ask questions: _…_
- Objection handling — how we want reps to react: _…_
- Success / next step that counts: _…_

## C. Common patterns to flag  **[NEEDS INPUT]**
Behaviours we care about across a rep's calls, e.g.:
- pitched before discovering the need
- gave up after the first objection
- talked too much / didn't listen
- didn't ask for a specific next step
- recurring objections we keep losing on
- _add your own…_

## D. Scoring
Keep a light score as a **secondary** signal, lead with patterns + actionable
coaching? Or drop scores entirely? → _decision pending_

---

## Rubric (strawman — edit me)

| Parameter | Max | High score (what we want) | Low score (what's wrong) |
|-----------|-----|---------------------------|--------------------------|
| Hook | 25 | Clear context + relevance in first 20s; earns the right to continue | Generic intro; prospect confused / uninterested |
| Objection handling | 35 | acknowledge → reframe → evidence → close | ignored / weakly deflected / argued |
| Enout pitch | 25 | Problem → Solution → Outcome, specific to prospect, concise | monologue, generic, disconnected from context |
| Discovery booked | 15 | specific date/time fixed | "we'll talk later" / no next step |

---

## How feedback should read (per call)
Not just a number — for each weak area state: **what happened → why it falls
short of the expectation above → the exact line/move to use next time** (English).

## Per-BD email
Lead with **patterns across that day's calls** (recurring misses), then 2–3
"do this instead" examples. Score shown small, as context — not the headline.
