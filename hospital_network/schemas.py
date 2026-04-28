from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OptimizationConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p_expansions: int = Field(default=7, ge=1)
    added_beds_per_expansion: int = Field(default=1500, ge=1)
    time_limit_seconds: int = Field(default=120, ge=1)
    fixed_hub_hospital_ids: list[str] = Field(default_factory=list)
    show_solver_log: bool = True
    export_model_file: bool = False
    display_interval_seconds: int = Field(default=1, ge=1)


class DatasetCsvPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    distance_csv: str | None = None
    hospitals_csv: str | None = None
    zones_csv: str | None = None


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: OptimizationConfigPayload = Field(default_factory=OptimizationConfigPayload)
    dataset: DatasetCsvPayload | None = None
