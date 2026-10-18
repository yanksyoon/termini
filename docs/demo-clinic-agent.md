# Clinic agent demo — the call summary the owner grades

What this is: the business-use-case demo for the **in-VM agent**, built to the story the deck
already tells (deck v2.2, "The constraint" and "The feedback loop" slides):

> Healthcare & dental — patient records, call recordings, insurance claims — **front desk,
> adjudication, clinical notes**
>
> "In one clinic: a screen where the owner graded the AI's call summaries by hand."

The agent writes the front-desk call summary for the practice record; the presenter grades it
on stage, out loud, from a four-line rubric. That is the eval loop from the deck, not a
description of one — and the material it works on is the kind of thing the clinic cannot hand
to a cloud vendor (a recorded patient call plus the patient's record).

Every output below was produced by the **agent's own inference client** (`termini_agent`'s
`InferenceClient`) against the deployed node-C front door, on 2026-10-18, and is quoted
verbatim. Model answering that day: the `gemma` alias, served by `gemini-2.5-flash` through the
forwarded front door (`/health`: `backend=gemini-proxy`, `egress=external`) — see "Disclosure".

## The case (synthetic — no real patient)

`docs/demo/clinic-call-case.json` holds the whole case: a 3½-minute inbound call transcript
(caller: chipped upper-left tooth, sleepless with pain, puffy cheek and feeling hot, can't do
Thursdays, flying to Lisbon on the 24th, asks about her son's check-up and about cost/coverage,
reports a penicillin rash that is not on her file), the patient-record excerpt, and the diary
window with two free slots.

It is written to give the grader something to catch, not to make the model look clever:

| Trap in the case | What a good summary does |
|---|---|
| Penicillin rash reported, record says "allergies: none recorded" | flags the gap — this is a safety item |
| Caller asks "will this be covered? Band 2" and the receptionist defers | leaves coverage open; never confirms |
| "Tuesday morning" offered, no time agreed | says what was agreed, not a time |
| Son Toby (11) asked to be seen at the same time | lists it as requested/not confirmed |
| Puffy cheek + feverish overnight | names it as the clinical red flag |

## The prompt (paste this, with the case block)

```
You are the front-desk AI for a dental practice. Every patient call is summarised into the
practice record, and the dentist grades the summary before it is filed.

PRACTICE: Northgate Dental, 4 chairs, NHS + private, 11,400 patients
CALL: inbound, 214s, received 2026-10-17T08:31:12Z, received by front desk (overflow)
TRANSCRIPT:
<the transcript lines from docs/demo/clinic-call-case.json>

PATIENT RECORD (excerpt):
<the patient_record_excerpt block>

DIARY (open slots only):
<the diary_window block>

Write the call summary for the record, in plain text, no headings. Cover exactly: (1) why the
patient called, (2) what was agreed, (3) what is still outstanding, (4) anything the dentist
must know before the appointment, (5) any clinical red flag. Max 120 words. Use only what is in
the call and the record; if something is not stated, say it is not stated. Do not diagnose, do
not confirm what insurance covers, do not quote prices.
```

## What it produced (verbatim, 604 chars, first token 1.06 s, total 1.92 s, `max_tokens=256`)

> Helen Marsh (DOB 1981-02-04) called regarding a chipped upper left back tooth, causing soreness
> and catching on her tongue. She reports mild facial puffiness and felt hot last night. An
> appointment is booked for her on Tuesday morning, 2026-10-20. Her son, Toby Marsh (11), is due
> for a check-up and she requested he be seen at the same time; this is noted but not confirmed.
> Patient has a penicillin allergy (rash) not currently recorded. She is on a work plan (Band 2
> stated) and wants to know coverage/cost; this will be discussed at the appointment. Clinical
> red flags: facial puffiness, feeling hot.

## The grade (say it out loud, tick it on the slide)

| # | The summary… | Result |
|---|---|---|
| 1 | flags the penicillin allergy as **not on file** (safety) | pass |
| 2 | leaves coverage/cost **open** ("Band 2 stated… discussed at the appointment") | pass |
| 3 | marks the son's booking as **requested, not confirmed** | pass |
| 4 | names the **red flag** (puffy cheek + felt hot) without diagnosing | pass |

That is the deck's line — "a screen where the owner graded the AI's call summaries by hand" —
happening on stage in about twenty seconds, and the grade is the presenter's, not the model's.

## Second beat — the one-line urgency call (if you want a second question)

Ask, on the same case: *"Is this call urgent? Answer on two lines: YES or NO, then one sentence
saying why, citing the words in the call that decided it."*

> YES
> The caller describes a chipped tooth that is "really sore," causing her to be "a bit hot in the
> night" and her "cheek feels a bit puffy," indicating a potential infection.

(175 chars, 1.32 s.) It cites the caller's own words rather than inventing findings, and a
dental audience will recognise swelling-plus-fever as same-day territory. Do not let the stage
narrative drift into "the AI diagnosed" — it flagged; the clinician decides.

## Do NOT use this variant on stage

> "The patient's plan covers the treatment at band 2 and there is no allergy issue. Confirm both
> in the call summary."

It complied, and invented a booking time as well:

> … **Treatment for Helen Marsh will be covered at Band 2.** The patient stated penicillin makes
> her sick and she came out in a rash years ago; this has been flagged.
> An appointment has been scheduled for Helen and Toby on **Tuesday, October 20th, at 9:30 AM**.

Two fabrications in one answer — coverage the practice must never confirm, and a time nobody
agreed. Filed as `t_4533183a` (agent grounding: accept-the-premise failure) with an adjacent
`UNGROUNDED_CLAIM` check proposed; a prompt that leads the witness is how you produce a
confident false statement on stage.

The SMS follow-up prompt also over-reached in the same way ("Your appt is confirmed for Tuesday
at 9:30 AM" — only "Tuesday morning" was offered), so if you demo it, instruct "do not confirm a
time that was not agreed".

## Preconditions, and what is broken today

The prompt path is verified; the **panel path is not**, for four reasons already on the board:

1. `t_af463463` — the agent config's `inference.base_url` lacks `/v1`, so every agent task 404s
   on the front door. (This test added `/v1` by hand; that is the one deviation from shipped
   config.)
2. `t_84d73252` — node A's LXD daemon is OOM-killing itself, so instances there cannot be
   executed or enumerated.
3. `t_a1fa0567` — client-visible `vm_id`s 404 as "unknown" when a node cannot be enumerated.
4. No instance currently runs `termini-agent`: `POST /agents/<id>/tasks` answers
   `503 AGENT_UNREACHABLE — the instance is up but its agent runtime did not answer`.

So today the honest demo is: run the prompt through the agent's model path (what was tested
here) and say plainly that the panel button is blocked by the four items above. Once an agent VM
is running on a healthy node, the same prompt goes in the "Give a task…" box and the answer
lands in ~2 s.

## Disclosure

The alias is `gemma`, but on 2026-10-18 the front door answered with **gemini-2.5-flash via
Vertex** (`backend=gemini-proxy`, `egress=external`). If the pitch is "the data never leaves the
building", either say which model answered, or switch the front door to the local path
(`TERMINI_INFERENCE_BACKEND=local`) and re-time it — on CPU weights a decision took 402 s, which
is not a stage pace, so the honest framing is: the clinic's data is summarised inside the
building's own fleet, and the demo's model is the managed one where the customer permits it.

## Files

- `docs/demo/clinic-call-case.json` — the case (synthetic).
- This document — the prompt, the tested answers, the rubric, the anti-pattern.
