"""Prior-art search and pre-exam state (docs/선행기술조사_사전심사_설계_2026-10.md)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SEARCH_COUNTRIES = ["KR", "JP", "US", "CN", "EP", "PCT"]

# PRIOR_ART_SET labels (설계 5.1). A searched set is never an exhaustive search.
LABEL_NONE = "NONE"
LABEL_PROVIDED = "PROVIDED"
LABEL_SEARCHED = "SEARCHED — NOT_EXHAUSTIVE"
LABEL_BOTH = "PROVIDED+SEARCHED — NOT_EXHAUSTIVE"

# p_screen → relevance bands (설계 5.6)
RELEVANCE_HIGH = 0.7
RELEVANCE_MID = 0.4


class PriorSearchOptions(BaseModel):
    """Per-run search options: claim-agent.yaml `prior_search` defaults, overridden by the request."""

    enabled: bool = False
    countries: list[str] = Field(default_factory=lambda: list(SEARCH_COUNTRIES))
    size_per_query: int = Field(default=10, ge=1, le=50)
    max_docs: int = Field(default=5, ge=0, le=20)
    confirm: bool = False                     # True: the user picks the documents and base date before fetching
    base_date: str | None = None              # 출원 예정일; None → the run date
    inventive_high: Literal["note", "review"] = "note"
    screen_threshold: float = Field(default=0.5, ge=0, le=1)
    trim_threshold: float = Field(default=0.5, ge=0, le=1)
    trim: bool = True
    max_wips_calls: int = Field(default=40, ge=1)


class PriorArtDoc(BaseModel):
    skey: str
    number: str = ""
    title: str = ""
    published: str = ""
    filed: str = ""
    score: float = 0.0
    path: str = ""
    sha256: str = ""
    p_screen: float | None = None
    p_trim: float | None = None
    relevance: str | None = None
    warnings: list[str] = Field(default_factory=list)


class PriorArtState(BaseModel):
    options: PriorSearchOptions
    base_date: str
    base_date_source: Literal["REQUEST", "RUN_DATE"] = "RUN_DATE"
    query_record_id: str | None = None
    queries_ko: list[str] = Field(default_factory=list)
    queries_en: list[str] = Field(default_factory=list)
    provisional_elements: list[str] = Field(default_factory=list)
    problem: str = ""
    hits: list[dict[str, Any]] = Field(default_factory=list)
    search_warnings: list[str] = Field(default_factory=list)
    screened: dict[str, dict[str, Any]] = Field(default_factory=dict)   # skey -> {p_screen, relevance, reason}
    screening: Literal["JEV", "MODEL_FALLBACK", "SCORE_ONLY"] | None = None
    jev_questions: dict[str, str] = Field(default_factory=dict)        # stage -> question template
    selected: list[str] = Field(default_factory=list)
    selection_rule: str = ""
    excluded_by_date: list[str] = Field(default_factory=list)
    docs: list[PriorArtDoc] = Field(default_factory=list)              # fetched
    trim: Literal["JEV", "SKIPPED", "OFF"] | None = None
    trimmed: list[dict[str, Any]] = Field(default_factory=list)       # excluded by PRIOR_TRIM: {skey, number, p_trim}
    set_id: str | None = None
    set_record_id: str | None = None
    wips_calls: int = 0
    skipped: bool = False      # the user accepted PRIOR_SEARCH_UNAVAILABLE: the run continues without a searched set

    def kept(self) -> list[PriorArtDoc]:
        drop = {t["skey"] for t in self.trimmed}
        return [d for d in self.docs if d.skey not in drop]


def relevance_band(p: float) -> str:
    return "높음" if p >= RELEVANCE_HIGH else "중간" if p >= RELEVANCE_MID else "낮음"
