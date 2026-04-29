const state = {
    availableHospitalIds: [],
    currentMatrix: null,
    optimizedMatrix: null,
};

const elements = {
    appStatus: document.getElementById("app-status"),
    errorBanner: document.getElementById("request-error"),
    solveMeta: document.getElementById("solve-meta"),
    runScenario: document.getElementById("run-scenario"),
    resetConfig: document.getElementById("reset-config"),
    reloadDataset: document.getElementById("reload-dataset"),
    datasetSummary: document.getElementById("dataset-summary"),
    distancePreview: document.getElementById("distance-preview"),
    hospitalsPreview: document.getElementById("hospitals-preview"),
    zonesPreview: document.getElementById("zones-preview"),
    metrics: document.getElementById("metrics"),
    currentNetwork: document.getElementById("current-network"),
    optimizedNetwork: document.getElementById("optimized-network"),
    selectedHospitalsTable: document.getElementById("selected-hospitals-table"),
    hospitalSummaryTable: document.getElementById("hospital-summary-table"),
    allocationTable: document.getElementById("allocation-table"),
    currentMatrixTable: document.getElementById("current-matrix-table"),
    optimizedMatrixTable: document.getElementById("optimized-matrix-table"),
    solverLog: document.getElementById("solver-log"),
    artifactLinks: document.getElementById("artifact-links"),
    downloadCurrentMatrix: document.getElementById("download-current-matrix"),
    downloadOptimizedMatrix: document.getElementById("download-optimized-matrix"),
};

const configFields = [
    "p_expansions",
    "added_beds_per_expansion",
    "time_limit_seconds",
    "fixed_hub_hospital_ids",
    "show_solver_log",
    "export_model_file",
];

function applyDefaultConfig() {
    for (const field of configFields) {
        const element = document.getElementById(field);
        if (!element) continue;
        const value = window.DEFAULT_CONFIG[field];
        if (element.type === "checkbox") {
            element.checked = Boolean(value);
        } else {
            element.value = value;
        }
    }
    autoFillIncumbentHubs();
}

function setStatus(text, tone = "default") {
    elements.appStatus.textContent = text;
    elements.appStatus.className = "status-pill";
    if (tone !== "default") {
        elements.appStatus.classList.add(tone);
    }
}

function showError(message) {
    elements.errorBanner.textContent = message;
    elements.errorBanner.classList.remove("hidden");
}

function clearError() {
    elements.errorBanner.textContent = "";
    elements.errorBanner.classList.add("hidden");
}

function formatNumber(value, digits = 2) {
    if (value === null || value === undefined || value === "") {
        return "-";
    }
    if (typeof value !== "number") {
        return String(value);
    }
    if (!Number.isFinite(value)) {
        return "-";
    }
    return new Intl.NumberFormat("en-US", {
        minimumFractionDigits: 0,
        maximumFractionDigits: digits,
    }).format(value);
}

function autoFillIncumbentHubs() {
    const pValue = Number(document.getElementById("p_expansions").value);
    if (!Number.isInteger(pValue) || pValue <= 0 || state.availableHospitalIds.length === 0) {
        return;
    }
    const hospitalCount = state.availableHospitalIds.length;
    const selectionSize = Math.min(pValue, hospitalCount);
    const startIndex = Math.max(0, hospitalCount - selectionSize);
    const fillIds = state.availableHospitalIds.slice(startIndex);
    document.getElementById("fixed_hub_hospital_ids").value = fillIds.join(",");
}

function readFileAsText(file) {
    return new Promise((resolve, reject) => {
        if (!file) {
            resolve(null);
            return;
        }
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(new Error(`Failed to read ${file.name}`));
        reader.readAsText(file);
    });
}

function renderSummaryGrid(container, items) {
    container.innerHTML = items
        .map(
            ([label, value]) => `
                <div class="summary-item">
                    <span>${label}</span>
                    <strong>${value}</strong>
                </div>
            `
        )
        .join("");
}

function renderComparisonGrid(container, rows) {
    container.innerHTML = rows
        .map(
            (row) => `
                <div class="comparison-row">
                    <div class="comparison-name">
                        <span>${escapeHtml(row.name)}</span>
                        <strong>${escapeHtml(row.hubs)}</strong>
                    </div>
                    <div class="comparison-value">
                        <span>Total Cost</span>
                        <strong>${escapeHtml(formatNumber(row.totalCost))}</strong>
                    </div>
                    <div class="comparison-value">
                        <span>Leader Cost</span>
                        <strong>${escapeHtml(formatNumber(row.leaderCost))}</strong>
                    </div>
                    <div class="comparison-value">
                        <span>Distance Cost</span>
                        <strong>${escapeHtml(formatNumber(row.distanceCost))}</strong>
                    </div>
                </div>
            `
        )
        .join("");
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function renderTable(container, records, preferredColumns = null) {
    if (!records || records.length === 0) {
        container.classList.add("empty-state");
        container.textContent = "No rows to display.";
        return;
    }

    const columns = preferredColumns || Object.keys(records[0]);
    const thead = columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
    const tbody = records
        .map((row) => {
            const cells = columns
                .map((column) => `<td>${escapeHtml(row[column] ?? "-")}</td>`)
                .join("");
            return `<tr>${cells}</tr>`;
        })
        .join("");

    container.classList.remove("empty-state");
    container.innerHTML = `<table><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>`;
}

function renderHospitalSummary(container, records) {
    if (!records || records.length === 0) {
        container.classList.add("empty-state");
        container.textContent = "No rows to display.";
        return;
    }

    const rows = records
        .map((row) => {
            const currentPct = Math.max(0, Math.min((row.current_utilization || 0) * 100, 160));
            const optimizedPct = Math.max(0, Math.min((row.optimized_utilization || 0) * 100, 160));
            const currentClass = row.current_overload > 0 ? "load-bar overloaded" : "load-bar";

            return `
                <tr>
                    <td>${escapeHtml(row.hospital_id)}</td>
                    <td>${escapeHtml(row.name)}</td>
                    <td>${row.expanded ? "Yes" : "No"}</td>
                    <td>${formatNumber(row.current_capacity)}</td>
                    <td>${formatNumber(row.current_load)}</td>
                    <td>
                        <div class="${currentClass}">
                            <span style="width:${Math.min(currentPct, 100)}%"></span>
                        </div>
                        <div>${formatNumber(row.current_utilization * 100)}%</div>
                    </td>
                    <td>${formatNumber(row.current_overload)}</td>
                    <td>${formatNumber(row.current_slack)}</td>
                    <td>${escapeHtml(row.current_status || "")}</td>
                    <td>${escapeHtml(row.current_assignment || "")}</td>
                    <td>${formatNumber(row.optimized_capacity)}</td>
                    <td>${formatNumber(row.optimized_load)}</td>
                    <td>
                        <div class="load-bar">
                            <span style="width:${Math.min(optimizedPct, 100)}%"></span>
                        </div>
                        <div>${formatNumber(row.optimized_utilization * 100)}%</div>
                    </td>
                    <td>${formatNumber(row.optimized_slack)}</td>
                </tr>
            `;
        })
        .join("");

    container.classList.remove("empty-state");
    container.innerHTML = `
        <table>
            <thead>
                <tr>
                    <th>Hospital</th>
                    <th>Name</th>
                    <th>Expanded</th>
                    <th>Current Capacity</th>
                    <th>Current Load</th>
                    <th>Current Utilization</th>
                    <th>Current Overload</th>
                    <th>Current Slack</th>
                    <th>Current Status</th>
                    <th>Current Assignment</th>
                    <th>Optimized Capacity</th>
                    <th>Optimized Load</th>
                    <th>Optimized Utilization</th>
                    <th>Optimized Slack</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

function renderMatrix(container, matrix) {
    if (!matrix) {
        container.classList.add("empty-state");
        container.textContent = "No matrix available.";
        return;
    }

    const headerCells = matrix.columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
    const bodyRows = matrix.rows
        .map((rowLabel, rowIndex) => {
            const values = matrix.values[rowIndex]
                .map((value) => `<td>${escapeHtml(formatNumber(value))}</td>`)
                .join("");
            return `<tr><th>${escapeHtml(rowLabel)}</th>${values}</tr>`;
        })
        .join("");

    container.classList.remove("empty-state");
    container.innerHTML = `
        <table>
            <thead>
                <tr>
                    <th>Zone</th>
                    ${headerCells}
                </tr>
            </thead>
            <tbody>${bodyRows}</tbody>
        </table>
    `;
}

function renderNetwork(container, network, edgeKey) {
    if (!network || !network.enabled) {
        container.classList.add("empty-state");
        container.textContent = network?.reason || "Visualization unavailable.";
        return;
    }

    const hospitals = network.hospitals || [];
    const zones = network.zones || [];
    const edges = network[edgeKey] || [];
    const maxFlow = Math.max(1, ...edges.map((edge) => Number(edge.assigned_patients || 0)));
    const isCurrentView = edgeKey === "current_edges";
    const activeLoadField = isCurrentView ? "current_load" : "optimized_load";
    const activeCapacityField = isCurrentView ? "current_capacity" : "optimized_capacity";
    const activeHubField = isCurrentView ? "current_hub" : "optimal_hub";

    const margin = 8;
    const scale = 0.82;
    const projectX = (x) => margin + scale * Number(x);
    const projectY = (y) => 100 - margin - scale * Number(y);
    const clamp = (value, min, max) => Math.max(min, Math.min(value, max));

    const hospitalMarkers = hospitals.map((hospital) => ({
        ...hospital,
        plotX: projectX(hospital.x_coord),
        plotY: projectY(hospital.y_coord),
    }));

    const hubPalette = ["#ef4444", "#2563eb", "#16a34a", "#d97706", "#8b5cf6", "#0f766e", "#be123c", "#c2410c"];
    const hubColorMap = {};
    const hubMarkerMap = {};
    let hubColorIndex = 0;

    hospitalMarkers.forEach((hospital) => {
        if (hospital[activeHubField] === 1) {
            const color = hubPalette[hubColorIndex % hubPalette.length];
            const markerId = `arrow-${hospital.hospital_id}`;
            hubColorMap[hospital.hospital_id] = color;
            hubMarkerMap[hospital.hospital_id] = markerId;
            hubColorIndex += 1;
        }
    });

    const markerDefs = Object.entries(hubMarkerMap)
        .map(([hospitalId, markerId]) => {
            const color = hubColorMap[hospitalId] || "#94a3b8";
            return `
                <marker id="${markerId}" viewBox="0 0 6 6" refX="5" refY="3" markerUnits="strokeWidth" markerWidth="4" markerHeight="4" orient="auto-start-reverse">
                    <path d="M0,0 L6,3 L0,6 Z" fill="${color}" />
                </marker>
            `;
        })
        .join("");

    const edgeSvg = edges
        .map((edge) => {
            const stroke = hubColorMap[edge.hospital_id] || "#7b8794";
            const marker = hubMarkerMap[edge.hospital_id] ? `url(#${hubMarkerMap[edge.hospital_id]})` : "";
            const width = 0.25 + 1.3 * Number(edge.assigned_patients || 0) / maxFlow;
            return `
                <line
                    x1="${projectX(edge.x_coord_zone)}"
                    y1="${projectY(edge.y_coord_zone)}"
                    x2="${projectX(edge.x_coord_hospital)}"
                    y2="${projectY(edge.y_coord_hospital)}"
                    class="network-edge"
                    stroke="${stroke}"
                    stroke-opacity="0.72"
                    stroke-width="${width}"
                    marker-end="${marker}">
                    <title>${escapeHtml(`${edge.zone_id} → ${edge.hospital_id}: ${formatNumber(edge.assigned_patients)} patients`)}</title>
                </line>
            `;
        })
        .join("");

    const zoneSvg = zones
        .map((zone) => {
            const radius = 0.65 + Math.min(Number(zone.patient_demand || 0) / 360, 0.75);
            return `
            <g>
                <circle
                    class="zone-dot"
                    cx="${projectX(zone.x_coord)}"
                    cy="${projectY(zone.y_coord)}"
                    r="${radius}"
                    fill="#111827"
                    fill-opacity="0.7">
                    <title>${escapeHtml(`${zone.zone_id}: demand ${formatNumber(zone.patient_demand)}`)}</title>
                </circle>
                <text
                    x="${projectX(zone.x_coord)}"
                    y="${projectY(zone.y_coord) - 1.2}"
                    class="zone-label"
                    text-anchor="middle">
                    ${escapeHtml(String(zone.zone_id))}
                </text>
            </g>
        `;
        })
        .join("");

    const hubMarkersOnly = hospitalMarkers.filter((hospital) => hospital[activeHubField] === 1);
    const labelHospitals = hubMarkersOnly
        .slice()
        .sort((left, right) => Number(right[activeLoadField] || 0) - Number(left[activeLoadField] || 0))
        .slice(0, 8);

    const occupiedLabels = [];
    const hospitalLabelSvg = labelHospitals
        .sort((left, right) => left.plotY - right.plotY)
        .map((hospital) => {
            const labelText = String(hospital.hospital_id);
            const preferredOffsetX = hospital.plotX < 50 ? 2.0 : -2.0;
            const preferredOffsetY = hospital.plotY < 50 ? -2.4 : 2.8;

            let labelX = clamp(hospital.plotX + preferredOffsetX, 1.5, 94.5);
            let labelY = clamp(hospital.plotY + preferredOffsetY, 4.8, 96.5);
            const width = 1.8 + labelText.length * 1.2;
            const height = 3.6;

            const overlaps = (candidate) =>
                occupiedLabels.some((box) => !(
                    candidate.x + candidate.width < box.x ||
                    box.x + box.width < candidate.x ||
                    candidate.y + candidate.height < box.y ||
                    box.y + box.height < candidate.y
                ));

            let candidate = {
                x: labelX - 1.0,
                y: labelY - 2.8,
                width,
                height,
            };

            if (overlaps(candidate)) {
                const alternatives = [
                    { x: clamp(hospital.plotX + 2.0, 1.5, 94.5), y: clamp(hospital.plotY - 2.8, 4.8, 96.5) },
                    { x: clamp(hospital.plotX + 2.0, 1.5, 94.5), y: clamp(hospital.plotY + 2.8, 4.8, 96.5) },
                    { x: clamp(hospital.plotX - 3.4, 1.5, 94.5), y: clamp(hospital.plotY - 2.8, 4.8, 96.5) },
                    { x: clamp(hospital.plotX - 3.4, 1.5, 94.5), y: clamp(hospital.plotY + 2.8, 4.8, 96.5) },
                ];

                for (const option of alternatives) {
                    const optionCandidate = {
                        x: option.x - 1.0,
                        y: option.y - 2.8,
                        width,
                        height,
                    };
                    if (!overlaps(optionCandidate)) {
                        labelX = option.x;
                        labelY = option.y;
                        candidate = optionCandidate;
                        break;
                    }
                }
            }

            if (overlaps(candidate)) {
                return "";
            }

            occupiedLabels.push(candidate);
            return `
                <g>
                    <rect
                        class="network-label-tag"
                        x="${candidate.x}"
                        y="${candidate.y}"
                        width="${candidate.width}"
                        height="${candidate.height}"
                        rx="1.2"
                        ry="1.2">
                    </rect>
                    <text x="${labelX}" y="${labelY}" class="network-label" text-anchor="middle">${escapeHtml(labelText)}</text>
                </g>
            `;
        })
        .join("");

    const hospitalSvg = hubMarkersOnly
        .map((hospital) => {
            const x = hospital.plotX;
            const y = hospital.plotY;
            const fill = hubColorMap[hospital.hospital_id] || "#d97706";
            const outer = 2.6;
            const inner = 1.0;
            const points = 5;
            const coordinates = Array.from({ length: points * 2 }, (_, index) => {
                const angle = (Math.PI / points) * index - Math.PI / 2;
                const radius = index % 2 === 0 ? outer : inner;
                return `${x + radius * Math.cos(angle)},${y + radius * Math.sin(angle)}`;
            }).join(" ");
            return `
                <g class="network-node">
                    <polygon points="${coordinates}"
                        class="hub-star"
                        fill="${fill}"
                        stroke="#0f172a"
                        stroke-width="0.18">
                        <title>${escapeHtml(`${hospital.hospital_id} | load ${formatNumber(hospital[activeLoadField])} / ${formatNumber(hospital[activeCapacityField])} | hub`)}</title>
                    </polygon>
                </g>
            `;
        })
        .join("");

    container.classList.remove("empty-state");
    container.innerHTML = `
        <svg viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet" aria-label="Network visualization">
            <defs>
                <pattern id="network-grid-pattern" width="10" height="10" patternUnits="userSpaceOnUse">
                    <path d="M 10 0 L 0 0 0 10" fill="none" stroke="rgba(51, 84, 120, 0.08)" stroke-width="0.3"></path>
                </pattern>
                ${markerDefs}
            </defs>
            <rect x="6" y="6" width="88" height="88" fill="url(#network-grid-pattern)" stroke="rgba(71, 96, 124, 0.16)" stroke-width="0.35"></rect>
            <text x="9" y="12.8" class="network-axis-label">Projected service region</text>
            ${edgeSvg}
            ${zoneSvg}
            ${hospitalSvg}
            ${hospitalLabelSvg}
        </svg>
    `;
}

function matrixToCsv(matrix) {
    const header = ["zone_id", ...matrix.columns];
    const lines = [header.join(",")];
    matrix.rows.forEach((rowLabel, index) => {
        lines.push([rowLabel, ...matrix.values[index]].join(","));
    });
    return lines.join("\n");
}

function downloadText(filename, content) {
    const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
}

function renderArtifacts(artifacts) {
    if (artifacts?.model_file_url) {
        elements.artifactLinks.innerHTML = `
            <a class="artifact-link" href="${artifacts.model_file_url}" target="_blank" rel="noreferrer">
                Download ${escapeHtml(artifacts.model_file_name)}
            </a>
        `;
        return;
    }
    elements.artifactLinks.innerHTML = "";
}

function renderMetrics(payload) {
    const comparison = payload.comparison;
    if (!comparison) {
        console.error("Missing comparison data in response:", payload);
        elements.metrics.classList.add("empty-state");
        elements.metrics.textContent = "Error: Missing comparison data in response.";
        return;
    }
    const providedHubs = Array.isArray(comparison.provided_hubs) ? comparison.provided_hubs : [];
    const optimalHubs = Array.isArray(comparison.optimal_hubs) ? comparison.optimal_hubs : [];
    renderComparisonGrid(elements.metrics, [
        {
            name: "Current Assignment",
            hubs: "No added hubs",
            totalCost: comparison.current_total_cost,
            leaderCost: comparison.current_leader_cost,
            distanceCost: comparison.current_travel_cost,
        },
        {
            name: "Provided Hospitals",
            hubs: providedHubs.length > 0 ? providedHubs.join(", ") : "-",
            totalCost: comparison.provided_total_cost,
            leaderCost: comparison.provided_leader_cost,
            distanceCost: comparison.provided_travel_cost,
        },
        {
            name: "Optimal Selection",
            hubs: optimalHubs.length > 0 ? optimalHubs.join(", ") : "Unknown",
            totalCost: comparison.optimal_total_cost,
            leaderCost: comparison.optimal_leader_cost,
            distanceCost: comparison.optimal_travel_cost,
        },
    ]);
}

function renderDatasetPreview(payload) {
    const summary = payload.summary;
    state.availableHospitalIds = payload.hospital_ids || [];
    document.getElementById("p_expansions").max = String(summary.hospital_count);
    autoFillIncumbentHubs();
    renderSummaryGrid(elements.datasetSummary, [
        ["Hospitals", formatNumber(summary.hospital_count, 0)],
        ["Zones", formatNumber(summary.zone_count, 0)],
        ["Distance Records", formatNumber(summary.distance_record_count, 0)],
        ["Total Existing Capacity", formatNumber(summary.total_existing_capacity)],
        ["Total Demand", formatNumber(summary.total_demand)],
        ["Min Travel Cost", formatNumber(summary.travel_cost_min)],
        ["Max Travel Cost", formatNumber(summary.travel_cost_max)],
        ["Coordinates Available", summary.coordinates_available ? "Yes" : "No"],
    ]);
    renderTable(elements.hospitalsPreview, payload.hospitals_preview.rows, payload.hospitals_preview.columns);
    renderTable(elements.zonesPreview, payload.zones_preview.rows, payload.zones_preview.columns);
    renderTable(elements.distancePreview, payload.distance_preview.rows, payload.distance_preview.columns);
}

function renderSolveResponse(payload) {
    renderMetrics(payload);
    renderNetwork(elements.currentNetwork, payload.network, "current_edges");
    renderNetwork(elements.optimizedNetwork, payload.network, "optimized_edges");
    renderTable(elements.selectedHospitalsTable, payload.selected_hospitals);
    renderHospitalSummary(elements.hospitalSummaryTable, payload.hospital_summary);
    renderTable(elements.allocationTable, payload.allocation);

    state.currentMatrix = payload.routing_matrices.current;
    state.optimizedMatrix = payload.routing_matrices.optimized;
    renderMatrix(elements.currentMatrixTable, state.currentMatrix);
    renderMatrix(elements.optimizedMatrixTable, state.optimizedMatrix);

    elements.downloadCurrentMatrix.disabled = false;
    elements.downloadOptimizedMatrix.disabled = false;

    const providedHubs = Array.isArray(payload.comparison?.provided_hubs) ? payload.comparison.provided_hubs : [];
    const optimalHubs = Array.isArray(payload.comparison?.optimal_hubs) ? payload.comparison.optimal_hubs : [];
    elements.solverLog.classList.remove("empty-state");
    elements.solverLog.textContent = payload.solver_log || "No iteration log available for this run.";
    elements.solveMeta.textContent =
        `Status: ${payload.status.name} | Provided hubs: ${providedHubs.length > 0 ? providedHubs.join(", ") : "-"} | Optimal hubs: ${optimalHubs.length > 0 ? optimalHubs.join(", ") : "Unknown"} | Runtime: ${formatNumber(payload.metrics.runtime_seconds, 4)} s`;
    renderArtifacts(payload.artifacts);
}

async function loadDefaultPreview() {
    clearError();
    setStatus("Loading", "running");
    try {
        const response = await fetch("/api/dataset/default");
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.detail || "Failed to load dataset preview.");
        }
        renderDatasetPreview(payload);
        setStatus("Ready", "success");
    } catch (error) {
        showError(error.message);
        setStatus("Error", "error");
    }
}

async function buildSolvePayload() {
    const [distanceCsv, hospitalsCsv, zonesCsv] = await Promise.all([
        readFileAsText(document.getElementById("distance-file").files[0]),
        readFileAsText(document.getElementById("hospitals-file").files[0]),
        readFileAsText(document.getElementById("zones-file").files[0]),
    ]);

    return {
        config: {
            p_expansions: Number(document.getElementById("p_expansions").value),
            added_beds_per_expansion: Number(document.getElementById("added_beds_per_expansion").value),
            time_limit_seconds: Number(document.getElementById("time_limit_seconds").value),
            fixed_hub_hospital_ids: document.getElementById("fixed_hub_hospital_ids").value
                .split(",")
                .map((value) => value.trim())
                .filter((value) => value.length > 0),
            show_solver_log: document.getElementById("show_solver_log").checked,
            export_model_file: document.getElementById("export_model_file").checked,
        },
        dataset: {
            distance_csv: distanceCsv,
            hospitals_csv: hospitalsCsv,
            zones_csv: zonesCsv,
        },
    };
}

async function runScenario() {
    clearError();
    elements.runScenario.disabled = true;
    setStatus("Solving", "running");
    elements.solveMeta.textContent = "Optimization is running.";
    try {
        const payload = await buildSolvePayload();
        const response = await fetch("/api/solve", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const responsePayload = await response.json();
        if (!response.ok) {
            const errorDetail = responsePayload.detail || "Optimization failed.";
            console.error("API Error:", errorDetail, "Response:", responsePayload);
            throw new Error(errorDetail);
        }
        console.log("Solve Response:", responsePayload);
        if (!responsePayload.comparison) {
            console.error("Missing comparison in response:", responsePayload);
            throw new Error("Invalid response: missing comparison data.");
        }
        renderSolveResponse(responsePayload);
        setStatus("Solved", "success");
    } catch (error) {
        console.error("Scenario error:", error);
        showError(error.message);
        setStatus("Error", "error");
        elements.solveMeta.textContent = "Optimization failed.";
    } finally {
        elements.runScenario.disabled = false;
    }
}

elements.resetConfig.addEventListener("click", applyDefaultConfig);
elements.reloadDataset.addEventListener("click", loadDefaultPreview);
elements.runScenario.addEventListener("click", runScenario);
document.getElementById("p_expansions").addEventListener("input", autoFillIncumbentHubs);
elements.downloadCurrentMatrix.addEventListener("click", () => {
    if (state.currentMatrix) {
        downloadText("current_routing_matrix.csv", matrixToCsv(state.currentMatrix));
    }
});
elements.downloadOptimizedMatrix.addEventListener("click", () => {
    if (state.optimizedMatrix) {
        downloadText("optimized_routing_matrix.csv", matrixToCsv(state.optimizedMatrix));
    }
});

applyDefaultConfig();
loadDefaultPreview();
