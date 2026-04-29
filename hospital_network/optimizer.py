from __future__ import annotations

from dataclasses import asdict, dataclass, field
from io import StringIO
from pathlib import Path
from time import perf_counter
from typing import Any
import logging
import math
from time import perf_counter


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
    p_expansions: int = 2
    added_beds_per_expansion: int = 5000
    dual_ub_factor: float = 2.0
    time_limit_seconds: int = 30
    fixed_hub_hospital_ids: tuple[str, ...] = field(default_factory=tuple)
    show_solver_log: bool = True
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
        normalized_ids = tuple(
            str(hospital_id).strip() for hospital_id in self.fixed_hub_hospital_ids
        )
        if any(not hospital_id for hospital_id in normalized_ids):
            raise ValueError("fixed_hub_hospital_ids cannot contain blank identifiers.")
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("fixed_hub_hospital_ids cannot contain duplicates.")
        object.__setattr__(self, "fixed_hub_hospital_ids", normalized_ids)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["fixed_hub_hospital_ids"] = list(self.fixed_hub_hospital_ids)
        return payload


def load_dataset_from_disk(
    base_dir: str | Path = ".",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
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
    hospitals = _read_csv_text_or_disk(
        hospitals_csv, base_path / DEFAULT_HOSPITALS_FILE
    )
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


def summarize_dataset(
    distance: pd.DataFrame, hospitals: pd.DataFrame, zones: pd.DataFrame
) -> dict[str, Any]:
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


def solve_bilevel_optimization(distance, hospitals, zones, config, **kwargs):

    import pandas as pd
    import gurobipy as gp
    from gurobipy import GRB
    from time import perf_counter
    from itertools import combinations

    start_time = perf_counter()

    # ---------------------------
    # LOGGING
    # ---------------------------
    solver_logs = []
    iteration_counter = 0
    best_cost_so_far = float("inf")

    # ---------------------------
    # SAFE EMPTY RESULT
    # ---------------------------
    def empty_result(status="ERROR", msg="Unexpected failure"):
        return {
            "status_code": -1,
            "status_name": status,
            "message": msg,
            "objective_value": 0.0,
            "leader_cost": 0.0,
            "follower_cost": 0.0,
            "runtime_seconds": 0.0,
            "best_bound": None,
            "mip_gap": None,
            "node_count": 0,
            "selected_hospitals": pd.DataFrame(),
            "selected_hospital_ids": [],
            "allocation": pd.DataFrame(),
            "hospital_summary": pd.DataFrame(),
            "current_primary_assignment": pd.DataFrame(),
            "optimized_primary_assignment": pd.DataFrame(),
            "current_routing_matrix": pd.DataFrame(),
            "optimized_routing_matrix": pd.DataFrame(),
            "network": {"enabled": False},
            "comparison": {},
            "iteration_history": [],
            "solver_log": "",
            "config": config.model_dump() if hasattr(config, "model_dump") else {},
            "dataset_summary": {},
            "model_file_name": None,
            "model_file_path": None,
        }

    try:
        # ---------------------------
        # DATA
        # ---------------------------
        hospital_ids = hospitals["hospital_id"].tolist()
        zone_ids = zones["zone_id"].tolist()

        base_beds = dict(zip(hospital_ids, hospitals["existing_beds"]))
        demand = dict(zip(zone_ids, zones["patient_demand"]))
        names = dict(zip(hospital_ids, hospitals["name"]))

        added = config.added_beds_per_expansion

        expand_cost = {
            r.hospital_id: r.fixed_open_expand_cost + added * r.cost_per_added_bed
            for r in hospitals.itertuples()
        }

        travel_cost = {
            (r.hospital_id, r.zone_id): r.travel_cost
            for r in distance.itertuples()
        }

        provided_hubs = set(config.fixed_hub_hospital_ids)

        def solve_follower(S):
            m = gp.Model()
            m.Params.OutputFlag = 0

            q = m.addVars(hospital_ids, zone_ids, lb=0)

            for j in zone_ids:
                m.addConstr(sum(q[i, j] for i in hospital_ids) == demand[j])

            for i in hospital_ids:
                cap = base_beds[i] + (added if i in S else 0)
                m.addConstr(sum(q[i, j] for j in zone_ids) <= cap)

            m.setObjective(
                sum(travel_cost[i, j] * q[i, j]
                    for i in hospital_ids for j in zone_ids),
                GRB.MINIMIZE
            )

            m.optimize()

            if m.SolCount == 0:
                return float("inf"), {}

            alloc = {(i, j): q[i, j].X for i in hospital_ids for j in zone_ids}
            return m.ObjVal, alloc

        def build_assignment_dataframe(alloc):
            return pd.DataFrame(
                [
                    {"hospital_id": i, "zone_id": j, "assigned_patients": float(v)}
                    for (i, j), v in alloc.items() if v > 1e-6
                ]
            )

        def build_routing_matrix(alloc):
            matrix = pd.DataFrame(0.0, index=zone_ids, columns=hospital_ids)
            for (i, j), v in alloc.items():
                matrix.loc[j, i] = v
            return matrix

        # baseline_travel, baseline_alloc = solve_follower(set())
        # ---------------------------
# BASELINE (REALISTIC - NO OPTIMIZATION)
# ---------------------------
        baseline_alloc = {}

        for j in zone_ids:
            # assign all demand to nearest hospital
            best_i = min(hospital_ids, key=lambda i: travel_cost[(i, j)])
            baseline_alloc[(best_i, j)] = demand[j]

        baseline_assignment_df = build_assignment_dataframe(baseline_alloc)
        baseline_routing = build_routing_matrix(baseline_alloc)

        baseline_travel = sum(
            travel_cost[i, j] * v for (i, j), v in baseline_alloc.items()
        )



        baseline_leader = 0.0
        baseline_assignment_df = build_assignment_dataframe(baseline_alloc)
        baseline_routing = build_routing_matrix(baseline_alloc)

        provided_travel = baseline_travel
        provided_leader = baseline_leader
        provided_assignment_df = baseline_assignment_df
        provided_routing = baseline_routing
        if provided_hubs:
            provided_travel, provided_alloc = solve_follower(provided_hubs)
            provided_leader = sum(expand_cost[i] for i in provided_hubs)
            provided_assignment_df = build_assignment_dataframe(provided_alloc)
            provided_routing = build_routing_matrix(provided_alloc)

        # ---------------------------
        # SEARCH (safe brute-force)
        # ---------------------------
        best_cost = float("inf")
        best_S = None
        best_alloc = {}

        for comb in combinations(hospital_ids, config.p_expansions):

            if perf_counter() - start_time > config.time_limit_seconds:
                solver_logs.append(
                    f"[STOP] Time limit reached at iteration {iteration_counter}"
                )
                break

            iteration_counter += 1
            elapsed = perf_counter() - start_time

            S = set(comb)

            leader = sum(expand_cost[i] for i in S)
            travel, alloc = solve_follower(S)
            total = leader + travel

            # track best seen so far
            if total < best_cost_so_far:
                best_cost_so_far = total

            # iteration log
            solver_logs.append(
                f"[ITER {iteration_counter} | t={elapsed:.2f}s] "
                f"H={list(S)} | Exp={leader:,.2f} | "
                f"Travel={travel:,.2f} | Total={total:,.2f} | "
                f"BestSoFar={best_cost_so_far:,.2f}"
            )

            if total < best_cost:
                best_cost = total
                best_S = S
                best_alloc = alloc

                solver_logs.append(
                    f"[BEST UPDATE] Iter={iteration_counter} | "
                    f"New Best={best_cost:,.2f} | Hubs={list(best_S)}"
                )

        if best_S is None:
            return empty_result("NO_SOLUTION", "No feasible solution")

        # ---------------------------
        # BUILD OUTPUT
        # ---------------------------
        allocation_rows = [
            {"hospital_id": i, "zone_id": j, "assigned_patients": float(v)}
            for (i, j), v in best_alloc.items() if v > 1e-6
        ]
        allocation_df = pd.DataFrame(allocation_rows)

        routing = pd.DataFrame(0.0, index=zone_ids, columns=hospital_ids)
        for (i, j), v in best_alloc.items():
            routing.loc[j, i] = v

        leader_cost_val = sum(expand_cost[i] for i in best_S)
        follower_cost_val = sum(
            travel_cost[i, j] * best_alloc[(i, j)]
            for i in hospital_ids for j in zone_ids
        )

        summary = []

        for i in hospital_ids:

            # ---------------- CURRENT ----------------
            if not baseline_assignment_df.empty:
                current_load = baseline_assignment_df.loc[
                    baseline_assignment_df["hospital_id"] == i,
                    "assigned_patients"
                ].sum()
                current_assignment_parts = [
                    f"{row.zone_id}({int(row.assigned_patients) if float(row.assigned_patients).is_integer() else round(row.assigned_patients, 2)})"
                    for row in baseline_assignment_df.loc[
                        baseline_assignment_df["hospital_id"] == i
                    ].itertuples(index=False)
                ]
                current_assignment = ", ".join(current_assignment_parts)
            else:
                current_load = 0.0
                current_assignment = ""

            current_capacity = base_beds[i]
            current_overload = max(0.0, current_load - current_capacity)
            current_slack = max(0.0, current_capacity - current_load)
            if current_overload > 0:
                current_status = "Overcrowded"
            elif current_slack > 0:
                current_status = "Undercrowded"
            else:
                current_status = "Balanced"

            # ---------------- OPTIMIZED ----------------
            optimized_load = sum(best_alloc.get((i, j), 0.0) for j in zone_ids)
            optimized_capacity = base_beds[i] + (added if i in best_S else 0)

            # ---------------- SUMMARY ----------------
            summary.append({
                "hospital_id": i,
                "name": names[i],

                "current_capacity": current_capacity,
                "current_load": current_load,
                "current_overload": current_overload,
                "current_slack": current_slack,
                "current_status": current_status,
                "current_assignment": current_assignment,

                "optimized_capacity": optimized_capacity,
                "optimized_load": optimized_load,
                "optimized_slack": max(0.0, optimized_capacity - optimized_load),

                "expanded": int(i in best_S),
            })

        hospital_summary_df = pd.DataFrame(summary)

        # ---------------------------
        # FINAL LOG
        # ---------------------------
        total_time = perf_counter() - start_time
        solver_logs.append(
            f"[FINAL] Time={total_time:.2f}s | Best Cost={best_cost:,.2f} | "
            f"Iterations={iteration_counter}"
        )

        # ---------------------------
        # RETURN
        # ---------------------------
        current_primary_assignment = baseline_assignment_df
        current_routing_matrix = baseline_routing

        provided_leader_cost = provided_leader
        provided_travel_cost = provided_travel
        provided_total_cost = provided_leader_cost + provided_travel_cost

        return {
            "status_code": 2,
            "status_name": "OPTIMAL",
            "message": "Solution computed",

            "objective_value": float(best_cost),
            "leader_cost": float(leader_cost_val),
            "follower_cost": float(follower_cost_val),

            "runtime_seconds": float(total_time),

            "best_bound": float(best_cost),
            "mip_gap": None,  # not available in brute force
            "node_count": iteration_counter,

            "selected_hospitals": hospitals[hospitals["hospital_id"].isin(best_S)].copy(),
            "selected_hospital_ids": list(best_S),

            "allocation": allocation_df,
            "hospital_summary": hospital_summary_df,

            "current_primary_assignment": current_primary_assignment,
            "optimized_primary_assignment": allocation_df,

            "current_routing_matrix": current_routing_matrix,
            "optimized_routing_matrix": routing,

            "network": _build_network_payload(
                {
                    "hospital_summary": hospital_summary_df,
                    "current_primary_assignment": current_primary_assignment,
                    "allocation": allocation_df,
                    "comparison": {
                        "provided_hubs": list(provided_hubs),
                        "optimal_hubs": list(best_S),
                    },
                },
                hospitals,
                zones,
            ),

            "comparison": {
                "current_total_cost": float(baseline_travel),
                "current_leader_cost": float(baseline_leader),
                "current_travel_cost": float(baseline_travel),
                "provided_hubs": list(provided_hubs),
                "provided_total_cost": float(provided_total_cost),
                "provided_leader_cost": float(provided_leader_cost),
                "provided_travel_cost": float(provided_travel_cost),
                "optimal_total_cost": float(best_cost),
                "optimal_leader_cost": float(leader_cost_val),
                "optimal_travel_cost": float(follower_cost_val),
                "optimal_hubs": list(best_S),
            },

            "iteration_history": [],
            "solver_log": "\n".join(solver_logs),

            "config": config.model_dump() if hasattr(config, "model_dump") else {},
            "dataset_summary": {
                "hospital_count": len(hospital_ids),
                "zone_count": len(zone_ids),
            },

            "model_file_name": None,
            "model_file_path": None,
        }

    except Exception as e:
        return empty_result("EXCEPTION", str(e))


def _build_customer_benefit(distance: pd.DataFrame) -> dict[tuple[str, str], float]:
    max_travel_cost = float(distance["travel_cost"].max())
    return {
        (row.hospital_id, row.zone_id): round(
            max_travel_cost - float(row.travel_cost), 6
        )
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
        gp.quicksum(
            base_beds[i] + config.added_beds_per_expansion * y[i] for i in hospital_ids
        )
        >= float(sum(demand.values())),
        name="system_capacity",
    )
    for hospital_id in fixed_hubs:
        model.addConstr(y[hospital_id] == 1, name=f"fixed_hub_{hospital_id}")

    for zone_id in zone_ids:
        model.addConstr(
            gp.quicksum(q[hospital_id, zone_id] for hospital_id in hospital_ids)
            == demand[zone_id],
            name=f"demand_{zone_id}",
        )

    for hospital_id in hospital_ids:
        model.addConstr(
            gp.quicksum(q[hospital_id, zone_id] for zone_id in zone_ids)
            <= base_beds[hospital_id]
            + config.added_beds_per_expansion * y[hospital_id],
            name=f"capacity_{hospital_id}",
        )
        model.addConstr(
            w[hospital_id] <= dual_ub * y[hospital_id], name=f"lin1_{hospital_id}"
        )
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
        model.addConstr(
            customer_benefit_expr >= min_customer_benefit, name="benefit_floor"
        )

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
    expanded_flags = {
        hospital_id: int(y[hospital_id].X > 0.5) for hospital_id in hospital_ids
    }
    assignment_matrix = {
        (hospital_id, zone_id): float(q[hospital_id, zone_id].X)
        for hospital_id in hospital_ids
        for zone_id in zone_ids
    }
    optimized_load = {
        hospital_id: sum(
            assignment_matrix[hospital_id, zone_id] for zone_id in zone_ids
        )
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
    leader_cost = float(
        sum(
            expand_cost[hospital_id] * expanded_flags[hospital_id]
            for hospital_id in hospital_ids
        )
    )
    realized_travel_cost = float(
        sum(
            travel_cost[hospital_id, zone_id] * assignment_matrix[hospital_id, zone_id]
            for hospital_id in hospital_ids
            for zone_id in zone_ids
        )
    )
    realized_customer_benefit = float(
        sum(
            customer_benefit[hospital_id, zone_id]
            * assignment_matrix[hospital_id, zone_id]
            for hospital_id in hospital_ids
            for zone_id in zone_ids
        )
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
        "selected_hospital_ids": [
            hospital_id
            for hospital_id in hospital_ids
            if expanded_flags[hospital_id] == 1
        ],
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

    # Use comparison data already created in solve_bilevel_optimization
    comparison = result.get("comparison", {})

    return {
        "status": {
            "code": result["status_code"],
            "name": result["status_name"],
        },
        "config": result["config"],
        "dataset_summary": result["dataset_summary"],
        "metrics": {
            "objective_value": round(result.get("objective_value") or 0.0, 2),
            "leader_cost": round(result.get("leader_cost") or 0.0, 2),
            "follower_cost": round(result.get("follower_cost") or 0.0, 2),
            "customer_benefit": round(result.get("customer_benefit") or 0.0, 2),
            "runtime_seconds": round(result.get("runtime_seconds") or 0.0, 4),
            "best_bound": round(result.get("best_bound") or 0.0, 2),
            "mip_gap": round(result.get("mip_gap") or 0.0, 8),
        },
        "selected_hospitals": dataframe_records(result["selected_hospitals"]),
        "allocation": dataframe_records(result["allocation"]),
        "current_primary_assignment": dataframe_records(
            result["current_primary_assignment"]
        ),
        "optimized_primary_assignment": dataframe_records(
            result["optimized_primary_assignment"]
        ),
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
        serializable.loc[:, numeric_columns] = serializable.loc[
            :, numeric_columns
        ].round(decimals)
    serializable = serializable.replace({np.nan: None})
    return serializable.to_dict(orient="records")


def matrix_payload(matrix: pd.DataFrame, decimals: int = 2) -> dict[str, Any]:
    if matrix.empty or matrix.shape[0] == 0 or matrix.shape[1] == 0:
        return {
            "rows": [],
            "columns": [],
            "values": [],
        }
    
    try:
        # Ensure numeric data
        numeric_matrix = matrix.copy()
        numeric_matrix = numeric_matrix.astype(float)
        rounded = numeric_matrix.round(decimals)
        values = [[float(value) for value in row] for row in rounded.to_numpy()]
    except (ValueError, TypeError):
        # If conversion fails, return zeros with same shape
        values = [[0.0 for _ in range(matrix.shape[1])] for _ in range(matrix.shape[0])]
    
    return {
        "rows": [str(index_value) for index_value in matrix.index.tolist()],
        "columns": [str(column) for column in matrix.columns.tolist()],
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

    _validate_required_columns(
        frames["distance"], REQUIRED_DISTANCE_COLUMNS, "distance_matrix.csv"
    )
    _validate_required_columns(
        frames["hospitals"], REQUIRED_HOSPITAL_COLUMNS, "hospitals.csv"
    )
    _validate_required_columns(frames["zones"], REQUIRED_ZONE_COLUMNS, "zones.csv")

    _coerce_and_validate_ids(frames)
    _coerce_and_validate_numeric(frames)
    _validate_cross_references(frames["distance"], frames["hospitals"], frames["zones"])

    return frames["distance"], frames["hospitals"], frames["zones"]


def _validate_required_columns(
    frame: pd.DataFrame, required: set[str], label: str
) -> None:
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{label} is missing columns: {sorted(missing)}")


def _coerce_and_validate_ids(frames: dict[str, pd.DataFrame]) -> None:
    for frame_name, id_columns in ID_COLUMNS.items():
        frame = frames[frame_name]
        for column in id_columns:
            frame[column] = frame[column].astype(str).str.strip()
            if (frame[column] == "").any():
                raise ValueError(
                    f"{frame_name} contains blank identifiers in column '{column}'."
                )

    if frames["hospitals"]["hospital_id"].duplicated().any():
        duplicates = (
            frames["hospitals"]
            .loc[frames["hospitals"]["hospital_id"].duplicated(), "hospital_id"]
            .tolist()
        )
        raise ValueError(
            f"hospitals.csv contains duplicate hospital_id values: {duplicates}"
        )

    if frames["zones"]["zone_id"].duplicated().any():
        duplicates = (
            frames["zones"]
            .loc[frames["zones"]["zone_id"].duplicated(), "zone_id"]
            .tolist()
        )
        raise ValueError(f"zones.csv contains duplicate zone_id values: {duplicates}")

    duplicate_pairs = frames["distance"].duplicated(subset=["zone_id", "hospital_id"])
    if duplicate_pairs.any():
        duplicates = frames["distance"].loc[duplicate_pairs, ["zone_id", "hospital_id"]]
        pairs = duplicates.astype(str).agg(" -> ".join, axis=1).tolist()
        raise ValueError(
            f"distance_matrix.csv contains duplicate (zone_id, hospital_id) pairs: {pairs}"
        )


def _coerce_and_validate_numeric(frames: dict[str, pd.DataFrame]) -> None:
    for frame_name, columns in NUMERIC_COLUMNS.items():
        frame = frames[frame_name]
        for column in columns:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
            if not np.isfinite(frame[column]).all():
                raise ValueError(
                    f"{frame_name} column '{column}' contains non-finite values."
                )
            if (frame[column] < 0).any():
                raise ValueError(
                    f"{frame_name} column '{column}' contains negative values."
                )

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
        raise ValueError(
            f"distance_matrix.csv has no rows for hospitals: {missing_hospitals}"
        )
    if missing_zones:
        raise ValueError(f"distance_matrix.csv has no rows for zones: {missing_zones}")
    if extra_hospitals:
        raise ValueError(
            f"distance_matrix.csv references unknown hospitals: {extra_hospitals}"
        )
    if extra_zones:
        raise ValueError(f"distance_matrix.csv references unknown zones: {extra_zones}")

    expected_pairs = {
        (zone_id, hospital_id) for zone_id in zone_ids for hospital_id in hospital_ids
    }
    actual_pairs = set(
        distance[["zone_id", "hospital_id"]].itertuples(index=False, name=None)
    )
    missing_pairs = sorted(expected_pairs.difference(actual_pairs))
    extra_pairs = sorted(actual_pairs.difference(expected_pairs))

    if missing_pairs:
        preview = [
            f"{zone_id}->{hospital_id}" for zone_id, hospital_id in missing_pairs[:10]
        ]
        raise ValueError(
            "distance_matrix.csv does not define the full zone-hospital cartesian product. "
            f"Missing pairs include: {preview}"
        )
    if extra_pairs:
        preview = [
            f"{zone_id}->{hospital_id}" for zone_id, hospital_id in extra_pairs[:10]
        ]
        raise ValueError(
            f"distance_matrix.csv contains extra zone-hospital pairs: {preview}"
        )


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
        return {
            "enabled": False,
            "reason": "Coordinate columns are unavailable for visualization.",
        }

    hospital_summary = result["hospital_summary"]
    hospital_points = hospitals.merge(
        hospital_summary, on=["hospital_id", "name"], how="left"
    )
    zone_points = zones[["zone_id", "x_coord", "y_coord", "patient_demand"]].copy()

    provided_hubs = set(result["comparison"].get("provided_hubs", []))
    optimal_hubs = set(result["comparison"].get("optimal_hubs", []))

    hospital_points = hospital_points.assign(
        current_hub=hospital_points["hospital_id"].isin(provided_hubs).astype(int),
        optimal_hub=hospital_points["hospital_id"].isin(optimal_hubs).astype(int),
    )

    current_edges = zone_points.merge(
        result["current_primary_assignment"], on="zone_id", how="left"
    )
    current_edges = current_edges.merge(
        hospital_points[["hospital_id", "x_coord", "y_coord", "current_hub"]],
        on="hospital_id",
        how="left",
        suffixes=("_zone", "_hospital"),
    )

    optimized_edges = zone_points.merge(result["allocation"], on="zone_id", how="left")
    optimized_edges = optimized_edges.merge(
        hospital_points[["hospital_id", "x_coord", "y_coord", "optimal_hub"]],
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
                    "current_hub",
                    "expanded",
                    "optimized_capacity",
                    "optimized_load",
                    "optimal_hub",
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
                    "current_hub",
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
                    "optimal_hub",
                ]
            ]
        ),
    }
