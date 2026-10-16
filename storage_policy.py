"""Operational policy boundary, separate from OrderStore business persistence.

Policies produce reviewable plans. They never erase business data or schedule
background work, and backup expiration never implies audit expiration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class StoragePolicy:
    backup_directory: str = "data/backups"
    backup_interval_hours: int = 24
    backup_keep_count: int | None = None
    backup_max_age_days: int | None = None
    archive_after_days: int | None = None
    preserve_audit: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.backup_directory, str) or not self.backup_directory.strip():
            raise ValueError("backup_directory must be a non-empty path")
        if self.preserve_audit is not True:
            raise ValueError("audit deletion is not supported")
        for key in ("backup_interval_hours", "backup_keep_count", "backup_max_age_days", "archive_after_days"):
            value = getattr(self, key)
            if value is None and key != "backup_interval_hours":
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{key} must be a positive integer or null")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "StoragePolicy":
        if not isinstance(values, Mapping) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("unknown storage policy fields")
        return cls(**dict(values))

    @classmethod
    def from_file(cls, path: str | Path) -> "StoragePolicy":
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("policy dates require an explicit timezone")
    return parsed.astimezone(timezone.utc)


def retention_plan(policy: StoragePolicy, backups: Iterable[Mapping[str, Any]],
                   orders: Iterable[Mapping[str, Any]] = (), *, now: datetime | None = None) -> dict[str, Any]:
    """Suggest backup expiration and order archival without changing anything.

    Only verified backup entries can be candidates; keep at least the newest
    verified backup even when every configured age threshold has elapsed.
    Both configured thresholds must permit expiration when used together.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now requires a timezone")
    entries = sorted((dict(b) for b in backups if b.get("verified") is True),
                     key=lambda b: _date(b["createdAt"]), reverse=True)
    expired = []
    for index, entry in enumerate(entries):
        configured = policy.backup_keep_count is not None or policy.backup_max_age_days is not None
        beyond_count = policy.backup_keep_count is None or index >= policy.backup_keep_count
        beyond_age = policy.backup_max_age_days is None or _date(entry["createdAt"]) < now - timedelta(days=policy.backup_max_age_days)
        if configured and index > 0 and beyond_count and beyond_age:
            expired.append(entry["bundle"])
    archive = []
    if policy.archive_after_days:
        cutoff = now - timedelta(days=policy.archive_after_days)
        for order in orders:
            # Never archive a live draft, active quote or in-progress job from age alone.
            if order.get("status") == "cancelled" and _date(order["updatedAt"]) < cutoff:
                archive.append({"orderId": order["orderId"], "expectedRevision": order["revision"]})
    return {"mode": "plan_only", "preserveAudit": True, "deleteBusinessRecords": False,
            "backupExpirationCandidates": expired, "archiveCandidates": archive,
            "automaticScheduling": False, "policy": policy.as_dict()}
