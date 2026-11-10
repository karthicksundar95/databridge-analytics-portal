# DataBridge analytics service

Python/FastAPI service for internal submission operations: inbox queries, analyst assignments, status transitions, and private analyst notes.

Run locally with `uv run start` (port 8082). Access is restricted to JWTs carrying the analyst or administrator role. Database schema and local dependencies are provided by the DataBridge deployment repository's Docker Compose setup.
