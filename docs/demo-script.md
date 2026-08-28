# Demo video — narration script

Target length **3:00** (hard cap 3:30). Covers the competition's required *"one demonstrated
multi-turn session"* plus the architecture and results a judge needs to score Feasibility and
Presentation.

Placeholders in `«guillemets»` get filled from the final evaluator run before recording. The
timings are a budget, not a metronome — if a section runs long, cut from §3 (architecture), never
from §4 (the live session).

---

## Before you record

- [ ] Final `py -m evaluator.local_evaluator` run finished; note the aggregate + per-scenario numbers.
- [ ] Fill every `«placeholder»` below.
- [ ] Terminal at ~16pt or larger — judges may watch this on a laptop. Light background records
      better than dark.
- [ ] Pick the demo session in advance and confirm it converges. A session that finds the target
      around turn 3–4 tells the story best: long enough to show clarification working, short enough
      not to bore.
- [ ] Close anything with a key, token, or personal path in it. Nothing on screen should show an
      API key or `data/public_set.jsonl`'s `ground_truth` field.
- [ ] Have `docs/features/` open in a second tab for the results section.

---

## §1 — Hook (0:00 – 0:20)

**On screen:** title card — team name, "TikTok TechJam 2026 · Shopping Copilot".

> Search engines assume you already know what you want. Shopping usually isn't like that — you know
> roughly what you're after, and you figure out the rest by talking it through.
>
> We built a conversational shopping agent that does the talking part: it asks, it listens, and it
> narrows fifty thousand products down to ten.

---

## §2 — The task and how it's scored (0:20 – 0:45)

**On screen:** a single slide with the scoring formula.

> The setup is deliberately hard. There's a hidden target product in a fifty-thousand-item catalog,
> and a simulated customer who only tells us what we actually ask about. We get ten turns, and we
> only score if that exact product lands in our top ten.
>
> The score weights three things: finding it at all, ranking it near the top, and getting there in
> fewer turns. Fifty, thirty, twenty. That weighting drove every decision we made.

---

## §3 — Architecture (0:45 – 1:30)

**On screen:** architecture diagram — message in, three boxes (dialog state / retrieval routes /
ranking), ten ASINs out. Build it up box by box if you can.

> Every turn runs the same pipeline.
>
> First, **dialog state**. We pull structured constraints out of what the customer said — colour,
> material, budget «and: size, style, use-case» — and remember them across the whole conversation.
> That's what lets turn six be smarter than turn one.
>
> Second, **routing and retrieval**. If they've given us a hard constraint, we treat it as a buying
> session and filter aggressively. If they're still browsing, we cast wide instead. Either way we
> run more than one search — a keyword search across the whole catalog and a category-focused one —
> and fuse the rankings, so a strong category match can rescue a product that keyword search buried.
>
> Third, **clarification**. When the candidate pool is still too broad, we don't guess. We pick the
> one attribute that would narrow it most and ask about that specifically.
>
> «If implemented — reranking: And before we answer, we reorder the shortlist against everything
> we've learned, which is what moves a product from rank nine to rank one.»
>
> No fine-tuning, no external vector database. It runs in memory, and «it runs fully offline / it
> falls back to a fully offline path if the network is unavailable».

---

## §4 — Live multi-turn session (1:30 – 2:20)

**This is the section judges care about most. Show the real thing running, not a mock-up.**

**On screen:** terminal, running one full session. Let the turns actually appear rather than cutting
between stills.

> Here's a real session from the public set.
>
> **Turn one.** The customer says «quote the opening message verbatim». That's vague — so we're on
> the browsing track, and rather than guessing we ask about «attribute».
>
> **Turn two.** They tell us «quote what the customer reveals». Watch the state on the left: that
> constraint is now locked in, and the candidate pool drops from «N» to «N».
>
> «If the demo session is intent_override — **Turn three.** They change their mind: "actually,
> ignore my earlier preference." We don't stack that on top of the old one, we overwrite the slot it
> conflicts with. Getting this wrong is how an agent ends up searching for two contradictory things
> at once.»
>
> **Turn «N».** Target found, rank «N».
>
> «One line on why this session is representative — e.g. "and that's the common shape: two
> questions, one hit."»

---

## §5 — Results (2:20 – 2:50)

**On screen:** the score table — baseline vs. final, with the per-scenario breakdown visible.

> Against the organizer's baseline, we went from a technical score of **0.107** to **«final»** —
> «X»× the baseline.
>
> The honest version of that number: our biggest gains came from «buying / browsing» sessions. «Name
> the weakest scenario» is still our weakest, and we know why — «one-clause reason».
>
> We tracked every feature's effect on the score as we built, so we can tell you which ideas
> actually paid and which ones didn't.

---

## §6 — Close (2:50 – 3:00)

**On screen:** repo / team card.

> Ten turns, fifty thousand products, and a conversation that actually narrows things down.
>
> Thanks for watching.

---

## Notes on delivery

- **Show the score honestly.** Judges read the feasibility disclosures; a demo that quietly omits
  the weak scenario reads worse than one that names it and explains it in a clause.
- **Don't narrate the code line by line.** Judges have the repo. The video's job is the *shape* of
  the system and proof it runs.
- **Say the scoring weights once, early.** Every architectural choice reads as deliberate afterwards.
- If a section overruns, cut §3 down to the three box names. §4 is the required deliverable and §5
  is the evidence — protect both.
