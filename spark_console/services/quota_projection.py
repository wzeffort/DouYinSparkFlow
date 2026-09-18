from datetime import datetime, timezone

from spark_console.models import TaskQuotaGrant


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def active_positive_grants(
    grants: list[TaskQuotaGrant], now: datetime
) -> list[TaskQuotaGrant]:
    """Return grants that currently contribute usable task capacity."""
    current = aware(now)
    return [
        grant
        for grant in grants
        if grant.amount > 0
        and grant.revoked_at is None
        and aware(grant.starts_at) <= current
        and (grant.expires_at is None or aware(grant.expires_at) > current)
    ]
