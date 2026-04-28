import time

import matplotlib.pyplot as plt

from hospital_network.optimizer import (
    OptimizationConfig,
    load_dataset_from_disk,
    solve_bilevel_optimization,
)


P = 7
X = 1500
DUAL_UB_FACTOR = 2.0
TIME_LIMIT = 60
SHOW_SOLVER_LOG = True
EXPORT_MODEL_FILE = True
DISPLAY_INTERVAL = 1


def main():
    start = time.time()
    config = OptimizationConfig(
        p_expansions=P,
        added_beds_per_expansion=X,
        dual_ub_factor=DUAL_UB_FACTOR,
        time_limit_seconds=TIME_LIMIT,
        show_solver_log=SHOW_SOLVER_LOG,
        export_model_file=EXPORT_MODEL_FILE,
        display_interval_seconds=DISPLAY_INTERVAL,
    )

    distance, hospitals, zones = load_dataset_from_disk()
    result = solve_bilevel_optimization(
        distance,
        hospitals,
        zones,
        config,
        artifact_dir=".",
        log_to_console=SHOW_SOLVER_LOG,
        capture_solver_log=False,
    )

    print("\nBILEVEL HOSPITAL EXPANSION MODEL")
    print(f"P = {P}")
    print(f"X = {X} added beds per expanded hospital")
    print(f"Total objective value = {result['objective_value']:,.2f}")
    print(f"Upper-level expansion cost = {result['leader_cost']:,.2f}")
    print(f"Lower-level travel cost = {result['follower_cost']:,.2f}\n")
    print(f"Solver runtime = {result['runtime_seconds']:.4f} seconds")
    print(f"Best bound = {result['best_bound']:,.2f}")
    print(f"Final MIP gap = {result['mip_gap']:.8f}\n")
    print(f"Status = {result['status_name']}")
    print("Selected hospitals for expansion:")
    print(result["selected_hospitals"].to_string(index=False))
    print(f"\nNonzero zone-to-hospital allocations = {len(result['allocation'])}")

    hospital_summary = result["hospital_summary"]
    print("\nCurrent system overload by hospital:")
    print(
        hospital_summary.loc[hospital_summary["current_overload"] > 0, [
            "hospital_id",
            "name",
            "current_capacity",
            "current_load",
            "current_overload",
        ]].to_string(index=False)
    )

    current_matrix_path = "current_routing_matrix.csv"
    optimized_matrix_path = "optimized_routing_matrix.csv"
    result["current_routing_matrix"].round(2).to_csv(current_matrix_path)
    result["optimized_routing_matrix"].round(2).to_csv(optimized_matrix_path)

    print(f"\nCurrent routing matrix saved to {current_matrix_path}")
    print(result["current_routing_matrix"].round(2).to_string())
    print(f"\nOptimized routing matrix saved to {optimized_matrix_path}")
    print(result["optimized_routing_matrix"].round(2).to_string())

    zones_plot = zones[["zone_id", "x_coord", "y_coord", "patient_demand"]].copy()
    hospitals_plot = hospitals[["hospital_id", "name", "x_coord", "y_coord", "existing_beds"]].copy()
    hospitals_plot = hospitals_plot.merge(hospital_summary, on=["hospital_id", "name"], how="left")
    current_plot = zones_plot.merge(result["current_primary_assignment"], on="zone_id", how="left")
    current_plot = current_plot.merge(
        hospitals_plot[["hospital_id", "x_coord", "y_coord"]],
        on="hospital_id",
        how="left",
        suffixes=("_zone", "_hospital"),
    )
    optimized_plot = zones_plot.merge(result["optimized_primary_assignment"], on="zone_id", how="left")
    optimized_plot = optimized_plot.merge(
        hospitals_plot[["hospital_id", "x_coord", "y_coord"]],
        on="hospital_id",
        how="left",
        suffixes=("_zone", "_hospital"),
    )

    fig, axes = plt.subplots(2, 2, figsize=(18, 12))

    ax = axes[0, 0]
    for row in current_plot.itertuples(index=False):
        ax.plot(
            [row.x_coord_zone, row.x_coord_hospital],
            [row.y_coord_zone, row.y_coord_hospital],
            color="lightgray",
            alpha=0.35,
            linewidth=0.8,
        )
    ax.scatter(zones_plot["x_coord"], zones_plot["y_coord"], s=25, c="black", alpha=0.7, label="Zones")
    ax.scatter(
        hospitals_plot["x_coord"],
        hospitals_plot["y_coord"],
        s=hospitals_plot["existing_beds"] * 1.5,
        c=["crimson" if overload > 0 else "steelblue" for overload in hospitals_plot["current_overload"]],
        marker="s",
        edgecolors="black",
        linewidths=0.5,
        label="Hospitals",
    )
    ax.set_title("Current Scenario: Nearest-Hospital Assignment")
    ax.set_xlabel("X coordinate")
    ax.set_ylabel("Y coordinate")
    ax.legend(loc="upper right")

    ax = axes[0, 1]
    for row in optimized_plot.itertuples(index=False):
        ax.plot(
            [row.x_coord_zone, row.x_coord_hospital],
            [row.y_coord_zone, row.y_coord_hospital],
            color="lightgray",
            alpha=0.35,
            linewidth=0.8,
        )
    ax.scatter(zones_plot["x_coord"], zones_plot["y_coord"], s=25, c="black", alpha=0.7, label="Zones")
    non_expanded = hospitals_plot[hospitals_plot["expanded"] == 0]
    expanded = hospitals_plot[hospitals_plot["expanded"] == 1]
    ax.scatter(
        non_expanded["x_coord"],
        non_expanded["y_coord"],
        s=non_expanded["optimized_capacity"] * 0.8,
        c="steelblue",
        marker="s",
        edgecolors="black",
        linewidths=0.5,
        label="Not expanded",
    )
    ax.scatter(
        expanded["x_coord"],
        expanded["y_coord"],
        s=expanded["optimized_capacity"] * 0.8,
        c="gold",
        marker="*",
        edgecolors="black",
        linewidths=0.7,
        label="Expanded",
    )
    ax.set_title("Optimized Scenario: Post-Expansion Assignment")
    ax.set_xlabel("X coordinate")
    ax.set_ylabel("Y coordinate")
    ax.legend(loc="upper right")

    ax = axes[1, 0]
    x_axis = range(len(hospital_summary))
    ax.bar(x_axis, hospital_summary["current_capacity"], color="lightsteelblue", label="Current capacity")
    ax.bar(
        x_axis,
        hospital_summary["current_load"],
        alpha=0.75,
        color=["crimson" if overload > 0 else "seagreen" for overload in hospital_summary["current_overload"]],
        label="Current assigned load",
    )
    ax.set_title("Current Capacity vs Load")
    ax.set_xlabel("Hospital")
    ax.set_ylabel("Patients / Beds")
    ax.set_xticks(list(x_axis))
    ax.set_xticklabels(hospital_summary["hospital_id"], rotation=90)
    ax.legend(loc="upper right")

    ax = axes[1, 1]
    ax.bar(x_axis, hospital_summary["optimized_capacity"], color="lightsteelblue", label="Optimized capacity")
    ax.bar(x_axis, hospital_summary["optimized_load"], alpha=0.75, color="seagreen", label="Optimized assigned load")
    ax.set_title("Optimized Capacity vs Load")
    ax.set_xlabel("Hospital")
    ax.set_ylabel("Patients / Beds")
    ax.set_xticks(list(x_axis))
    ax.set_xticklabels(hospital_summary["hospital_id"], rotation=90)
    ax.legend(loc="upper right")

    total_current_overload = hospital_summary["current_overload"].sum()
    total_optimized_slack = hospital_summary["optimized_slack"].sum()
    fig.suptitle(
        "Hospital Network Before vs After Optimization\n"
        f"Total current overload = {total_current_overload:,.0f} patients | "
        f"Total optimized slack = {total_optimized_slack:,.0f} beds",
        fontsize=14,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    output_path = "hospital_scenario_comparison.png"
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)

    mesh_fig, mesh_axes = plt.subplots(1, 2, figsize=(20, 10))

    max_current_flow = max(1.0, result["current_primary_assignment"]["assigned_patients"].max())
    max_optimized_flow = max(1.0, result["allocation"]["assigned_patients"].max())

    ax = mesh_axes[0]
    for row in current_plot.itertuples(index=False):
        width = 0.8 + 4.0 * row.assigned_patients / max_current_flow
        ax.plot(
            [row.x_coord_zone, row.x_coord_hospital],
            [row.y_coord_zone, row.y_coord_hospital],
            color="dimgray",
            alpha=0.35,
            linewidth=width,
            zorder=1,
        )
    ax.scatter(
        zones_plot["x_coord"],
        zones_plot["y_coord"],
        s=20 + zones_plot["patient_demand"] * 0.25,
        c="black",
        alpha=0.75,
        label="Zones",
        zorder=2,
    )
    ax.scatter(
        hospitals_plot["x_coord"],
        hospitals_plot["y_coord"],
        s=150 + hospitals_plot["current_capacity"] * 0.6,
        c=["crimson" if overload > 0 else "steelblue" for overload in hospitals_plot["current_overload"]],
        marker="s",
        edgecolors="black",
        linewidths=0.6,
        label="Hospitals",
        zorder=3,
    )
    for row in hospitals_plot.itertuples(index=False):
        ax.text(row.x_coord + 1.2, row.y_coord + 1.2, row.hospital_id, fontsize=8, weight="bold")
    for row in zones_plot.itertuples(index=False):
        ax.text(row.x_coord + 0.8, row.y_coord + 0.8, row.zone_id, fontsize=6, alpha=0.8)
    ax.set_title("Current Routing Mesh")
    ax.set_xlabel("X coordinate")
    ax.set_ylabel("Y coordinate")
    ax.legend(loc="upper right")

    ax = mesh_axes[1]
    optimized_mesh = zones_plot.merge(result["allocation"], on="zone_id", how="left")
    optimized_mesh = optimized_mesh.merge(
        hospitals_plot[["hospital_id", "x_coord", "y_coord", "expanded"]],
        on="hospital_id",
        how="left",
        suffixes=("_zone", "_hospital"),
    )
    for row in optimized_mesh.itertuples(index=False):
        width = 0.8 + 4.0 * row.assigned_patients / max_optimized_flow
        ax.plot(
            [row.x_coord_zone, row.x_coord_hospital],
            [row.y_coord_zone, row.y_coord_hospital],
            color="darkgreen" if row.expanded == 1 else "gray",
            alpha=0.35,
            linewidth=width,
            zorder=1,
        )
    ax.scatter(
        zones_plot["x_coord"],
        zones_plot["y_coord"],
        s=20 + zones_plot["patient_demand"] * 0.25,
        c="black",
        alpha=0.75,
        label="Zones",
        zorder=2,
    )
    non_expanded = hospitals_plot[hospitals_plot["expanded"] == 0]
    expanded = hospitals_plot[hospitals_plot["expanded"] == 1]
    ax.scatter(
        non_expanded["x_coord"],
        non_expanded["y_coord"],
        s=150 + non_expanded["optimized_capacity"] * 0.4,
        c="steelblue",
        marker="s",
        edgecolors="black",
        linewidths=0.6,
        label="Not expanded",
        zorder=3,
    )
    ax.scatter(
        expanded["x_coord"],
        expanded["y_coord"],
        s=250 + expanded["optimized_capacity"] * 0.4,
        c="gold",
        marker="*",
        edgecolors="black",
        linewidths=0.8,
        label="Expanded",
        zorder=4,
    )
    for row in hospitals_plot.itertuples(index=False):
        ax.text(row.x_coord + 1.2, row.y_coord + 1.2, row.hospital_id, fontsize=8, weight="bold")
    for row in zones_plot.itertuples(index=False):
        ax.text(row.x_coord + 0.8, row.y_coord + 0.8, row.zone_id, fontsize=6, alpha=0.8)
    ax.set_title("Optimized Routing Mesh")
    ax.set_xlabel("X coordinate")
    ax.set_ylabel("Y coordinate")
    ax.legend(loc="upper right")

    mesh_fig.suptitle(
        "Zone-to-Hospital Routing Mesh for All Zones\n"
        "Edge thickness is proportional to routed patients",
        fontsize=14,
    )
    mesh_fig.tight_layout(rect=[0, 0, 1, 0.95])
    mesh_output_path = "hospital_routing_mesh.png"
    mesh_fig.savefig(mesh_output_path, dpi=300, bbox_inches="tight")
    plt.close(mesh_fig)

    grid_fig, ax = plt.subplots(figsize=(12, 10))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_xticks(range(0, 101, 10))
    ax.set_yticks(range(0, 101, 10))
    ax.grid(True, linestyle="--", linewidth=0.7, alpha=0.35)
    ax.scatter(
        zones_plot["x_coord"],
        zones_plot["y_coord"],
        s=30 + zones_plot["patient_demand"] * 0.2,
        c="black",
        alpha=0.75,
        label="Zones",
        zorder=2,
    )
    ax.scatter(
        hospitals_plot["x_coord"],
        hospitals_plot["y_coord"],
        s=120 + hospitals_plot["optimized_capacity"] * 0.35,
        c=["gold" if flag == 1 else "steelblue" for flag in hospitals_plot["expanded"]],
        marker="s",
        edgecolors="black",
        linewidths=0.7,
        label="Hospitals",
        zorder=3,
    )
    for row in zones_plot.itertuples(index=False):
        ax.text(row.x_coord + 0.8, row.y_coord + 0.8, row.zone_id, fontsize=7)
    for row in hospitals_plot.itertuples(index=False):
        ax.text(row.x_coord + 1.0, row.y_coord + 1.0, row.hospital_id, fontsize=8, weight="bold")
    ax.set_title("Zone Grid Layout")
    ax.set_xlabel("X coordinate")
    ax.set_ylabel("Y coordinate")
    ax.legend(loc="upper right")
    grid_fig.tight_layout()
    grid_output_path = "zone_grid_layout.png"
    grid_fig.savefig(grid_output_path, dpi=300, bbox_inches="tight")
    plt.close(grid_fig)

    print(f"\nVisualization saved to {output_path}")
    print(f"Routing mesh saved to {mesh_output_path}")
    print(f"Zone grid saved to {grid_output_path}")
    print(f"Execution time = {time.time() - start:.2f} seconds")


if __name__ == "__main__":
    main()
