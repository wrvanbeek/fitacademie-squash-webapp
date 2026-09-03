"""Database models for FitAcademie Squash Webapp."""
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Integer, String, Boolean, DateTime, Date, Time, Text, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # bcrypt of local password

    # FitAcademie portal credentials (encrypted)
    fitacademie_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    fitacademie_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Playwright session cookies (JSON storage_state)
    session_cookies: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cookies_updated: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Remember me token
    remember_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    partners: Mapped[list["Partner"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    recurring_bookings: Mapped[list["RecurringBooking"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    reservations: Mapped[list["Reservation"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Partner(Base):
    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255))
    is_bepalend_lid: Mapped[bool] = mapped_column(Boolean, default=False)  # no extra cost
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="partners")
    recurring_bookings: Mapped[list["RecurringBooking"]] = relationship(back_populates="partner")
    reservations: Mapped[list["Reservation"]] = relationship(back_populates="partner")


class RecurringBooking(Base):
    __tablename__ = "recurring_bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    partner_id: Mapped[int] = mapped_column(Integer, ForeignKey("partners.id", ondelete="SET NULL"), nullable=True)

    court: Mapped[int] = mapped_column(Integer)  # 1 or 2
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Mon ... 6=Sun
    time: Mapped[str] = mapped_column(String(5))  # "HH:MM"
    frequency: Mapped[str] = mapped_column(String(20), default="weekly")  # weekly, biweekly, cron
    cron_expr: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    last_run: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_run: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship(back_populates="recurring_bookings")
    partner: Mapped[Optional["Partner"]] = relationship(back_populates="recurring_bookings")


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    partner_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("partners.id", ondelete="SET NULL"), nullable=True)

    court: Mapped[int] = mapped_column(Integer)
    date: Mapped[datetime] = mapped_column(Date, index=True)
    time: Mapped[str] = mapped_column(String(5))  # "HH:MM"
    status: Mapped[str] = mapped_column(String(20), default="booked")  # booked, paid, cancelled, failed
    fitacademie_reservation_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    amount_paid: Mapped[Optional[float]] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="reservations")
    partner: Mapped[Optional["Partner"]] = relationship(back_populates="reservations")

    __table_args__ = (
        UniqueConstraint("user_id", "court", "date", "time", name="uq_user_court_datetime"),
    )