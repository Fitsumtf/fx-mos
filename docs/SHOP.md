# FX MOS for an auto service shop

Ten bays, eight technicians, eighty vehicles a day.

The engine is the same as the assembly line, configured as a `PARALLEL` layout.
The bays do not form a sequence — a car goes into bay 4 and comes out of bay 4 —
so there is only one gate that matters: **may this vehicle go back to its owner.**

---

## What actually changes for a shop

| | Line | Shop |
|---|---|---|
| Resources | Stations in order | Bays in parallel, each with capabilities |
| Identity | VIN allocated by the plant | Repair order number; the car arrives with its own VIN |
| Routing | One flow for the line | One plan per service type |
| The gate | May it move downstream | May it be released to the customer |
| Bay choice | Fixed by sequence | Any bay with the right equipment |

A tyre job needs a `TYRE_MACHINE`. If the service advisor puts it in a quick
lube bay, the system refuses and says why. An alignment needs an
`ALIGNMENT_RACK`. The shop's plans are validated against the equipment it
actually owns, so a plan that needs a dyno is rejected at authoring time rather
than discovered by a technician at 4pm.

---

## Run it

```bash
python -m fx_mos.simulator_shop --vehicles 80 --reset
uvicorn fx_mos.api.main:app --reload
# http://localhost:8000  →  the shop board is at /api/dashboard?line_code=SVC-1
```

---

## The twelve-minute demo

Do it in this order. The last step is the one that sells.

### 1. Ask the question first, before showing anything

> When a customer comes back angry saying you didn't tighten their wheels, or
> that you never changed their oil — what do you show them?

Let him answer. Whatever he says is the frame for everything after.

### 2. Show a normal job going through

Assign a tyre job to BAY-03. Run the steps. Release the car. Thirty seconds.
The point is that it does not get in the way — three screens, no typing beyond
the numbers the tech already reads off the tools.

### 3. Under-torque a wheel on purpose

Record the rear right at 62 Nm against a 100–130 window.

The car locks in the bay. A non-conformance opens automatically. The release
gate returns, in plain English:

```
OUT_OF_SPEC   Wheel torque rear right recorded 62Nm against 100–130.
NC_OPEN       NC-000001: Torque wheel nuts to specification failed at BAY-03
```

**Say this out loud:** that car cannot be handed back until somebody with
authority signs off a reason. Not the tech who did the work — a foreman, with
their name on it and a written resolution.

### 4. Fix it and release

Re-torque to 118 Nm, disposition the hold as rework, release. The car goes out.

Then open the service record and show him that **the 62 Nm reading is still
there**. The failure never disappears; it sits in the permanent record next to
the correction. That is the difference between a system that records reality and
one that records what everybody wishes had happened.

### 5. The service record — this is the close

```
RO: RO-20260827-0001 | COMPLETE | plan TYRSVC-PLAN v1

Fitted:
  TYRE-225-45R17   DOT-3024-1849   Meridian Parts
  TYRE-225-45R17   DOT-0825-8685   Meridian Parts
  TYRE-225-45R17   DOT-2523-5085   Nordfilter
  TYRE-225-45R17   DOT-4724-7687   Castoline

Measured:
  [LOCK] Wheel torque front left     106.4 Nm
  [LOCK] Wheel torque front right   117.51 Nm
  [LOCK] Wheel torque rear left     112.24 Nm
  [LOCK] Wheel torque rear right    115.11 Nm
  [LOCK] Cold pressure all round     36.52 psi

Who did what:
  BAY-03  TYR-010  COMPLETE  tech01
  BAY-03  TYR-020  COMPLETE  tech08
  BAY-03  TYR-030  COMPLETE  tech04
```

**Say:** this is what you print and hand across the counter. Four torque values,
four DOT codes, three technicians, timestamped. The conversation is over in
ninety seconds instead of ending with you eating a claim.

### 6. The recall query

Type in one DOT code. Get back every vehicle that has a tyre from that batch.

> If a manufacturer recalls a production batch tomorrow, how do you find which
> of your customers have those tyres?

At eighty cars a day he has no answer to this. That is the second sale.

---

## What to charge him

Do not quote a monthly figure in the first conversation. Ask two things instead:

1. **How many comebacks a month, and what does an average one cost you?**
   Redo labour, the part, and the goodwill discount. Most shops land somewhere
   between a few hundred and a few thousand a month and have never added it up.
2. **What did your last liability claim or insurance excess cost?**

Price against that number, not against what software "should" cost. If he loses
$800 a month to comebacks, a few hundred a month is an easy yes. If he has never
had a claim and never will, he is not your customer and you should say so.

**Do the install yourself, free, for him.** He is your first reference and a
friend. What you want in exchange is written permission to use his numbers:
*"eighty vehicles a day, comebacks down X%, recall containment from a day to a
minute."* That one page is worth more than his subscription.

---

## Before it touches a real customer's car

This is a working system, not a certified one. Be straight with him about all
of it — he is trusting you with his liability.

- **The spec limits in `seed_shop.py` are illustrative.** Wheel torque, oil
  quantity and disc thickness vary by vehicle. Real limits must come from the
  manufacturer's data for each model. Wrong limits are worse than no system.
- **There is no authentication yet.** Anyone with the URL can record a torque
  value or close a hold. A shop needs technician logins and a foreman role
  before this record has any evidential weight — right now a defence lawyer
  would take it apart in one question.
- **No torque tool integration.** Values are typed in. That is still a large
  improvement on nothing, but a typed number is a claim, not a measurement.
  Bluetooth torque wrenches that report actual readings are the upgrade path,
  and the one worth charging for.
- **It is not a safety system.** It records and gates. It does not replace a
  technician checking the work.

Tell him all four before he asks. If he still wants it, you have a real
customer rather than a favour from a friend.
