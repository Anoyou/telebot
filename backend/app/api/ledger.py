"""资金台账 API。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..deps import CurrentUser, DBSession
from ..services import ledger_service, stats_service

router = APIRouter(prefix="/api/ledger", tags=["ledger"])


class LedgerEntryOut(BaseModel):
    id: int
    source: str
    source_id: int
    direction: str
    amount: str
    signed_amount: str
    status: str
    account_id: int
    chat_id: int | None
    plugin_key: str | None
    entry_key: str | None
    channel: str | None
    session_key: str | None
    action_type: str
    payout_key: str | None
    error_code: str | None
    created_at: datetime
    params_summary: dict[str, Any] = Field(default_factory=dict)


class LedgerEntriesResponse(BaseModel):
    items: list[LedgerEntryOut]


class LedgerSummaryBucketOut(BaseModel):
    key: str
    label: str
    income: str
    payout: str
    net: str
    count: int


class LedgerSummaryOut(BaseModel):
    income: str
    payout: str
    net: str
    count: int
    by_day: list[LedgerSummaryBucketOut]
    by_chat: list[LedgerSummaryBucketOut]


class LedgerCompensationOut(BaseModel):
    id: int
    payout_key: str
    account_id: int
    trace_id: str | None
    plugin_key: str | None
    entry_key: str | None
    origin: str
    chat_id: int
    amount: str
    status: str
    error_code_first: str | None
    error_code_last: str | None
    error_last: str | None
    ambiguous: bool
    retry_count: int
    next_attempt_at: datetime
    sent_message_id: int | None
    sent_at: datetime | None
    notified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LedgerCompensationsResponse(BaseModel):
    items: list[LedgerCompensationOut]


class LedgerManualPaidRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class MetricAvailabilityOut(BaseModel):
    key: str
    label: str
    status: str
    source: str
    note: str


class OperationalStatsTotalOut(BaseModel):
    started_sessions: int
    participant_count: int | None
    payout_success_count: int
    payout_failure_count: int
    payout_attempt_count: int
    payout_success_rate: str | None
    ledger_income: str
    ledger_payout: str
    ledger_net: str
    ledger_count: int


class OperationalStatsBucketOut(BaseModel):
    key: str
    label: str
    started_sessions: int
    payout_success_count: int
    payout_failure_count: int
    payout_attempt_count: int
    payout_success_rate: str | None
    ledger_income: str
    ledger_payout: str
    ledger_net: str
    ledger_count: int


class OperationalStatsOut(BaseModel):
    total: OperationalStatsTotalOut
    by_day: list[OperationalStatsBucketOut]
    by_chat: list[OperationalStatsBucketOut]
    source_matrix: list[MetricAvailabilityOut]


def _filters(
    *,
    since: datetime | None,
    until: datetime | None,
    account_id: int | None,
    chat_id: int | None,
    plugin_key: str | None,
    direction: str | None,
    amount: Decimal | None,
    amount_min: Decimal | None,
    amount_max: Decimal | None,
    status: str | None,
    limit: int | None,
) -> ledger_service.LedgerFilters:
    return ledger_service.LedgerFilters(
        since=since,
        until=until,
        account_id=account_id,
        chat_id=chat_id,
        plugin_key=plugin_key,
        direction=direction,
        amount=amount,
        amount_min=amount_min,
        amount_max=amount_max,
        status=status,
        limit=limit,
    )


def _stats_filters(
    *,
    since: datetime | None,
    until: datetime | None,
    account_id: int | None,
    chat_id: int | None,
    plugin_key: str | None,
) -> stats_service.StatsFilters:
    return stats_service.StatsFilters(
        since=since,
        until=until,
        account_id=account_id,
        chat_id=chat_id,
        plugin_key=plugin_key,
    )


@router.get("", response_model=LedgerEntriesResponse)
async def list_ledger_entries(
    db: DBSession,
    _user: CurrentUser,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    account_id: int | None = Query(default=None),
    chat_id: int | None = Query(default=None),
    plugin_key: str | None = Query(default=None),
    direction: str | None = Query(default=None),
    amount: Decimal | None = Query(default=None),
    amount_min: Decimal | None = Query(default=None),
    amount_max: Decimal | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> LedgerEntriesResponse:
    entries = await ledger_service.list_ledger_entries(
        db,
        _filters(
            since=since,
            until=until,
            account_id=account_id,
            chat_id=chat_id,
            plugin_key=plugin_key,
            direction=direction,
            amount=amount,
            amount_min=amount_min,
            amount_max=amount_max,
            status=status,
            limit=limit,
        ),
    )
    return LedgerEntriesResponse(items=[LedgerEntryOut(**asdict(item)) for item in entries])


@router.get("/summary", response_model=LedgerSummaryOut)
async def get_ledger_summary(
    db: DBSession,
    _user: CurrentUser,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    account_id: int | None = Query(default=None),
    chat_id: int | None = Query(default=None),
    plugin_key: str | None = Query(default=None),
    direction: str | None = Query(default=None),
    amount: Decimal | None = Query(default=None),
    amount_min: Decimal | None = Query(default=None),
    amount_max: Decimal | None = Query(default=None),
    status: str | None = Query(default=None),
) -> LedgerSummaryOut:
    summary = await ledger_service.summarize_ledger(
        db,
        _filters(
            since=since,
            until=until,
            account_id=account_id,
            chat_id=chat_id,
            plugin_key=plugin_key,
            direction=direction,
            amount=amount,
            amount_min=amount_min,
            amount_max=amount_max,
            status=status,
            limit=None,
        ),
    )
    return LedgerSummaryOut(**asdict(summary))


@router.get("/stats", response_model=OperationalStatsOut)
async def get_operational_stats(
    db: DBSession,
    _user: CurrentUser,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    account_id: int | None = Query(default=None),
    chat_id: int | None = Query(default=None),
    plugin_key: str | None = Query(default=None),
) -> OperationalStatsOut:
    stats = await stats_service.summarize_operational_stats(
        db,
        _stats_filters(
            since=since,
            until=until,
            account_id=account_id,
            chat_id=chat_id,
            plugin_key=plugin_key,
        ),
    )
    return OperationalStatsOut(**asdict(stats))


@router.get("/compensations", response_model=LedgerCompensationsResponse)
async def list_compensations(
    db: DBSession,
    _user: CurrentUser,
    account_id: int | None = Query(default=None),
    chat_id: int | None = Query(default=None),
    plugin_key: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> LedgerCompensationsResponse:
    rows = await ledger_service.list_compensations(
        db,
        account_id=account_id,
        chat_id=chat_id,
        plugin_key=plugin_key,
        limit=limit,
    )
    return LedgerCompensationsResponse(items=[LedgerCompensationOut(**asdict(item)) for item in rows])


@router.post("/compensations/{compensation_id}/manual-paid", response_model=LedgerCompensationOut)
async def mark_compensation_manual_paid(
    compensation_id: int,
    payload: LedgerManualPaidRequest,
    db: DBSession,
    user: CurrentUser,
) -> LedgerCompensationOut:
    row = await ledger_service.mark_compensation_manual_paid(
        db,
        compensation_id,
        user_id=user.id,
        note=payload.note,
    )
    await db.commit()
    return LedgerCompensationOut(**asdict(row))
