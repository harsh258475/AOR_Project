from __future__ import annotations

from dataclasses import asdict, dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any
import logging

import gurobipy as gp
import numpy as np
import pandas as pd
from gurobipy import GRB


logger = logging.getLogger(__name__)

DEFAULT_DISTANCE_FILE = "distance_matrix.csv"
DEFAULT_HOSPITALS_FILE = "hospitals.csv"
DEFAULT_ZONES_FILE = "zones.csv"

STATUS_LABELS = {
    GRB.LOADED: "LOADED",
    GRB.OPTIMAL: "OPTIMAL",
    GRB.INFEASIBLE: "INFEASIBLE",
    GRB.INF_OR_UNBD: "INF_OR_UNBD",
    GRB.UNBOUNDED: "UNBOUNDED",
    GRB.CUTOFF: "CUTOFF",
    GRB.ITERATION_LIMIT: "ITERATION_LIMIT",
    GRB.NODE_LIMIT: "NODE_LIMIT",
    GRB.TIME_LIMIT: "TIME_LIMIT",
    GRB.SOLUTION_LIMIT: "SOLUTION_LIMIT",
    GRB.INTERRUPTED: "INTERRUPTED",
    GRB.NUMERIC: "NUMERIC",
    GRB.SUBOPTIMAL: "SUBOPTIMAL",
    GRB.INPROGRESS: "INPROGRESS",
    GRB.USER_OBJ_LIMIT: "USER_OBJ_LIMIT",
}

REQUIRED_DISTANCE_COLUMNS = {"zone_id", "hospital_id", "travel_cost"}
REQUIRED_HOSPITAL_COLUMNS = {
    "hospital_id",
    "name",
    "existing_beds",
    "cost_per_added_bed",
    "fixed_open_expand_cost",
}
REQUIRED_ZONE_COLUMNS = {"zone_id", "patient_demand"}

ID_COLUMNS = {
    "distance": ("zone_id", "hospital_id"),
    "hospitals": ("hospital_id",),
    "zones": ("zone_id",),
}

NUMERIC_COLUMNS = {
    "distance": ("travel_cost",),
    "hospitals": ("existing_beds", "cost_per_added_bed", "fixed_open_expand_cost"),
    "zones": ("patient_demand",),
}

VISUAL_COLUMNS = {
    "hospitals": {"x_coord", "y_coord"},
    "zones": {"x_coord", "y_coord"},
}


@dataclass(frozen=True)
class OptimizationConfig:
    p_expansions: int = 7
    added_beds_per_expansion: int = 1500
    dual_ub_factor: float = 2.0
    time_limit_seconds: int = 60
    fixed_hub_hospital_ids: tuple[str, ...] = field(default_factory=tuple)
    show_solver_log: bool = False
    export_model_file: bool = False
    display_interval_seconds: int = 1

    def __post_init__(self) -> None:
        if self.p_expansions <= 0:
            raise ValueError("p_expansions must be positive.")
        if self.added_beds_per_expansion <= 0:
            raise ValueError("added_beds_per_expansion must be positive.")
        if self.dual_ub_factor <= 0:
            raise ValueError("dual_ub_factor must be strictly positive.")
        if self.time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be positive.")
        if self.display_interval_seconds <= 0:
            raise ValueError("display_interval_seconds must be positive.")
        normalized_ids = tuple(str(hospital_id).strip() for hospital_id in self.fixed_hub_hospital_ids)
        if any(not hospital_id for hospital_id in normalized_ids):
            raise ValueError("fixed_hub_hospital_ids cannot contain blank identifiers.")
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("fixed_hub_hospital_ids cannot contain duplicates.")
        object.__setattr__(self, "fixed_hub_hospital_ids", normalized_ids)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["fixed_hub_hospital_ids"] = list(self.fixed_hub_hospital_ids)
        return payload


def load_dataset_from_disk(base_dir: str | Path = ".") -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    base_path = Path(base_dir)
    distance = pd.read_csv(base_path / DEFAULT_DISTANCE_FILE)
    hospitals = pd.read_csv(base_path / DEFAULT_HOSPITALS_FILE)
    zones = pd.read_csv(base_path / DEFAULT_ZONES_FILE)
    return _normalize_and_validate(distance, hospitals, zones)


def load_dataset_from_csv_text(
    *,
    base_dir: str | Path = ".",
    distance_csv: str | None = None,
    hospitals_csv: str | None = None,
    zones_csv: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    base_path = Path(base_dir)

    distance = _read_csv_text_or_disk(distance_csv, base_path / DEFAULT_DISTANCE_FILE)
    hospitals = _read_csv_text_or_disk(hospitals_csv, base_path / DEFAULT_HOSPITALS_FILE)
    zones = _read_csv_text_or_disk(zones_csv, base_path / DEFAULT_ZONES_FILE)
    return _normalize_and_validate(distance, hospitals, zones)


def build_dataset_preview(
    distance: pd.DataFrame,
    hospitals: pd.DataFrame,
    zones: pd.DataFrame,
    sample_rows: int = 5,
) -> dict[str, Any]:
    return {
        "summary": summarize_dataset(distance, hospitals, zones),
        "hospital_ids": hospitals["hospital_id"].astype(str).tolist(),
        "distance_preview": dataframe_preview(distance, sample_rows),
        "hospitals_preview": dataframe_preview(hospitals, sample_rows),
        "zones_preview": dataframe_preview(zones, sample_rows),
    }


def summarize_dataset(distance: pd.DataFrame, hospitals: pd.DataFrame, zones: pd.DataFrame) -> dict[str, Any]:
    return {
        "hospital_count": int(len(hospitals)),
        "zone_count": int(len(zones)),
        "distance_record_count": int(len(distance)),
        "total_existing_capacity": float(hospitals["existing_beds"].sum()),
        "total_demand": float(zones["patient_demand"].sum()),
        "travel_cost_min": float(distance["travel_cost"].min()),
        "travel_cost_max": float(distance["travel_cost"].max()),
        "coordinates_available": _coordinates_available(hospitals, zones),
    }


def solve_bilevel_optimization(
    distance: pd.DataFrame,
    hospitals: pd.DataFrame,
    zones: pd.DataFrame,
    config: OptimizationConfig,
    *,
    artifact_dir: str | Path | None = None,
    log_to_console: bool = False,
    capture_solver_log: bool = False,
) -> dict[str, Any]:
    hospital_ids = hospitals["hospital_id"].tolist()
    zone_ids = zones["zone_id"].tolist()

    if config.p_expansions > len(hospital_ids):
        raise ValueError(
            f"p_expansions = {config.p_expansions} exceeds the number of hospitals = {len(hospital_ids)}."
        )

    provided_hub_ids = tuple(config.fixed_hub_hospital_ids)
    unknown_fixed_hubs = sorted(set(provided_hub_ids).difference(hospital_ids))
    if unknown_fixed_hubs:
        raise ValueError(f"Unknown fixed hub hospital_id values: {unknown_fixed_hubs}")
    if provided_hub_ids and len(provided_hub_ids) != config.p_expansions:
        raise ValueError(
            "Provide exactly p_expansions hospital IDs as the incumbent starting hub combination."
        )

    base_beds = dict(zip(hospital_ids, hospitals["existing_beds"]))
    demand = dict(zip(zone_ids, zones["patient_demand"]))
    hospital_name = dict(zip(hospital_ids, hospitals["name"]))
    added_beds = config.added_beds_per_expansion

    expand_cost = {
        row.hospital_id: float(row.fixed_open_expand_cost + added_beds * row.cost_per_added_bed)
        for row in hospitals.itertuples(index=False)
    }
    travel_cost = {
        (row.hospital_id, row.zone_id): float(row.travel_cost)
        for row in distance.itertuples(index=False)
    }
    customer_benefit = _build_customer_benefit(distance)

    total_demand = float(sum(demand.values()))
    total_capacity = float(sum(base_beds.values()) + config.p_expansions * added_beds)
    if total_capacity < total_demand:
        raise ValueError(
            "The model is infeasible under the current capacity policy: "
            f"total capacity {total_capacity:,.2f} < total demand {total_demand:,.2f}."
        )

    if provided_hub_ids and len(provided_hub_ids) != config.p_expansions:
        raise ValueError(
            "Provide exactly p_expansions hospital IDs as the incumbent starting hub combination."
        )

    dual_ub = float(max(customer_benefit.values()) if customer_benefit else 0.0) * config.dual_ub_factor
    dual_ub = max(dual_ub, 1.0)
    artifact_root = Path(artifact_dir) if artifact_dir is not None else Path(".")
    artifact_root.mkdir(parents=True, exist_ok=True)

    model_file_path: Path | None = None
    solver_log_sections: list[str] = []
    iteration_history: list[dict[str, Any]] = []

    logger.info(
        "Starting branch-and-cut bilevel optimization with %s hospitals, %s zones, p=%s.",
        len(hospital_ids),
        len(zone_ids),
        config.p_expansions,
    )

    iteration_log_lines = [
        "Branch-and-cut bilevel solve started.",
        f"Hospitals={len(hospital_ids)} Zones={len(zone_ids)} P={config.p_expansions} AddedBeds={added_beds}",
        f"Initial incumbent hubs={list(provided_hub_ids) if provided_hub_ids else '[]'}",
        "The provided hubs are evaluated first as an incumbent baseline.",
        "Global optimization then uses MILP branch-and-cut and stops when optimality is proven.",
    ]

    incumbent_solution: dict[str, Any] | None = None
    incumbent_cost_cutoff: float | None = None
    if provided_hub_ids:
        incumbent_model, incumbent_vars = _build_bilevel_stage_model(
            hospital_ids=hospital_ids,
            zone_ids=zone_ids,
            base_beds=base_beds,
            demand=demand,
            expand_cost=expand_cost,
            customer_benefit=customer_benefit,
            config=config,
            dual_ub=dual_ub,
            fixed_hubs=set(provided_hub_ids),
            objective_mode="maximize_benefit",
        )
        incumbent_stage_log = _optimize_model(
            incumbent_model,
            stage_name="incumbent_provided_hubs",
            artifact_root=artifact_root,
            display_interval_seconds=config.display_interval_seconds,
            time_limit_seconds=config.time_limit_seconds,
            log_to_console=log_to_console,
            capture_solver_log=capture_solver_log,
            export_model_file=config.export_model_file,
        )
        if incumbent_stage_log["model_file_path"] is not None:
            model_file_path = incumbent_stage_log["model_file_path"]
        solver_log_sections.extend(incumbent_stage_log["solver_logs"])
        _validate_optimization_outcome(incumbent_model)
        incumbent_solution = _extract_solution(
            hospital_ids=hospital_ids,
            zone_ids=zone_ids,
            hospitals=hospitals,
            zones=zones,
            distance=distance,
            base_beds=base_beds,
            demand=demand,
            hospital_name=hospital_name,
            expand_cost=expand_cost,
            travel_cost=travel_cost,
            customer_benefit=customer_benefit,
            added_beds=added_beds,
            model=incumbent_model,
            y=incumbent_vars["y"],
            q=incumbent_vars["q"],
        )
        incumbent_cost_cutoff = incumbent_solution["leader_cost"]
        iteration_history.append(
            {
                "iteration": 0,
                "stage": "incumbent_evaluation",
                "status": incumbent_solution["status_name"],
                "selected_hospitals": incumbent_solution["selected_hospital_ids"],
                "leader_cost": round(incumbent_solution["leader_cost"], 2),
                "customer_benefit": round(incumbent_solution["customer_benefit"], 2),
                "travel_cost": round(incumbent_solution["follower_cost"], 2),
                "runtime_seconds": round(incumbent_solution["runtime_seconds"], 4),
            }
        )
        iteration_log_lines.extend(
            [
                f"Incumbent hubs={incumbent_solution['selected_hospital_ids']}",
                f"Incumbent status={incumbent_solution['status_name']}",
                f"Incumbent customer benefit={incumbent_solution['customer_benefit']:,.2f}",
                f"Incumbent leader cost={incumbent_solution['leader_cost']:,.2f}",
                f"Incumbent travel cost={incumbent_solution['follower_cost']:,.2f}",
            ]
        )

    stage1_model, stage1_vars = _build_bilevel_stage_model(
        hospital_ids=hospital_ids,
        zone_ids=zone_ids,
        base_beds=base_beds,
        demand=demand,
        expand_cost=expand_cost,
        customer_benefit=customer_benefit,
        config=config,
        dual_ub=dual_ub,
        fixed_hubs=set(),
        objective_mode="maximize_benefit",
    )
    stage1_log = _optimize_model(
        stage1_model,
        stage_name="stage1_global_maximize_customer_benefit",
        artifact_root=artifact_root,
        display_interval_seconds=config.display_interval_seconds,
        time_limit_seconds=config.time_limit_seconds,
        log_to_console=log_to_console,
        capture_solver_log=capture_solver_log,
        export_model_file=config.export_model_file,
    )
    if stage1_log["model_file_path"] is not None:
        model_file_path = stage1_log["model_file_path"]
    solver_log_sections.extend(stage1_log["solver_logs"])
    _validate_optimization_outcome(stage1_model)
    stage1_solution = _extract_solution(
        hospital_ids=hospital_ids,
        zone_ids=zone_ids,
        hospitals=hospitals,
        zones=zones,
        distance=distance,
        base_beds=base_beds,
        demand=demand,
        hospital_name=hospital_name,
        expand_cost=expand_cost,
        travel_cost=travel_cost,
        customer_benefit=customer_benefit,
        added_beds=added_beds,
        model=stage1_model,
        y=stage1_vars["y"],
        q=stage1_vars["q"],
    )
    iteration_history.append(
        {
            "iteration": 1,
            "stage": "global_maximize_customer_benefit",
            "status": stage1_solution["status_name"],
            "selected_hospitals": stage1_solution["selected_hospital_ids"],
            "leader_cost": round(stage1_solution["leader_cost"], 2),
            "customer_benefit": round(stage1_solution["customer_benefit"], 2),
            "travel_cost": round(stage1_solution["follower_cost"], 2),
            "runtime_seconds": round(stage1_solution["runtime_seconds"], 4),
        }
    )
    iteration_log_lines.extend(
        [
            "Stage 1: maximize customer benefit over all hub selections.",
            f"Stage 1 optimal status={stage1_solution['status_name']}",
            f"Stage 1 optimal hubs={stage1_solution['selected_hospital_ids']}",
            f"Stage 1 optimal customer benefit={stage1_solution['customer_benefit']:,.2f}",
            f"Stage 1 leader cost={stage1_solution['leader_cost']:,.2f}",
        ]
    )

    benefit_floor = max(0.0, stage1_solution["customer_benefit"] - 1e-6)
    stage2_cutoff = None
    if incumbent_solution is not None and incumbent_solution["customer_benefit"] >= benefit_floor:
        stage2_cutoff = incumbent_cost_cutoff
    stage2_model, stage2_vars = _build_bilevel_stage_model(
        hospital_ids=hospital_ids,
        zone_ids=zone_ids,
        base_beds=base_beds,
        demand=demand,
        expand_cost=expand_cost,
        customer_benefit=customer_benefit,
        config=config,
        dual_ub=dual_ub,
        fixed_hubs=set(),
        objective_mode="minimize_cost",
        min_customer_benefit=benefit_floor,
    )
    stage2_log = _optimize_model(
        stage2_model,
        stage_name="stage2_global_minimize_leader_cost",
        artifact_root=artifact_root,
        display_interval_seconds=config.display_interval_seconds,
        time_limit_seconds=config.time_limit_seconds,
        log_to_console=log_to_console,
        capture_solver_log=capture_solver_log,
        export_model_file=config.export_model_file,
        incumbent_cutoff=stage2_cutoff,
    )
    if stage2_log["model_file_path"] is not None:
        model_file_path = stage2_log["model_file_path"]
    solver_log_sections.extend(stage2_log["solver_logs"])
    _validate_optimization_outcome(stage2_model)
    final_solution = _extract_solution(
        hospital_ids=hospital_ids,
        zone_ids=zone_ids,
        hospitals=hospitals,
        zones=zones,
        distance=distance,
        base_beds=base_beds,
        demand=demand,
        hospital_name=hospital_name,
        expand_cost=expand_cost,
        travel_cost=travel_cost,
        customer_benefit=customer_benefit,
        added_beds=added_beds,
        model=stage2_model,
        y=stage2_vars["y"],
        q=stage2_vars["q"],
    )
    iteration_history.append(
        {
            "iteration": 2,
            "stage": "global_minimize_leader_cost",
            "status": final_solution["status_name"],
            "selected_hospitals": final_solution["selected_hospital_ids"],
            "leader_cost": round(final_solution["leader_cost"], 2),
            "customer_benefit": round(final_solution["customer_benefit"], 2),
            "travel_cost": round(final_solution["follower_cost"], 2),
            "runtime_seconds": round(final_solution["runtime_seconds"], 4),
        }
    )
    iteration_log_lines.extend(
        [
            "Stage 2: minimize leader cost at the globally optimal customer benefit.",
            f"Stage 2 optimal status={final_solution['status_name']}",
            f"Stage 2 optimal hubs={final_solution['selected_hospital_ids']}",
            f"Stage 2 optimal customer benefit={final_solution['customer_benefit']:,.2f}",
            f"Stage 2 optimal leader cost={final_solution['leader_cost']:,.2f}",
            f"Stage 2 optimal travel cost={final_solution['follower_cost']:,.2f}",
            "Branch-and-cut bilevel solve completed.",
        ]
    )

    solver_log_text = "\n".join(iteration_log_lines).strip()
    if solver_log_sections:
        solver_log_text = (
            f"{solver_log_text}\n\n"
            + "\n\n".join(section for section in solver_log_sections if section.strip())
        ).strip()

    selected = []
    for i in hospital_ids:
        if final_solution["expanded_flags"][i] == 1:
            served = final_solution["optimized_load"][i]
            selected.append(
                {
                    "hospital_id": i,
                    "name": hospital_name[i],
                    "added_beds": added_beds,
                    "total_capacity": base_beds[i] + added_beds,
                    "assigned_patients": round(served, 2),
                    "expansion_cost": round(expand_cost[i], 2),
                }
            )

    allocation_df = pd.DataFrame(
        final_solution["allocation"],
        columns=["zone_id", "hospital_id", "assigned_patients"],
    )

    current_primary = []
    current_load = {i: 0.0 for i in hospital_ids}
    current_distance_cost = 0.0
    for j in zone_ids:
        nearest_hospital = min(hospital_ids, key=lambda i: travel_cost[i, j])
        current_load[nearest_hospital] += demand[j]
        current_distance_cost += travel_cost[nearest_hospital, j] * demand[j]
        current_primary.append(
            {
                "zone_id": j,
                "hospital_id": nearest_hospital,
                "assigned_patients": demand[j],
            }
        )

    optimized_primary = []
    optimized_load = {i: 0.0 for i in hospital_ids}
    for j in zone_ids:
        best_hospital = max(hospital_ids, key=lambda i: final_solution["assignment_matrix"][i, j])
        optimized_primary.append(
            {
                "zone_id": j,
                "hospital_id": best_hospital,
                "assigned_patients": round(final_solution["assignment_matrix"][best_hospital, j], 2),
            }
        )
        for i in hospital_ids:
            optimized_load[i] += final_solution["assignment_matrix"][i, j]

    current_primary_df = pd.DataFrame(
        current_primary,
        columns=["zone_id", "hospital_id", "assigned_patients"],
    )
    optimized_primary_df = pd.DataFrame(
        optimized_primary,
        columns=["zone_id", "hospital_id", "assigned_patients"],
    )
    current_routing_matrix = (
        current_primary_df.pivot(index="zone_id", columns="hospital_id", values="assigned_patients")
        .fillna(0.0)
        .reindex(index=zone_ids, columns=hospital_ids, fill_value=0.0)
    )
    optimized_routing_matrix = (
        allocation_df.pivot(index="zone_id", columns="hospital_id", values="assigned_patients")
        .fillna(0.0)
        .reindex(index=zone_ids, columns=hospital_ids, fill_value=0.0)
    )

    hospital_summary = pd.DataFrame(
        {
            "hospital_id": hospital_ids,
            "name": [hospital_name[i] for i in hospital_ids],
            "current_capacity": [base_beds[i] for i in hospital_ids],
            "current_load": [round(current_load[i], 2) for i in hospital_ids],
            "expanded": [final_solution["expanded_flags"][i] for i in hospital_ids],
            "optimized_capacity": [base_beds[i] + added_beds * final_solution["expanded_flags"][i] for i in hospital_ids],
            "optimized_load": [round(optimized_load[i], 2) for i in hospital_ids],
        }
    )
    hospital_summary["current_overload"] = (
        hospital_summary["current_load"] - hospital_summary["current_capacity"]
    ).clip(lower=0)
    hospital_summary["optimized_slack"] = (
        hospital_summary["optimized_capacity"] - hospital_summary["optimized_load"]
    ).round(2)

    result = {
        "status_code": final_solution["status_code"],
        "status_name": final_solution["status_name"],
        "objective_value": float(final_solution["leader_cost"] + final_solution["follower_cost"]),
        "leader_cost": float(final_solution["leader_cost"]),
        "follower_cost": float(final_solution["follower_cost"]),
        "customer_benefit": float(final_solution["customer_benefit"]),
        "runtime_seconds": float(sum(stage["runtime_seconds"] for stage in iteration_history)),
        "best_bound": float(final_solution["best_bound"]),
        "mip_gap": float(final_solution["mip_gap"]),
        "current_solution": {
            "selected_hospital_ids": [],
            "leader_cost": 0.0,
            "follower_cost": float(current_distance_cost),
            "total_cost": float(current_distance_cost),
        },
        "provided_solution": incumbent_solution,
        "selected_hospitals": pd.DataFrame(
            selected,
            columns=[
                "hospital_id",
                "name",
                "added_beds",
                "total_capacity",
                "assigned_patients",
                "expansion_cost",
            ],
        ),
        "allocation": allocation_df,
        "current_primary_assignment": current_primary_df,
        "optimized_primary_assignment": optimized_primary_df,
        "current_routing_matrix": current_routing_matrix,
        "optimized_routing_matrix": optimized_routing_matrix,
        "hospital_summary": hospital_summary,
        "dataset_summary": summarize_dataset(distance, hospitals, zones),
        "config": config.as_dict(),
        "solver_log": solver_log_text,
        "iteration_history": iteration_history,
        "model_file_name": model_file_path.name if model_file_path is not None else None,
        "model_file_path": str(model_file_path) if model_file_path is not None else None,
    }
    result["network"] = _build_network_payload(result, hospitals, zones)

    logger.info(
        "Optimization completed with status=%s leader_cost=%.2f benefit=%.2f runtime=%.3fs gap=%.6f.",
        result["status_name"],
        result["leader_cost"],
        result["customer_benefit"],
        result["runtime_seconds"],
        result["mip_gap"],
    )
    return result


def _build_customer_benefit(distance: pd.DataFrame) -> dict[tuple[str, str], float]:
    max_travel_cost = float(distance["travel_cost"].max())
    return {
        (row.hospital_id, row.zone_id): round(max_travel_cost - float(row.travel_cost), 6)
        for row in distance.itertuples(index=False)
    }


def _build_bilevel_stage_model(
    *,
    hospital_ids: list[str],
    zone_ids: list[str],
    base_beds: dict[str, float],
    demand: dict[str, float],
    expand_cost: dict[str, float],
    customer_benefit: dict[tuple[str, str], float],
    config: OptimizationConfig,
    dual_ub: float,
    fixed_hubs: set[str],
    objective_mode: str,
    min_customer_benefit: float | None = None,
) -> tuple[gp.Model, dict[str, Any]]:
    model = gp.Model(f"Hospital_Bilevel_{objective_mode}")
    y = model.addVars(hospital_ids, vtype=GRB.BINARY, name="expand")
    q = model.addVars(hospital_ids, zone_ids, lb=0.0, name="assign")
    pi = model.addVars(zone_ids, lb=-dual_ub, ub=dual_ub, name="pi")
    mu = model.addVars(hospital_ids, lb=0.0, ub=dual_ub, name="mu")
    w = model.addVars(hospital_ids, lb=0.0, ub=dual_ub, name="w")

    leader_cost = gp.quicksum(expand_cost[i] * y[i] for i in hospital_ids)
    customer_benefit_expr = gp.quicksum(
        customer_benefit[i, j] * q[i, j] for i in hospital_ids for j in zone_ids
    )
    dual_benefit_expr = (
        gp.quicksum(demand[j] * pi[j] for j in zone_ids)
        + gp.quicksum(base_beds[i] * mu[i] for i in hospital_ids)
        + config.added_beds_per_expansion * gp.quicksum(w[i] for i in hospital_ids)
    )

    if objective_mode == "maximize_benefit":
        model.setObjective(customer_benefit_expr, GRB.MAXIMIZE)
    elif objective_mode == "minimize_cost":
        model.setObjective(leader_cost, GRB.MINIMIZE)
    else:
        raise ValueError(f"Unsupported objective_mode: {objective_mode}")

    model.addConstr(y.sum() == config.p_expansions, name="choose_p_hospitals")
    model.addConstr(
        gp.quicksum(base_beds[i] + config.added_beds_per_expansion * y[i] for i in hospital_ids)
        >= float(sum(demand.values())),
        name="system_capacity",
    )
    for hospital_id in fixed_hubs:
        model.addConstr(y[hospital_id] == 1, name=f"fixed_hub_{hospital_id}")

    for zone_id in zone_ids:
        model.addConstr(
            gp.quicksum(q[hospital_id, zone_id] for hospital_id in hospital_ids) == demand[zone_id],
            name=f"demand_{zone_id}",
        )

    for hospital_id in hospital_ids:
        model.addConstr(
            gp.quicksum(q[hospital_id, zone_id] for zone_id in zone_ids)
            <= base_beds[hospital_id] + config.added_beds_per_expansion * y[hospital_id],
            name=f"capacity_{hospital_id}",
        )
        model.addConstr(w[hospital_id] <= dual_ub * y[hospital_id], name=f"lin1_{hospital_id}")
        model.addConstr(w[hospital_id] <= mu[hospital_id], name=f"lin2_{hospital_id}")
        model.addConstr(
            w[hospital_id] >= mu[hospital_id] - dual_ub * (1 - y[hospital_id]),
            name=f"lin3_{hospital_id}",
        )
        for zone_id in zone_ids:
            model.addConstr(
                pi[zone_id] + mu[hospital_id] >= customer_benefit[hospital_id, zone_id],
                name=f"dual_feas_{hospital_id}_{zone_id}",
            )

    model.addConstr(customer_benefit_expr == dual_benefit_expr, name="strong_duality")
    if min_customer_benefit is not None:
        model.addConstr(customer_benefit_expr >= min_customer_benefit, name="benefit_floor")

    warm_start_hubs = set(config.fixed_hub_hospital_ids)
    for hospital_id in hospital_ids:
        y[hospital_id].Start = 1.0 if hospital_id in warm_start_hubs else 0.0

    model.update()
    return model, {"y": y, "q": q}


def _optimize_model(
    model: gp.Model,
    *,
    stage_name: str,
    artifact_root: Path,
    display_interval_seconds: int,
    time_limit_seconds: int,
    log_to_console: bool,
    capture_solver_log: bool,
    export_model_file: bool,
    incumbent_cutoff: float | None = None,
) -> dict[str, Any]:
    model.Params.TimeLimit = time_limit_seconds
    model.Params.OutputFlag = 1 if (log_to_console or capture_solver_log) else 0
    model.Params.LogToConsole = 1 if log_to_console else 0
    model.Params.DisplayInterval = display_interval_seconds
    if incumbent_cutoff is not None and model.ModelSense == GRB.MINIMIZE:
        model.Params.Cutoff = incumbent_cutoff + 1e-6

    model_file_path: Path | None = None
    if export_model_file:
        model_file_path = artifact_root / f"{stage_name}.lp"
        model.write(str(model_file_path))

    log_sections: list[str] = []
    log_file_path: Path | None = None
    if capture_solver_log:
        log_file_path = artifact_root / f"{stage_name}.gurobi.log"
        model.Params.LogFile = str(log_file_path)

    model.optimize()

    if capture_solver_log and log_file_path is not None:
        model.Params.LogFile = ""
        stage_log = log_file_path.read_text(encoding="utf-8", errors="ignore")
        log_file_path.unlink(missing_ok=True)
        if stage_log.strip():
            log_sections.append(f"[{stage_name}]\n{stage_log.strip()}")

    return {
        "solver_logs": log_sections,
        "model_file_path": model_file_path,
    }


def _validate_optimization_outcome(model: gp.Model) -> None:
    accepted_statuses = {GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.SUBOPTIMAL, GRB.INTERRUPTED}
    if model.status not in accepted_statuses:
        raise RuntimeError(
            f"Gurobi terminated with status {STATUS_LABELS.get(model.status, str(model.status))}."
        )
    if model.SolCount == 0:
        raise RuntimeError(
            f"Gurobi terminated with status {STATUS_LABELS.get(model.status, str(model.status))} "
            "without a feasible incumbent."
        )


def _extract_solution(
    *,
    hospital_ids: list[str],
    zone_ids: list[str],
    hospitals: pd.DataFrame,
    zones: pd.DataFrame,
    distance: pd.DataFrame,
    base_beds: dict[str, float],
    demand: dict[str, float],
    hospital_name: dict[str, str],
    expand_cost: dict[str, float],
    travel_cost: dict[tuple[str, str], float],
    customer_benefit: dict[tuple[str, str], float],
    added_beds: int,
    model: gp.Model,
    y: Any,
    q: Any,
) -> dict[str, Any]:
    expanded_flags = {hospital_id: int(y[hospital_id].X > 0.5) for hospital_id in hospital_ids}
    assignment_matrix = {
        (hospital_id, zone_id): float(q[hospital_id, zone_id].X)
        for hospital_id in hospital_ids
        for zone_id in zone_ids
    }
    optimized_load = {
        hospital_id: sum(assignment_matrix[hospital_id, zone_id] for zone_id in zone_ids)
        for hospital_id in hospital_ids
    }
    allocation = [
        {
            "zone_id": zone_id,
            "hospital_id": hospital_id,
            "assigned_patients": round(assignment_matrix[hospital_id, zone_id], 2),
        }
        for zone_id in zone_ids
        for hospital_id in hospital_ids
        if assignment_matrix[hospital_id, zone_id] > 1e-6
    ]
    leader_cost = float(sum(expand_cost[hospital_id] * expanded_flags[hospital_id] for hospital_id in hospital_ids))
    realized_travel_cost = float(
        sum(travel_cost[hospital_id, zone_id] * assignment_matrix[hospital_id, zone_id]
            for hospital_id in hospital_ids for zone_id in zone_ids)
    )
    realized_customer_benefit = float(
        sum(customer_benefit[hospital_id, zone_id] * assignment_matrix[hospital_id, zone_id]
            for hospital_id in hospital_ids for zone_id in zone_ids)
    )
    return {
        "status_code": model.status,
        "status_name": STATUS_LABELS.get(model.status, str(model.status)),
        "runtime_seconds": float(model.Runtime),
        "best_bound": float(model.ObjBound),
        "mip_gap": float(model.MIPGap if model.IsMIP else 0.0),
        "leader_cost": leader_cost,
        "follower_cost": realized_travel_cost,
        "customer_benefit": realized_customer_benefit,
        "selected_hospital_ids": [hospital_id for hospital_id in hospital_ids if expanded_flags[hospital_id] == 1],
        "expanded_flags": expanded_flags,
        "optimized_load": optimized_load,
        "assignment_matrix": assignment_matrix,
        "allocation": allocation,
    }


def serialize_result(result: dict[str, Any]) -> dict[str, Any]:
    hospital_summary = result["hospital_summary"].copy()
    hospital_summary["current_utilization"] = (
        hospital_summary["current_load"] / hospital_summary["current_capacity"]
    ).replace([np.inf, -np.inf], np.nan)
    hospital_summary["optimized_utilization"] = (
        hospital_summary["optimized_load"] / hospital_summary["optimized_capacity"]
    ).replace([np.inf, -np.inf], np.nan)
    provided_solution = result.get("provided_solution")
    current_solution = result["current_solution"]
    comparison = {
        "current_hubs": current_solution["selected_hospital_ids"],
        "current_total_cost": round(current_solution["total_cost"], 2),
        "current_leader_cost": round(current_solution["leader_cost"], 2),
        "current_travel_cost": round(current_solution["follower_cost"], 2),
        "optimal_hubs": result["selected_hospitals"]["hospital_id"].astype(str).tolist(),
        "optimal_total_cost": round(result["leader_cost"] + result["follower_cost"], 2),
        "optimal_leader_cost": round(result["leader_cost"], 2),
        "optimal_travel_cost": round(result["follower_cost"], 2),
    }
    if provided_solution is not None:
        comparison["provided_hubs"] = provided_solution["selected_hospital_ids"]
        comparison["provided_total_cost"] = round(
            provided_solution["leader_cost"] + provided_solution["follower_cost"], 2
        )
        comparison["provided_leader_cost"] = round(provided_solution["leader_cost"], 2)
        comparison["provided_travel_cost"] = round(provided_solution["follower_cost"], 2)
    else:
        comparison["provided_hubs"] = []
        comparison["provided_total_cost"] = None
        comparison["provided_leader_cost"] = None
        comparison["provided_travel_cost"] = None

    return {
        "status": {
            "code": result["status_code"],
            "name": result["status_name"],
        },
        "config": result["config"],
        "dataset_summary": result["dataset_summary"],
        "metrics": {
            "objective_value": round(result["objective_value"], 2),
            "leader_cost": round(result["leader_cost"], 2),
            "follower_cost": round(result["follower_cost"], 2),
            "customer_benefit": round(result["customer_benefit"], 2),
            "runtime_seconds": round(result["runtime_seconds"], 4),
            "best_bound": round(result["best_bound"], 2),
            "mip_gap": round(result["mip_gap"], 8),
        },
        "selected_hospitals": dataframe_records(result["selected_hospitals"]),
        "allocation": dataframe_records(result["allocation"]),
        "current_primary_assignment": dataframe_records(result["current_primary_assignment"]),
        "optimized_primary_assignment": dataframe_records(result["optimized_primary_assignment"]),
        "hospital_summary": dataframe_records(hospital_summary),
        "routing_matrices": {
            "current": matrix_payload(result["current_routing_matrix"]),
            "optimized": matrix_payload(result["optimized_routing_matrix"]),
        },
        "network": result["network"],
        "artifacts": {
            "model_file_name": result["model_file_name"],
            "model_file_path": result["model_file_path"],
        },
        "comparison": comparison,
        "iteration_history": result["iteration_history"],
        "solver_log": result["solver_log"],
    }


def dataframe_preview(frame: pd.DataFrame, sample_rows: int = 5) -> dict[str, Any]:
    return {
        "row_count": int(len(frame)),
        "columns": [str(column) for column in frame.columns.tolist()],
        "rows": dataframe_records(frame.head(sample_rows)),
    }


def dataframe_records(frame: pd.DataFrame, decimals: int = 4) -> list[dict[str, Any]]:
    if frame.empty:
        return []

    serializable = frame.copy()
    numeric_columns = serializable.select_dtypes(include=["number"]).columns
    if len(numeric_columns) > 0:
        serializable.loc[:, numeric_columns] = serializable.loc[:, numeric_columns].round(decimals)
    serializable = serializable.replace({np.nan: None})
    return serializable.to_dict(orient="records")


def matrix_payload(matrix: pd.DataFrame, decimals: int = 2) -> dict[str, Any]:
    rounded = matrix.round(decimals)
    values = [[float(value) for value in row] for row in rounded.to_numpy()]
    return {
        "rows": [str(index_value) for index_value in rounded.index.tolist()],
        "columns": [str(column) for column in rounded.columns.tolist()],
        "values": values,
    }


def _read_csv_text_or_disk(csv_text: str | None, file_path: Path) -> pd.DataFrame:
    if csv_text is not None and csv_text.strip():
        return pd.read_csv(StringIO(csv_text))
    return pd.read_csv(file_path)


def _normalize_and_validate(
    distance: pd.DataFrame,
    hospitals: pd.DataFrame,
    zones: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    frames = {
        "distance": distance.copy(),
        "hospitals": hospitals.copy(),
        "zones": zones.copy(),
    }

    for frame in frames.values():
        frame.columns = frame.columns.str.strip().str.lower()

    _validate_required_columns(frames["distance"], REQUIRED_DISTANCE_COLUMNS, "distance_matrix.csv")
    _validate_required_columns(frames["hospitals"], REQUIRED_HOSPITAL_COLUMNS, "hospitals.csv")
    _validate_required_columns(frames["zones"], REQUIRED_ZONE_COLUMNS, "zones.csv")

    _coerce_and_validate_ids(frames)
    _coerce_and_validate_numeric(frames)
    _validate_cross_references(frames["distance"], frames["hospitals"], frames["zones"])

    return frames["distance"], frames["hospitals"], frames["zones"]


def _validate_required_columns(frame: pd.DataFrame, required: set[str], label: str) -> None:
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{label} is missing columns: {sorted(missing)}")


def _coerce_and_validate_ids(frames: dict[str, pd.DataFrame]) -> None:
    for frame_name, id_columns in ID_COLUMNS.items():
        frame = frames[frame_name]
        for column in id_columns:
            frame[column] = frame[column].astype(str).str.strip()
            if (frame[column] == "").any():
                raise ValueError(f"{frame_name} contains blank identifiers in column '{column}'.")

    if frames["hospitals"]["hospital_id"].duplicated().any():
        duplicates = frames["hospitals"].loc[
            frames["hospitals"]["hospital_id"].duplicated(), "hospital_id"
        ].tolist()
        raise ValueError(f"hospitals.csv contains duplicate hospital_id values: {duplicates}")

    if frames["zones"]["zone_id"].duplicated().any():
        duplicates = frames["zones"].loc[
            frames["zones"]["zone_id"].duplicated(), "zone_id"
        ].tolist()
        raise ValueError(f"zones.csv contains duplicate zone_id values: {duplicates}")

    duplicate_pairs = frames["distance"].duplicated(subset=["zone_id", "hospital_id"])
    if duplicate_pairs.any():
        duplicates = frames["distance"].loc[duplicate_pairs, ["zone_id", "hospital_id"]]
        pairs = duplicates.astype(str).agg(" -> ".join, axis=1).tolist()
        raise ValueError(f"distance_matrix.csv contains duplicate (zone_id, hospital_id) pairs: {pairs}")


def _coerce_and_validate_numeric(frames: dict[str, pd.DataFrame]) -> None:
    for frame_name, columns in NUMERIC_COLUMNS.items():
        frame = frames[frame_name]
        for column in columns:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
            if not np.isfinite(frame[column]).all():
                raise ValueError(f"{frame_name} column '{column}' contains non-finite values.")
            if (frame[column] < 0).any():
                raise ValueError(f"{frame_name} column '{column}' contains negative values.")

    if frames["hospitals"]["name"].astype(str).str.strip().eq("").any():
        raise ValueError("hospitals.csv contains blank hospital names.")


def _validate_cross_references(
    distance: pd.DataFrame,
    hospitals: pd.DataFrame,
    zones: pd.DataFrame,
) -> None:
    hospital_ids = hospitals["hospital_id"].tolist()
    zone_ids = zones["zone_id"].tolist()

    distance_hospitals = set(distance["hospital_id"])
    distance_zones = set(distance["zone_id"])
    hospital_set = set(hospital_ids)
    zone_set = set(zone_ids)

    missing_hospitals = sorted(hospital_set.difference(distance_hospitals))
    missing_zones = sorted(zone_set.difference(distance_zones))
    extra_hospitals = sorted(distance_hospitals.difference(hospital_set))
    extra_zones = sorted(distance_zones.difference(zone_set))

    if missing_hospitals:
        raise ValueError(f"distance_matrix.csv has no rows for hospitals: {missing_hospitals}")
    if missing_zones:
        raise ValueError(f"distance_matrix.csv has no rows for zones: {missing_zones}")
    if extra_hospitals:
        raise ValueError(f"distance_matrix.csv references unknown hospitals: {extra_hospitals}")
    if extra_zones:
        raise ValueError(f"distance_matrix.csv references unknown zones: {extra_zones}")

    expected_pairs = {(zone_id, hospital_id) for zone_id in zone_ids for hospital_id in hospital_ids}
    actual_pairs = set(distance[["zone_id", "hospital_id"]].itertuples(index=False, name=None))
    missing_pairs = sorted(expected_pairs.difference(actual_pairs))
    extra_pairs = sorted(actual_pairs.difference(expected_pairs))

    if missing_pairs:
        preview = [f"{zone_id}->{hospital_id}" for zone_id, hospital_id in missing_pairs[:10]]
        raise ValueError(
            "distance_matrix.csv does not define the full zone-hospital cartesian product. "
            f"Missing pairs include: {preview}"
        )
    if extra_pairs:
        preview = [f"{zone_id}->{hospital_id}" for zone_id, hospital_id in extra_pairs[:10]]
        raise ValueError(f"distance_matrix.csv contains extra zone-hospital pairs: {preview}")


def _coordinates_available(hospitals: pd.DataFrame, zones: pd.DataFrame) -> bool:
    if not VISUAL_COLUMNS["hospitals"].issubset(hospitals.columns):
        return False
    if not VISUAL_COLUMNS["zones"].issubset(zones.columns):
        return False

    for frame in (hospitals, zones):
        for column in ("x_coord", "y_coord"):
            numeric = pd.to_numeric(frame[column], errors="coerce")
            if numeric.isna().any():
                return False
    return True


def _build_network_payload(
    result: dict[str, Any],
    hospitals: pd.DataFrame,
    zones: pd.DataFrame,
) -> dict[str, Any]:
    if not _coordinates_available(hospitals, zones):
        return {"enabled": False, "reason": "Coordinate columns are unavailable for visualization."}

    hospital_summary = result["hospital_summary"]
    hospital_points = hospitals.merge(hospital_summary, on=["hospital_id", "name"], how="left")
    zone_points = zones[["zone_id", "x_coord", "y_coord", "patient_demand"]].copy()

    current_edges = zone_points.merge(result["current_primary_assignment"], on="zone_id", how="left")
    current_edges = current_edges.merge(
        hospital_points[["hospital_id", "x_coord", "y_coord", "expanded"]],
        on="hospital_id",
        how="left",
        suffixes=("_zone", "_hospital"),
    )

    optimized_edges = zone_points.merge(result["allocation"], on="zone_id", how="left")
    optimized_edges = optimized_edges.merge(
        hospital_points[["hospital_id", "x_coord", "y_coord", "expanded"]],
        on="hospital_id",
        how="left",
        suffixes=("_zone", "_hospital"),
    )

    return {
        "enabled": True,
        "zones": dataframe_records(zone_points),
        "hospitals": dataframe_records(
            hospital_points[
                [
                    "hospital_id",
                    "name",
                    "x_coord",
                    "y_coord",
                    "current_capacity",
                    "current_load",
                    "expanded",
                    "optimized_capacity",
                    "optimized_load",
                    "current_overload",
                    "optimized_slack",
                ]
            ]
        ),
        "current_edges": dataframe_records(
            current_edges[
                [
                    "zone_id",
                    "hospital_id",
                    "assigned_patients",
                    "x_coord_zone",
                    "y_coord_zone",
                    "x_coord_hospital",
                    "y_coord_hospital",
                    "expanded",
                ]
            ]
        ),
        "optimized_edges": dataframe_records(
            optimized_edges[
                [
                    "zone_id",
                    "hospital_id",
                    "assigned_patients",
                    "x_coord_zone",
                    "y_coord_zone",
                    "x_coord_hospital",
                    "y_coord_hospital",
                    "expanded",
                ]
            ]
        ),
    }
