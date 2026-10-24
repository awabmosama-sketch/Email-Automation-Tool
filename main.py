"""ASGI entrypoint so `uvicorn main:app --reload` works from the project root.

The dashboard/API application lives in `backend/app.py`.
The command-line batch sender is still `python -m backend.main`.
"""

from backend.app import app

__all__ = ["app"]
