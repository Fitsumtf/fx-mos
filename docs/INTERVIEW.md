# Interview brief — FX MOS

Read this once. Do not memorise it. The strongest thing you have is that you
built a system, ran it, and it broke in two interesting ways.

---

## The 60-second opener

> I built a Manufacturing Operating System core — production control,
> traceability, interlocking and OEE — for a five-zone assembly line:
> sub-frame, pre-marriage, marriage, post-marriage, end of line. It's about
> 2,500 lines of Python with a REST API and a floor display, 28 tests. It's not
> a mockup; it runs, and I found two real bugs by running it that I wouldn't
> have found by reading the code.

Then stop. Let them ask which bugs. That's the whole play.

---

## The two bugs — your best material

### 1. Rework was a one-way door

**Symptom.** Simulation of 30 units: 1 signed off, 28 stuck, line plugged.

**Cause.** The gate checked *every* out-of-spec measurement in the unit's
history. A unit that failed a torque check, went through rework, and passed the
retest was still blocked — by its own past. And because each station has
capacity 1, everything behind it backed up.

**Fix.** Gate on the *latest* value per check per station. The failure stays in
the birth certificate permanently, because the audit record must never be
rewritten. But the interlock reflects current state, not history.

**After.** 28 signed off, 2 scrapped. A realistic first-pass yield.

**Why it matters, and say this part:** the distinction between *the audit record*
and *the current state* is the whole difference between a system a plant will
trust and one they'll bypass with a spreadsheet. If rework can't clear the gate,
operators will find a way around your MOS by the second shift, and then you've
lost the traceability you built it for.

There's a named regression test: `test_a_reworked_measurement_no_longer_blocks_the_gate`.

### 2. OEE blamed the wrong thing

**Symptom.** A station that ran for 45 minutes and produced nothing was
reported as having a *speed* loss.

**Cause.** `speed_loss = run_time − ideal_cycle × count`. With count zero,
the entire run time was attributed to slow cycles.

**Fix.** Speed loss is only attributed when there is throughput to compare
against. Zero throughput is an availability or quality problem.

**Why it matters:** OEE is a maintenance-budget number. Misattributing loss
sends a reliability engineer to retune a robot that was never the problem. The
number alone is nearly useless — which is why every result here carries the
loss breakdown and names the largest single bucket.

---

## Design decisions they will probe

**Why are released flows immutable?**
Because a customer will ask, two years later, what specification was in force
when their unit was built. You clone to a draft, validate, release — which
archives the prior version. Units keep the version they started under. If you
let people edit a live routing, your build records become unfalsifiable.

**Why does the gate return reasons instead of true/false?**
Two audiences. The operator needs to know what to fix. The andon board and the
ERP need to act on it programmatically. So every blocker carries a machine code
(`OUT_OF_SPEC`, `PART_MISSING`, `NC_OPEN`, `STATION_FULL`) *and* a line of
plain English.

**Why an outbox instead of calling the ERP directly?**
Because the ERP will be down and the line will not stop for it. Events are
written to a table in the same transaction as the process data, and a worker
drains it. Inbound orders are idempotent on the order id — retries are normal,
duplicate VINs are not.

**Why is line OEE the bottleneck, not the average?**
A line runs at the pace of its worst station. Averaging hides the constraint,
which is the only number that tells you where to spend money.

**Why quality is counted first-pass.**
A unit reworked to good still counts as a reject at the station that made it.
Otherwise rework becomes invisible and you optimise the wrong thing.

---

## Questions worth asking them

These signal that you've thought about the operational reality, not just the code.

1. How do you handle a routing change for units already in flight? Do they
   finish on the old version or get pulled onto the new one?
2. What's your device layer — OPC-UA, MQTT, vendor SDKs? How much of the
   integration cost is the gateway rather than the MOS?
3. When an operator can't clear an interlock, what's the escape path? Every
   plant has one; the question is whether it's designed or improvised.
4. How do you scope containment when a supplier lot goes bad — is genealogy
   queryable in minutes or is it a data-warehouse job?
5. What's your first-pass yield definition, and does rework count against it?

---

## If C3 AI comes up

Be accurate about the overlap, and don't overclaim. C3 sits mostly *above* this
layer — predictive maintenance, supply-chain and reliability models built on
data that a system like this produces. The honest framing:

> The MOS is the system of record for what physically happened. The AI layer is
> only as good as that record. I built the record layer because that's where
> the data quality problem actually lives — if your torque values aren't tied
> to the right VIN and the right spec version, no model on top of it is
> trustworthy.

That's a genuinely strong position for someone doing an MSc in AI/ML, and it
avoids pretending you've built something you haven't.

---

## Things to *not* say

- **Don't claim this is how Tesla does it.** Your source material is journalism
  and Medium posts, not documentation. Say it's a clean-room MES built on
  ISA-95 concepts. That's stronger *and* it's true — and any engineer who has
  worked in the space will know the difference immediately.
- **Don't mention what a previous employer wasn't using.** It reads as a jab,
  not a differentiator, and interviewers assume you'll one day say it about them.
- **Don't oversell scope.** Read the "Scope and honest limits" section of the
  README and say those limits out loud before they find them. No device layer,
  no auth, no electronic signature, no scheduler, not a safety system.
  Volunteering your gaps is the single most credible thing a senior engineer
  can do in an interview, and it's the opposite of what a nervous candidate does.

---

## The demo, if you get to show it

Five minutes, in this order:

1. `python -m fx_mos.simulator --units 30 --defect-rate 0.07 --reset` — talk
   over it while it runs.
2. Start the server, open the board. Point at a **held** carrier — the hazard
   stripes — and read out the blocker.
3. Show the **build record** panel: serial, components with lot numbers, every
   measurement with its spec window, the failed attempt still visible.
4. Deploy a new step at EOL-10 via the flow API and release it. That's the
   workflow you know from the floor.
5. Try to release a step that routes backwards. Show the validator refusing it
   in plain English.

If the demo breaks, say what you'd check first. That's also a good answer.

---

Last thing: you have a PhD, twenty years on real production lines, and you
built this. The floor experience is the part most candidates don't have and
can't fake. Lead with the bugs, be exact about the limits, and let the rest sit.
