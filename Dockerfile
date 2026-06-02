FROM python:3.12-slim AS base

WORKDIR /script

FROM python:3.12-slim AS realease

COPY --from=base /usr/local/bin/ /usr/local/bin/

COPY . .

COPY requirements.txt .
RUN pip install -r requirements.txt

CMD ["python", "script.py"]