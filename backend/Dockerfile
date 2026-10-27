FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py database.py places.py weather.py system_prompt.txt ./
COPY data ./data

RUN mkdir -p logs

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
