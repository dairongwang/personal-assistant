from datetime import date, datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Priority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"


# ---- shared validation helpers ------------------------------------------------


def as_utc(value: datetime | None) -> datetime | None:
    """Accept aware or naive datetimes; naive means UTC. Always return aware UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def not_blank(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


def clean_tags(tags: list[str] | None) -> list[str] | None:
    if tags is None:
        return None
    seen, out = set(), []
    for tag in tags:
        tag = tag.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


# ==================== Tasks ====================


class TaskBase(BaseModel):
    title: str = Field(max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    priority: Priority = Priority.medium
    status: TaskStatus = TaskStatus.todo
    due_date: datetime | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)

    _v_title = field_validator("title")(not_blank)
    _v_due = field_validator("due_date")(as_utc)
    _v_tags = field_validator("tags")(clean_tags)


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    priority: Priority | None = None
    status: TaskStatus | None = None
    due_date: datetime | None = None
    tags: list[str] | None = Field(default=None, max_length=20)

    _v_title = field_validator("title")(not_blank)
    _v_due = field_validator("due_date")(as_utc)
    _v_tags = field_validator("tags")(clean_tags)


class Task(TaskBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# ==================== Events ====================


class EventBase(BaseModel):
    title: str = Field(max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    start_time: datetime
    end_time: datetime
    location: str | None = Field(default=None, max_length=500)

    _v_title = field_validator("title")(not_blank)
    _v_times = field_validator("start_time", "end_time")(as_utc)

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class EventCreate(EventBase):
    pass


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    start_time: datetime | None = None
    end_time: datetime | None = None
    location: str | None = Field(default=None, max_length=500)

    _v_title = field_validator("title")(not_blank)
    _v_times = field_validator("start_time", "end_time")(as_utc)


class Event(EventBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# ==================== Today ====================


class TodaySummary(BaseModel):
    date: date
    timezone: str
    tasks_due_today: list[Task]
    overdue_tasks: list[Task]
    upcoming_events: list[Event]
    total_open_tasks: int
    completed_today: int
