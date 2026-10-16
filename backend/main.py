from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, or_
from sqlalchemy.orm import Session

import config
import models
import schemas
from database import get_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Personal Assistant API",
    description="Backend for the Personal Assistant Dashboard",
    version=config.APP_VERSION,
    lifespan=lifespan,
)

if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ==================== Helpers ====================

# Sort priorities by meaning, not alphabetically.
PRIORITY_RANK = case(
    (models.Task.priority == "urgent", 4),
    (models.Task.priority == "high", 3),
    (models.Task.priority == "medium", 2),
    else_=1,
)


def resolve_timezone(
    tz: str | None = Query(
        default=None,
        description="IANA timezone of the client, e.g. Europe/Berlin. "
        "Defaults to the X-Timezone header, then the server default.",
    ),
    x_timezone: str | None = Header(default=None),
) -> ZoneInfo:
    name = tz or x_timezone or config.DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Unknown timezone: {name!r}") from exc


def local_day_bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of a calendar day in ``zone``, as UTC."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def get_task_or_404(task_id: int, db: Session) -> models.Task:
    task = db.get(models.Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def get_event_or_404(event_id: int, db: Session) -> models.Event:
    event = db.get(models.Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


# ==================== Tasks ====================


@app.get("/tasks", response_model=list[schemas.Task], tags=["tasks"])
def list_tasks(
    status: schemas.TaskStatus | None = None,
    priority: schemas.Priority | None = None,
    tag: str | None = Query(default=None, description="Only tasks carrying this tag"),
    q: str | None = Query(default=None, min_length=1, description="Search in title/description"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(models.Task)
    if status:
        query = query.filter(models.Task.status == status.value)
    if priority:
        query = query.filter(models.Task.priority == priority.value)
    if q:
        pattern = f"%{q}%"
        query = query.filter(or_(models.Task.title.ilike(pattern), models.Task.description.ilike(pattern)))
    tasks = (
        query.order_by(models.Task.due_date.asc().nulls_last(), PRIORITY_RANK.desc(), models.Task.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    if tag:
        # tags are a JSON list; filtering in Python keeps this portable across databases.
        wanted = tag.lower()
        tasks = [t for t in tasks if any(x.lower() == wanted for x in (t.tags or []))]
    return tasks


@app.post("/tasks", response_model=schemas.Task, status_code=201, tags=["tasks"])
def create_task(task: schemas.TaskCreate, db: Session = Depends(get_db)):
    data = task.model_dump(mode="python")
    data["priority"] = task.priority.value
    data["status"] = task.status.value
    if task.status == schemas.TaskStatus.done:
        data["completed_at"] = datetime.now(timezone.utc)
    db_task = models.Task(**data)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task


@app.get("/tasks/{task_id}", response_model=schemas.Task, tags=["tasks"])
def get_task(task_id: int, db: Session = Depends(get_db)):
    return get_task_or_404(task_id, db)


@app.patch("/tasks/{task_id}", response_model=schemas.Task, tags=["tasks"])
def update_task(task_id: int, task_update: schemas.TaskUpdate, db: Session = Depends(get_db)):
    task = get_task_or_404(task_id, db)
    update_data = task_update.model_dump(exclude_unset=True)

    new_status = update_data.get("status")
    if new_status is not None:
        update_data["status"] = new_status.value
        if new_status == schemas.TaskStatus.done and task.status != "done":
            task.completed_at = datetime.now(timezone.utc)
        elif new_status != schemas.TaskStatus.done:
            task.completed_at = None
    if update_data.get("priority") is not None:
        update_data["priority"] = update_data["priority"].value

    for key, value in update_data.items():
        setattr(task, key, value)
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


@app.delete("/tasks/{task_id}", status_code=204, tags=["tasks"])
def delete_task(task_id: int, db: Session = Depends(get_db)):
    task = get_task_or_404(task_id, db)
    db.delete(task)
    db.commit()
    return None


# ==================== Events ====================


@app.get("/events", response_model=list[schemas.Event], tags=["events"])
def list_events(
    start: datetime | None = Query(default=None, description="Events ending at/after this time"),
    end: datetime | None = Query(default=None, description="Events starting before this time"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(models.Event)
    if start:
        query = query.filter(models.Event.end_time >= schemas.as_utc(start))
    if end:
        query = query.filter(models.Event.start_time < schemas.as_utc(end))
    return query.order_by(models.Event.start_time, models.Event.id).offset(offset).limit(limit).all()


@app.post("/events", response_model=schemas.Event, status_code=201, tags=["events"])
def create_event(event: schemas.EventCreate, db: Session = Depends(get_db)):
    db_event = models.Event(**event.model_dump())
    db.add(db_event)
    db.commit()
    db.refresh(db_event)
    return db_event


@app.get("/events/{event_id}", response_model=schemas.Event, tags=["events"])
def get_event(event_id: int, db: Session = Depends(get_db)):
    return get_event_or_404(event_id, db)


@app.patch("/events/{event_id}", response_model=schemas.Event, tags=["events"])
def update_event(event_id: int, event_update: schemas.EventUpdate, db: Session = Depends(get_db)):
    event = get_event_or_404(event_id, db)
    update_data = event_update.model_dump(exclude_unset=True)
    new_start = update_data.get("start_time", event.start_time)
    new_end = update_data.get("end_time", event.end_time)
    if new_end <= new_start:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")
    for key, value in update_data.items():
        setattr(event, key, value)
    db.commit()
    db.refresh(event)
    return event


@app.delete("/events/{event_id}", status_code=204, tags=["events"])
def delete_event(event_id: int, db: Session = Depends(get_db)):
    event = get_event_or_404(event_id, db)
    db.delete(event)
    db.commit()
    return None


# ==================== Today Summary ====================


@app.get("/today", response_model=schemas.TodaySummary, tags=["today"])
def get_today_summary(
    zone: ZoneInfo = Depends(resolve_timezone),
    db: Session = Depends(get_db),
):
    """Summary for *the client's* calendar day.

    Pass ``?tz=Area/City`` (or an ``X-Timezone`` header) so that "today" and "overdue"
    are computed in the user's timezone rather than the server's.
    """
    now = datetime.now(zone)
    today = now.date()
    start_utc, end_utc = local_day_bounds(today, zone)

    open_tasks = models.Task.status != "done"

    tasks_due_today = (
        db.query(models.Task)
        .filter(models.Task.due_date >= start_utc, models.Task.due_date < end_utc, open_tasks)
        .order_by(models.Task.due_date, PRIORITY_RANK.desc())
        .all()
    )
    overdue_tasks = (
        db.query(models.Task)
        .filter(models.Task.due_date < start_utc, open_tasks)
        .order_by(models.Task.due_date, PRIORITY_RANK.desc())
        .all()
    )
    upcoming_events = (
        db.query(models.Event)
        .filter(models.Event.start_time >= start_utc, models.Event.start_time < end_utc)
        .order_by(models.Event.start_time)
        .all()
    )
    total_open = db.query(models.Task).filter(open_tasks).count()
    completed_today = (
        db.query(models.Task)
        .filter(
            models.Task.status == "done",
            models.Task.completed_at >= start_utc,
            models.Task.completed_at < end_utc,
        )
        .count()
    )

    return schemas.TodaySummary(
        date=today,
        timezone=str(zone.key),
        tasks_due_today=tasks_due_today,
        overdue_tasks=overdue_tasks,
        upcoming_events=upcoming_events,
        total_open_tasks=total_open,
        completed_today=completed_today,
    )


@app.get("/health", tags=["meta"])
def health():
    return {
        "status": "ok",
        "version": config.APP_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ==================== Frontend ====================
# The single-page frontend is served from the same origin as the API so the app works
# from any device on the network (phone, tablet, another PC) at http://<host>:8000/
# with no API-URL configuration and no CORS. Mounted last so API routes win.
if config.FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")
