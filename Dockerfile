# FX MOS demo image.
#
# The database is seeded at build time so the demo opens with a shop already
# running: bays occupied, faults caught, real bay hours. An empty board tells a
# visitor nothing about whether the system works.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Seed both boards: the assembly line and the service shop.
RUN python -m fx_mos.simulator --units 24 --defect-rate 0.09 --seed 7 --reset \
 && python -m fx_mos.simulator_shop --vehicles 60 --defect-rate 0.09 --seed 11 --leave-in-bays 6

ENV PORT=8000
EXPOSE 8000

CMD uvicorn fx_mos.api.main:app --host 0.0.0.0 --port ${PORT}
