import pandas as pd
import gurobipy as gp
from gurobipy import GRB


def load_data():
    H = pd.read_csv("hospitals.csv")
    Z = pd.read_csv("zones.csv")
    D = pd.read_csv("distance_matrix.csv")
    return H, Z, D


def solve_model(H, Z, D, P=10, X=1500):

    H_ids = H["hospital_id"].tolist()
    Z_ids = Z["zone_id"].tolist()

    base = dict(zip(H_ids, H["existing_beds"]))
    dem = dict(zip(Z_ids, Z["patient_demand"]))
    t = {(r.hospital_id, r.zone_id): r.travel_cost for r in D.itertuples()}

    cost = {
        r.hospital_id: r.fixed_open_expand_cost + X * r.cost_per_added_bed
        for r in H.itertuples()
    }

    m = gp.Model("Hospital_Expansion")

    # SETTINGS
    m.Params.TimeLimit = 60
    m.Params.MIPGap = 0.05
    m.Params.MIPFocus = 1
    m.Params.Heuristics = 0.1
    m.Params.Presolve = 2
    m.Params.OutputFlag = 1

    # VARIABLES
    y = m.addVars(H_ids, vtype=GRB.BINARY, name="y")
    q = m.addVars(H_ids, Z_ids, lb=0, name="q")

    # SELECT P
    m.addConstr(gp.quicksum(y[i] for i in H_ids) == P)

    # DEMAND
    for j in Z_ids:
        m.addConstr(gp.quicksum(q[i, j] for i in H_ids) == dem[j])

    # CAPACITY
    for i in H_ids:
        m.addConstr(
            gp.quicksum(q[i, j] for j in Z_ids)
            <= base[i] + X * y[i]
        )

    # OBJECTIVE
    m.setObjective(
        gp.quicksum(cost[i] * y[i] for i in H_ids)
        + gp.quicksum(t[i, j] * q[i, j] for i in H_ids for j in Z_ids),
        GRB.MINIMIZE
    )

    m.optimize()

    
    # OUTPUT

    print("\n===== RESULT =====")

    if m.SolCount > 0:

        selected = [i for i in H_ids if y[i].X > 0.5]

        expansion_cost = sum(cost[i] for i in selected)
        routing_cost = sum(
            t[i, j] * q[i, j].X for i in H_ids for j in Z_ids
        )

        print("Selected Hospitals:", selected)
        print("Expansion Cost:", round(expansion_cost, 2))
        print("Routing Cost:", round(routing_cost, 2))
        print("Total Cost:", round(m.ObjVal, 2))

        print("\nSolver Stats:")
        print("Best Bound:", m.ObjBound)
        print("Gap:", m.MIPGap)
        print("Runtime:", m.Runtime)

    else:
        print("No feasible solution found")


def main():
    H, Z, D = load_data()

    P = 10
    X = 1500

    solve_model(H, Z, D, P=P, X=X)


if __name__ == "__main__":
    main()