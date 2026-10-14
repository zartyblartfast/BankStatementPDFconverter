"""Typed MCP response envelope; analytical service remains standard-library only."""

from typing import Any, Literal
from pydantic import BaseModel


class AnalysisMeta(BaseModel):
    schema_version: Literal["1.1"]
    calculation_basis: Literal["raw", "transfer_adjusted"]
    as_of_utc: str
    data_revision: str
    currency: Literal["GBP"]
    money_unit: Literal["pence"]
    filters: dict[str, str]
    include_excluded: bool
    uncategorised_included: bool
    transaction_details_enabled: bool
    contains_transaction_details: bool
    method: str
    warnings: list[str]
    evidence_ref: str


class AnalysisResponse(BaseModel):
    meta: AnalysisMeta
    data: dict[str, Any]
