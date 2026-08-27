# Publishing FX MOS

Three things to do, in this order. Each is independent — do one, ship it, do the next.

---

## 1. GitHub

```bash
cd fx-mos
git init -b main
git add .
git commit -m "FX MOS: manufacturing operating system core"
git remote add origin https://github.com/<YOUR-ACCOUNT>/fx-mos.git
git push -u origin main
```

Then in the repo settings:

- **Description:** `Manufacturing Operating System core — traceability, process interlocking and OEE for discrete assembly lines. Commercial licence.`
- **Website:** `https://fitexindustrial.com`
- **Topics:** `manufacturing` `mes` `manufacturing-execution-system` `traceability` `oee` `isa-95` `industry-40` `fastapi` `python`

CI runs on the first push: the unit tests plus a smoke test that drives 20 units
through the line. If interlocks break, the smoke test catches it even when the
unit tests pass.

**Before pushing, check:** the badge URL in `README.md` points at your actual
account, and the note at the bottom of `LICENSE` is deleted.

---

## 2. Live demo

A link beats a description. Any container host with a free tier works.

```dockerfile
# Dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m fx_mos.simulator --units 25 --defect-rate 0.09 --seed 7 --reset
CMD uvicorn fx_mos.api.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

Seeding at build time means the demo opens with a line already running —
completed units, an open hold, real OEE numbers. An empty dashboard demos badly.

Two things to handle before you publish the URL:

- **It is writable by anyone.** `/api/simulate` and the disposition endpoints
  have no auth. For a public demo that is acceptable — the data is fake — but
  redeploy on a schedule so it resets.
- **Do not point it at a database you care about.** SQLite in the container,
  wiped on every deploy, is exactly right here.

---

## 3. Landing page

`docs/landing/index.html` is a single self-contained file. Drop it on
fitexindustrial.com at `/fx-mos` or similar.

Before publishing:

1. **Wire up the form.** It currently posts nowhere. There is a comment in the
   HTML with two options. A lead form that silently fails is worse than none.
2. **Fix the image path.** It points at `../img/floor-display.png`. Either keep
   that relative structure or change the `src` to wherever you host the image.
3. **Add the demo link.** Replace the "See what it does" button target with your
   live demo URL once step 2 is done.
4. **Decide on prices.** The tiers deliberately say "request a quote" rather
   than a number. That is the right call for a first sale — you do not yet know
   what the market bears, and a published number you later discount is worse
   than no number. Add figures once three quotes have been accepted or refused.

---

## What not to do yet

**Do not add a checkout button.** Nobody buys a system that gates the release
of physical product with a credit card. The sale is a conversation. The page's
only job is to start one.

**Do not build the Plant-tier features speculatively.** Roles, e-signature and
the device layer are listed on the page because buyers ask about them — build
them against a real customer's requirements, once someone is paying. Building
them alone and unpaid is how a year disappears.
