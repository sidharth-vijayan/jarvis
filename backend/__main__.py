"""Run with: python -m backend"""
import os

import uvicorn

# Bound to localhost only. Remote (phone) access is a later phase and stays off until configured.
uvicorn.run("backend.main:app", host="127.0.0.1", port=int(os.environ.get("JARVIS_PORT", "8765")))
