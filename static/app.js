const state = {
  config: null,
  analysis: null,
  jobId: null,
  pollTimer: null,
};

const $ = (selector) => document.querySelector(selector);

const elements = {
  apiKeyStatus: $("#apiKeyStatus"),
  configGrid: $("#configGrid"),
  analyzeForm: $("#analyzeForm"),
  analyzeButton: $("#analyzeButton"),
  episodeName: $("#episodeName"),
  csvFile: $("#csvFile"),
  analysisTime: $("#analysisTime"),
  metrics: $("#metrics"),
  messages: $("#messages"),
  speakerSummary: $("#speakerSummary"),
  voiceValidation: $("#voiceValidation"),
  previewTable: $("#previewTable"),
  forceRegenerate: $("#forceRegenerate"),
  generateButton: $("#generateButton"),
  downloadButton: $("#downloadButton"),
  jobStatus: $("#jobStatus"),
  progressLabel: $("#progressLabel"),
  progressBar: $("#progressBar"),
  jobMetrics: $("#jobMetrics"),
  jobLog: $("#jobLog"),
};

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "0";
  return Number(value).toLocaleString();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  if (label) button.textContent = label;
}

function renderMetricGrid(target, metrics) {
  target.innerHTML = metrics
    .map(
      (metric) => `
        <div class="metric">
          <span>${escapeHtml(metric.label)}</span>
          <strong>${escapeHtml(metric.value)}</strong>
        </div>
      `,
    )
    .join("");
}

function renderTable(target, rows, columns) {
  if (!rows || rows.length === 0) {
    target.className = "table-shell empty";
    target.textContent = "No rows.";
    return;
  }

  const columnNames = columns || Object.keys(rows[0]);
  target.className = "table-shell";
  target.innerHTML = `
    <table>
      <thead>
        <tr>${columnNames.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr>
      </thead>
      <tbody>
        ${rows
          .map(
            (row) => `
              <tr>
                ${columnNames.map((column) => `<td>${escapeHtml(row[column])}</td>`).join("")}
              </tr>
            `,
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function renderMessages(analysis) {
  const parts = [];
  if (analysis.errors.length) {
    parts.push(`<div class="message error">Generation is blocked by ${analysis.errors.length} error(s).</div>`);
    parts.push(`<div class="table-shell">${tableHtml(analysis.errors)}</div>`);
  }

  if (analysis.warnings.length) {
    parts.push(`<div class="message warning">${analysis.warnings.length} warning(s) need review before spending credits.</div>`);
    parts.push(`<div class="table-shell">${tableHtml(analysis.warnings)}</div>`);
  }

  if (!analysis.errors.length && !analysis.warnings.length) {
    parts.push(`<div class="message success">CSV is ready for generation.</div>`);
  }

  elements.messages.innerHTML = parts.join("");
}

function tableHtml(rows) {
  if (!rows || rows.length === 0) return "";
  const columns = Object.keys(rows[0]);
  return `
    <table>
      <thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>
      <tbody>
        ${rows
          .map(
            (row) => `
              <tr>${columns.map((column) => `<td>${escapeHtml(row[column])}</td>`).join("")}</tr>
            `,
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function renderConfig(config) {
  elements.apiKeyStatus.textContent = config.api_key_configured ? "API key configured" : "API key missing";
  elements.apiKeyStatus.classList.toggle("ok", config.api_key_configured);
  elements.apiKeyStatus.classList.toggle("missing", !config.api_key_configured);

  renderMetricGrid(elements.configGrid, [
    { label: "Model", value: config.model_id },
    { label: "Output", value: config.output_format },
    { label: "Known speakers", value: formatNumber(config.known_speakers.length) },
    { label: "Storage", value: config.storage },
  ]);
}

function renderAnalysis(analysis) {
  state.analysis = analysis;
  elements.analysisTime.textContent = `Analyzed ${analysis.analyzed_at}`;

  const summary = analysis.summary;
  renderMetricGrid(elements.metrics, [
    { label: "CSV file", value: analysis.filename },
    { label: "Rows", value: formatNumber(summary?.total_rows || 0) },
    { label: "Characters", value: formatNumber(summary?.total_characters || 0) },
    { label: "Blocking errors", value: formatNumber(analysis.errors.length) },
  ]);

  renderMessages(analysis);
  renderTable(elements.speakerSummary, summary?.by_speaker || [], ["speaker", "cues", "characters"]);
  renderTable(elements.voiceValidation, analysis.voice_validation, ["id", "speaker", "csv_voice_id", "expected_voice_id", "status"]);
  renderTable(elements.previewTable, analysis.preview, ["id", "speaker", "text_preview", "voice_id", "voice_status"]);

  elements.generateButton.disabled = !analysis.valid || !state.config?.api_key_configured;
  elements.jobStatus.textContent = analysis.valid ? "Ready to generate" : "Fix blocking errors first";
}

function renderJob(job) {
  const percent = Math.round((job.progress || 0) * 100);
  elements.jobStatus.textContent = job.status_message || job.status;
  elements.progressLabel.textContent = `${percent}%`;
  elements.progressBar.style.width = `${percent}%`;

  renderMetricGrid(elements.jobMetrics, [
    { label: "Status", value: job.status },
    { label: "Generated", value: formatNumber(job.generated) },
    { label: "Skipped", value: formatNumber(job.skipped) },
    { label: "Failed", value: formatNumber(job.failed) },
    { label: "ZIP files", value: formatNumber(job.zipped_count) },
  ]);

  renderTable(elements.jobLog, job.log_rows, ["id", "speaker", "filename", "status", "message", "character_count"]);

  if (job.status === "completed" && job.download_url) {
    elements.downloadButton.href = job.download_url;
    elements.downloadButton.classList.remove("disabled");
    elements.downloadButton.removeAttribute("aria-disabled");
  }

  if (job.status === "completed" || job.status === "failed") {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    elements.generateButton.disabled = !state.analysis?.valid;
  }
}

async function loadConfig() {
  const response = await fetch("/api/config");
  if (!response.ok) throw new Error("Cannot load configuration");
  state.config = await response.json();
  renderConfig(state.config);
}

async function analyzeCsv(event) {
  event.preventDefault();
  if (!elements.csvFile.files.length) return;

  setBusy(elements.analyzeButton, true, "Analyzing");
  elements.generateButton.disabled = true;
  elements.downloadButton.classList.add("disabled");
  elements.downloadButton.href = "#";
  elements.messages.innerHTML = "";

  try {
    const formData = new FormData();
    formData.append("episode_name", elements.episodeName.value || "EP01");
    formData.append("file", elements.csvFile.files[0]);

    const response = await fetch("/api/analyze", {
      method: "POST",
      body: formData,
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Analyze failed");
    renderAnalysis(payload);
  } catch (error) {
    elements.messages.innerHTML = `<div class="message error">${escapeHtml(error.message)}</div>`;
  } finally {
    setBusy(elements.analyzeButton, false, "Analyze CSV");
  }
}

async function startGeneration() {
  if (!state.analysis?.analysis_id) return;

  elements.generateButton.disabled = true;
  elements.downloadButton.classList.add("disabled");
  elements.downloadButton.href = "#";
  elements.jobLog.className = "table-shell empty";
  elements.jobLog.textContent = "Starting generation.";

  const response = await fetch("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      analysis_id: state.analysis.analysis_id,
      episode_name: elements.episodeName.value || "EP01",
      force_regenerate: elements.forceRegenerate.checked,
    }),
  });
  const payload = await response.json();
  if (!response.ok) {
    elements.jobStatus.textContent = payload.detail || "Generation failed to start";
    elements.generateButton.disabled = false;
    return;
  }

  state.jobId = payload.job_id;
  renderJob(payload);

  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(pollJob, 1500);
}

async function pollJob() {
  if (!state.jobId) return;
  const response = await fetch(`/api/jobs/${state.jobId}`);
  if (!response.ok) return;
  renderJob(await response.json());
}

elements.analyzeForm.addEventListener("submit", analyzeCsv);
elements.generateButton.addEventListener("click", startGeneration);

loadConfig().catch((error) => {
  elements.apiKeyStatus.textContent = error.message;
  elements.apiKeyStatus.classList.add("missing");
});
