# FX MOS — commercial notes

Working notes for taking this to market. Not a brochure.

---

## Who actually buys this

Not the automotive giants. They have MES already, and displacing SAP or Siemens
takes a two-year sales cycle and a reference customer you don't have yet.

The buyer is a **50–500 person contract manufacturer** who is currently running
production on paper travellers, a shared spreadsheet, and one person's memory.
They have a specific, expensive pain:

- A customer audit is coming and they cannot produce a build record.
- A supplier lot went bad and containment took three days of digging.
- They just lost a contract because they couldn't demonstrate traceability.
- Their first-pass yield is a guess.

That last group is the one that signs. **Sell the audit, not the software.**

Adjacent sectors with the same pain and better budgets: medical device
subassembly, aerospace fasteners and machined parts, battery pack assembly,
industrial electronics. All have regulatory traceability requirements that make
the spreadsheet approach untenable the moment they scale.

---

## The wedge

Do not lead with "manufacturing operating system." That word means a six-figure
project and a year of integration, and it will get you a polite no.

Lead with one line:

> When your customer asks for the build record of a specific serial number, how
> long does it take you to produce it?

If the answer is measured in hours or days, you have a conversation. The demo
that closes is `/api/units/{serial}/birth-certificate` returning in
milliseconds, with every component lot and every measurement against the spec
version in force that day.

**Land with traceability. Expand into interlocking and OEE.** Traceability is
the thing they'll pay for on day one because it's already costing them
contracts. Interlocking is a bigger behavioural change and needs trust first.

---

## Pricing shape

Per site, per year. Not per seat — factories have shift workers and per-seat
pricing makes them ration access, which kills adoption.

| Tier | Fits | Includes | Indicative |
|---|---|---|---|
| **Line** | One line, up to 12 stations | Traceability, routings, interlocking, OEE, floor display | Low four figures / month |
| **Plant** | Up to 6 lines | Above, plus ERP integration, multi-model routings, priority support | Mid four figures / month |
| **Enterprise** | Multi-site | Above, plus on-prem deployment, source escrow, SLA, named engineer | Negotiated |

Set the real numbers against your local market and your own cost to serve. The
structure matters more than the figures: annual, per site, with implementation
billed separately.

**Charge for implementation.** A fixed-fee onboarding — model their line, load
their routings, train the supervisors — is where the first real money is, it
qualifies serious buyers, and it's the work only you can do because of the
twenty years on the floor. Do not give it away to win the licence.

---

## What has to exist before the first paid install

Be honest with yourself about this list. Selling before these are done will
cost more than waiting.

1. **Authentication and roles.** Operator, supervisor, quality, engineer. An
   operator must not be able to disposition their own NC.
2. **Electronic signature on disposition and release.** Who approved it, when,
   under what credential. This is table stakes in any regulated sector.
3. **A device layer.** OPC-UA at minimum. Manual entry works for a pilot and
   nothing beyond it — the value is in capturing torque tools and testers
   automatically.
4. **Backup and restore, documented.** They will ask. It's their quality record.
5. **Product liability / professional indemnity insurance.** Before, not after.
   You are selling a system that gates the release of physical product.
6. **A lawyer's review of the LICENSE.** Section 7 especially. See the note at
   the bottom of that file.

---

## Honest competitive position

**Against SAP ME / Siemens Opcenter / Rockwell:** you lose on breadth,
certification, references and procurement comfort. You win on time to value,
price, and the fact that a small manufacturer can actually get you on the phone.
Don't compete on features — compete on being live in six weeks instead of
eighteen months.

**Against the spreadsheet:** this is the real competitor, and it's free and
already installed. You win when the cost of *not* having traceability becomes
concrete — a failed audit, a lost contract, a recall they had to scope by hand.
Time your outreach to those events.

**Against a build-it-in-house developer:** they'll underestimate it. Rework
gates, flow versioning, first-pass yield accounting and outbox reliability are
all things they'll get wrong in v1 — you know this because you got two of them
wrong yourself and fixed them. That's a fair and effective thing to say.

---

## First three moves

1. **One pilot, free or near-free, in exchange for a reference and a case
   study.** Pick a company where you already know someone. The reference is
   worth more than the fee.
2. **Write up the containment story.** "Supplier lot recall scoped from three
   days to four minutes" with real numbers from the pilot. That single page
   sells better than any feature list.
3. **Publish the repo.** Source-available with a commercial licence. Small
   manufacturers distrust black boxes inside their quality system, and a
   readable codebase with 28 tests is credibility you can't buy.

---

## A note on sequencing

You have a job interview and a business idea running at the same time, and the
two are not in conflict — but they compete for the same hours, and the business
needs *evidence*, not more features.

The fastest path to revenue is not more code. It's one manufacturer letting you
model their line. Everything on the "before first paid install" list above is
real, but it can be built *against a specific customer's requirements* rather
than speculatively. Building all of it first, alone, unpaid, is the failure
mode that eats a year.

Get one line in front of one plant manager. Then build what that plant needs.
