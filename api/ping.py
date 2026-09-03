"""Minimal Vercel Python function for testing."""
from fastapi import FastAPI

app = FastAPI()

@app.get("/api/ping")
async def ping():
    return {"status": "ok", "python": "works"}