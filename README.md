# AOR Project

This repository now supports two execution paths over the same hospital network design model:

1. `AOR_PROJECT.py`
   Command-line execution with the original CSV outputs and matplotlib figures.
2. `main.py`
   FastAPI web application with an interactive scenario runner, dataset preview, network visualization, routing matrices, and allocation tables.

## Architecture

The optimization core lives in [hospital_network/optimizer.py](hospital_network/optimizer.py). It is responsible for:

- dataset loading from disk or in-memory CSV text
- schema and consistency validation
- bilevel MILP construction in Gurobi
- result post-processing into allocation tables, routing matrices, and network payloads

The web-facing request schema lives in [hospital_network/schemas.py](hospital_network/schemas.py), while [main.py](main.py) exposes:

- `GET /`
- `GET /api/health`
- `GET /api/dataset/default`
- `POST /api/solve`

The frontend is a server-rendered single-page tool using:

- [templates/index.html](templates/index.html)
- [static/styles.css](static/styles.css)
- [static/app.js](static/app.js)

## Run the FastAPI website

```bash
python -m uvicorn main:app --reload --port 8000
```

Then open:

```text
http://127.0.0.1:8000
```

## Run the original script

```bash
python AOR_PROJECT.py
```

## Interactive workflow

The website supports:

- tuning `P`, `X`, dual upper bound factor, and time limit
- optional LP export
- optional solver log capture
- using the default repository CSV files
- overriding any of the three CSV inputs from the browser without installing multipart upload support

Custom CSV files are read in the browser and sent as plain JSON text to the FastAPI backend. This keeps the dependency surface small while preserving a clean API boundary.

## Data expectations

Required columns:

- `distance_matrix.csv`: `zone_id`, `hospital_id`, `travel_cost`
- `hospitals.csv`: `hospital_id`, `name`, `existing_beds`, `cost_per_added_bed`, `fixed_open_expand_cost`
- `zones.csv`: `zone_id`, `patient_demand`

Additional visualization columns are used when available:

- hospitals: `x_coord`, `y_coord`
- zones: `x_coord`, `y_coord`

The backend also enforces:

- unique `hospital_id`
- unique `zone_id`
- unique `(zone_id, hospital_id)` pairs
- full zone-hospital cartesian coverage in the distance matrix
- nonnegative numeric fields

## Notes

- Gurobi must be installed and licensed in the runtime environment.
- Exported LP models are written to `artifacts/` in web mode and to the project root in CLI mode.
