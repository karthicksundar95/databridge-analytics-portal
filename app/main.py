import os
from typing import Optional

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://databridge:databridge_dev@localhost:5432/databridge")
JWT_SECRET = os.getenv("JWT_SECRET", "development-only-secret-change-me")
JWT_ALGORITHM = "HS256"
ORIGINS = [item.strip() for item in os.getenv("CORS_ORIGIN", "").split(",") if item]
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
app = FastAPI(title="DataBridge Analytics Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def start():
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8082")),
        reload=os.getenv("ENVIRONMENT", "development") != "production",
    )


def database_session():
    with Session(engine) as session:
        yield session


def current_analyst(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    try:
        user = jwt.decode(authorization.removeprefix("Bearer "), JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    if user.get("role") not in {"analyst", "administrator"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Analytics workspace access required")
    return user


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class AssignmentRequest(ApiModel):
    analyst_id: str


class StatusRequest(ApiModel):
    status: str = Field(pattern="^(validating|received|awaiting_assignment|assigned|under_analysis|awaiting_review|completed|failed|rejected)$")


class NoteRequest(ApiModel):
    body: str = Field(min_length=1, max_length=10000)


@app.get("/")
def root():
    return {"service": "DataBridge Analytics Service", "status": "running", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health(db: Session = Depends(database_session)):
    db.execute(text("SELECT 1"))
    return {"status": "healthy", "service": "analytics"}


@app.get("/submissions")
def list_submissions(user=Depends(current_analyst), db: Session = Depends(database_session)):
    rows = db.execute(
        text("""SELECT s.id, s.project_name, s.site, s.country_code, s.status, s.created_at,
                       u.email AS contributor_email, p.display_name AS contributor_name,
                       COUNT(DISTINCT f.id) AS file_count, a.analyst_id
                FROM submissions s
                JOIN users u ON u.id = s.contributor_id
                LEFT JOIN profiles p ON p.user_id = u.id
                LEFT JOIN submission_files f ON f.submission_id = s.id
                LEFT JOIN assignments a ON a.submission_id = s.id
                GROUP BY s.id, u.email, p.display_name, a.analyst_id
                ORDER BY s.created_at DESC""")
    ).mappings()
    return [dict(row) for row in rows]


@app.post("/submissions/{submission_id}/assignment")
def assign_submission(submission_id: str, payload: AssignmentRequest, user=Depends(current_analyst), db: Session = Depends(database_session)):
    analyst = db.execute(
        text("SELECT id FROM users WHERE id = :id AND role = 'analyst' AND status = 'active'"),
        {"id": payload.analyst_id},
    ).one_or_none()
    if not analyst:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active analyst not found")
    submission = db.execute(text("SELECT id FROM submissions WHERE id = :id"), {"id": submission_id}).one_or_none()
    if not submission:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submission not found")
    assignment = db.execute(
        text("""INSERT INTO assignments (submission_id, analyst_id, assigned_by)
                VALUES (:submission_id, :analyst_id, :assigned_by)
                ON CONFLICT (submission_id) DO UPDATE SET analyst_id = EXCLUDED.analyst_id,
                    assigned_by = EXCLUDED.assigned_by, created_at = NOW()
                RETURNING id, submission_id, analyst_id, assigned_by, created_at"""),
        {"submission_id": submission_id, "analyst_id": payload.analyst_id, "assigned_by": user["sub"]},
    ).mappings().one()
    db.execute(
        text("UPDATE submissions SET status = 'assigned', updated_at = NOW() WHERE id = :id"),
        {"id": submission_id},
    )
    db.commit()
    return dict(assignment)


@app.patch("/submissions/{submission_id}/status")
def update_status(submission_id: str, payload: StatusRequest, user=Depends(current_analyst), db: Session = Depends(database_session)):
    result = db.execute(
        text("UPDATE submissions SET status = :status, updated_at = NOW() WHERE id = :id RETURNING id, status"),
        {"id": submission_id, "status": payload.status},
    ).mappings().one_or_none()
    if not result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submission not found")
    db.commit()
    return dict(result)


@app.post("/submissions/{submission_id}/notes", status_code=201)
def add_note(submission_id: str, payload: NoteRequest, user=Depends(current_analyst), db: Session = Depends(database_session)):
    note = db.execute(
        text("""INSERT INTO analyst_notes (submission_id, analyst_id, body)
                SELECT s.id, :analyst_id, :body FROM submissions s WHERE s.id = :submission_id
                RETURNING id, submission_id, analyst_id, body, created_at"""),
        {"submission_id": submission_id, "analyst_id": user["sub"], "body": payload.body.strip()},
    ).mappings().one_or_none()
    if not note:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submission not found")
    db.commit()
    return dict(note)


@app.get("/assignments/mine")
def my_assignments(user=Depends(current_analyst), db: Session = Depends(database_session)):
    rows = db.execute(
        text("""SELECT s.id, s.project_name, s.site, s.country_code, s.status, s.created_at
                FROM assignments a JOIN submissions s ON s.id = a.submission_id
                WHERE a.analyst_id = :analyst_id ORDER BY s.created_at DESC"""),
        {"analyst_id": user["sub"]},
    ).mappings()
    return [dict(row) for row in rows]
