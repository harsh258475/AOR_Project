from __future__ import annotations

from pathlib import Path
import logging
from xml.etree import ElementTree as ET
import zipfile

import gurobipy as gp
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from hospital_network.optimizer import (
    OptimizationConfig,
    build_dataset_preview,
    load_dataset_from_csv_text,
    load_dataset_from_disk,
    serialize_result,
    solve_bilevel_optimization,
)
from hospital_network.schemas import ScenarioRequest


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
ARTIFACT_DIR = BASE_DIR / "artifacts"
CASE_STUDY_FILE = BASE_DIR / "AOR_PROJECT_Hospital_Network_Design_and_Patient_Allocation Optimization in Delhi NCR.docx"
DATASET_FILES = {
    "distance_matrix.csv": BASE_DIR / "distance_matrix.csv",
    "hospitals.csv": BASE_DIR / "hospitals.csv",
    "zones.csv": BASE_DIR / "zones.csv",
}

ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Hospital Network Design Studio",
    version="1.0.0",
    summary="Interactive bilevel hospital expansion and patient allocation analysis.",
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/artifacts", StaticFiles(directory=str(ARTIFACT_DIR)), name="artifacts")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "default_config": OptimizationConfig().as_dict(),
            "case_study_url": "/case-study",
            "solution_approach_url": "/solution-approach",
            "dataset_links": {name: f"/dataset/file/{name}" for name in DATASET_FILES},
            "dataset_page_url": "/dataset",
        },
    )




@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/dataset/default")
def default_dataset_preview() -> dict:
    try:
        distance, hospitals, zones = load_dataset_from_disk(BASE_DIR)
        return build_dataset_preview(distance, hospitals, zones)
    except Exception as exc:  # pragma: no cover - defensive API boundary
        logger.exception("Failed to load default dataset preview.")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/case-study", response_class=HTMLResponse)
def case_study_page(request: Request) -> HTMLResponse:
    if not CASE_STUDY_FILE.exists():
        raise HTTPException(status_code=404, detail="Case study document not found.")
    return templates.TemplateResponse(
        request=request,
        name="case_study.html",
        context={
            "request": request,
            "title": "Hospital Network Design and Patient Allocation Optimization in Delhi NCR",
            "download_url": "/case-study/download",
            "sections": _extract_docx_sections(CASE_STUDY_FILE),
            "solution_approach_url": "/solution-approach",
            "dataset_page_url": "/dataset",
        },
    )


@app.get("/solution-approach", response_class=HTMLResponse)
def solution_approach_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="solution_approach.html",
        context={
            "request": request,
            "title": "Solution Approach",
            "dataset_page_url": "/dataset",
            "case_study_url": "/case-study",
            "example_iteration": {
                "iteration": 14,
                "checked_combinations": "128,440 / 2,035,800",
                "incumbent_hubs": ["H3", "H6", "H8", "H9", "H10", "H11", "H18"],
                "leader_cost": "21,528,929,871.00",
                "route_cost": "2,155,619.43",
                "event": "new best combination after follower LP evaluation",
            },
        },
    )


@app.get("/case-study/download")
def case_study_file() -> FileResponse:
    if not CASE_STUDY_FILE.exists():
        raise HTTPException(status_code=404, detail="Case study document not found.")
    return FileResponse(str(CASE_STUDY_FILE), filename=CASE_STUDY_FILE.name)


@app.get("/dataset", response_class=HTMLResponse)
def dataset_page(request: Request) -> HTMLResponse:
    previews = []
    for filename, file_path in DATASET_FILES.items():
        if not file_path.exists():
            continue
        frame = pd.read_csv(file_path)
        previews.append(
            {
                "filename": filename,
                "download_url": f"/dataset/file/{filename}",
                "row_count": len(frame),
                "column_count": len(frame.columns),
                "columns": [str(column) for column in frame.columns.tolist()],
                "rows": frame.head(15).fillna("").to_dict(orient="records"),
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="dataset.html",
        context={
            "request": request,
            "title": "Dataset Library",
            "previews": previews,
        },
    )


@app.get("/dataset/file/{filename}")
def dataset_file(filename: str) -> FileResponse:
    file_path = DATASET_FILES.get(filename)
    if file_path is None or not file_path.exists():
        raise HTTPException(status_code=404, detail="Dataset file not found.")
    return FileResponse(str(file_path), filename=file_path.name)


@app.post("/api/solve")
def solve_scenario(payload: ScenarioRequest) -> dict:
    try:
        logger.info("Solve request received with config: %s", payload.config.model_dump())
        config = OptimizationConfig(**payload.config.model_dump())
        dataset_payload = payload.dataset.model_dump() if payload.dataset else {}
        distance, hospitals, zones = load_dataset_from_csv_text(base_dir=BASE_DIR, **dataset_payload)
        logger.info("Dataset loaded: %d hospitals, %d zones", len(hospitals), len(zones))
        result = solve_bilevel_optimization(
            distance,
            hospitals,
            zones,
            config,
            artifact_dir=ARTIFACT_DIR,
            log_to_console=False,
            capture_solver_log=config.show_solver_log,
        )
        logger.info("Optimization completed. Serializing result...")
        response = serialize_result(result)
        logger.info("Result serialized successfully")
        model_file_name = response["artifacts"]["model_file_name"]
        if model_file_name:
            response["artifacts"]["model_file_url"] = f"/artifacts/{model_file_name}"
        else:
            response["artifacts"]["model_file_url"] = None
        return response
    except ValueError as exc:
        logger.exception("ValueError during solve")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("RuntimeError during solve")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except gp.GurobiError as exc:
        logger.exception("Gurobi execution failed.")
        raise HTTPException(status_code=500, detail=f"Gurobi error: {exc}") from exc
    except Exception as exc:  # pragma: no cover - defensive API boundary
        logger.exception("Unexpected failure during scenario solve.")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _extract_docx_sections(file_path: Path) -> list[str]:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(file_path) as archive:
        document_xml = archive.read("word/document.xml")
    root = ET.fromstring(document_xml)
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        text_fragments = [node.text for node in paragraph.findall(".//w:t", namespace) if node.text]
        merged = "".join(text_fragments).strip()
        if merged:
            paragraphs.append(merged)
    return paragraphs
