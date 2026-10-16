from datetime import timezone

from sqlalchemy import JSON, Column, DateTime, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class UTCDateTime(TypeDecorator):
    """Store datetimes as naive UTC, return them timezone-aware (UTC).

    Clients on phones, tablets and desktops are in different timezones; the API
    therefore speaks timezone-aware ISO-8601 only. Naive input is treated as UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    priority = Column(String, default="medium")  # low, medium, high, urgent
    status = Column(String, default="todo")  # todo, in_progress, done
    due_date = Column(UTCDateTime, nullable=True, index=True)
    tags = Column(JSON, default=list)
    completed_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, server_default=func.now())
    updated_at = Column(UTCDateTime, server_default=func.now(), onupdate=func.now())


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    start_time = Column(UTCDateTime, nullable=False, index=True)
    end_time = Column(UTCDateTime, nullable=False)
    location = Column(String, nullable=True)
    created_at = Column(UTCDateTime, server_default=func.now())
