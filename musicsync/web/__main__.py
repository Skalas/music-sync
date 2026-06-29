"""Entry point: python -m musicsync.web starts the API server on :8000."""

from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "musicsync.web.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
