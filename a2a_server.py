"""Standalone A2A service: the Guardian and the three carrier agents run in their own process (own port),
so every A2A call from the orchestrator crosses a real network boundary."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "mcp_server"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "my_agent" / ".env")
from fastapi import FastAPI  # noqa: E402

from a2a_agents import router  # noqa: E402

app = FastAPI(title="Vegam A2A agents")
app.include_router(router)
