FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
# Live dashboard (FastAPI + SSE). The raw ADK dev UI is still available locally via `adk web .`
# A2A agents (Guardian + 3 carriers) run as their own service on :8081; the orchestrator calls them over the network
ENV A2A_BASE=http://127.0.0.1:8081
CMD ["sh", "-c", "uvicorn a2a_server:app --host 127.0.0.1 --port 8081 & uvicorn dashboard:app --host 0.0.0.0 --port ${PORT}"]
