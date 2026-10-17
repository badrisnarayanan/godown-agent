FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
# Live dashboard (FastAPI + SSE). The raw ADK dev UI is still available locally via `adk web .`
CMD ["sh", "-c", "uvicorn dashboard:app --host 0.0.0.0 --port ${PORT}"]
