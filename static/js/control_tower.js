const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const TOTAL_PAGES = 75;
const PAGES_PER_DOCUMENT = 15;
const DEFAULT_BOUNDARIES = [1, 16, 31, 46, 61];
const documentNames = [
  "Lorem ipsum · Document 1", "Lorem ipsum · Document 2", "Lorem ipsum · Document 3",
  "Lorem ipsum · Document 4", "Lorem ipsum · Document 5",
];
let boundaries = new Set(DEFAULT_BOUNDARIES);
let zoom = 100;
let pipelineRunning = false;
let viewerPage = 1;
let toastTimer;

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 3200);
}

function openView(name, updateHash = true) {
  const target = $(`#view-${name}`);
  if (!target) return;
  $$(".view").forEach((view) => view.classList.toggle("active", view === target));
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  $("#page-title").textContent = target.dataset.title;
  $("#page-eyebrow").textContent = target.dataset.eyebrow;
  document.body.classList.toggle("splitter-active", name === "splitter");
  $(".sidebar").classList.remove("open");
  if (updateHash) history.replaceState(null, "", `#${name}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

$$('[data-view]').forEach((button) => button.addEventListener("click", () => openView(button.dataset.view)));
$$('[data-go]').forEach((button) => button.addEventListener("click", () => openView(button.dataset.go)));
$$('[data-view-link]').forEach((link) => link.addEventListener("click", (event) => {
  event.preventDefault();
  openView(link.dataset.viewLink);
}));
$("#mobile-menu").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
document.addEventListener("click", (event) => {
  if (window.innerWidth <= 800 && !event.target.closest(".sidebar") && !event.target.closest("#mobile-menu")) {
    $(".sidebar").classList.remove("open");
  }
});

function renderPages() {
  const grid = $("#page-grid");
  grid.innerHTML = "";
  for (let page = 1; page <= TOTAL_PAGES; page += 1) {
    const { sourceDocument, sourcePage, pageNumber } = pageCoordinates(page);
    const item = document.createElement("article");
    item.className = `page-item${boundaries.has(page) ? " boundary" : ""}`;
    item.dataset.page = page;
    item.innerHTML = `${boundaries.has(page) ? '<span class="page-tag">DOCUMENT START</span>' : ""}
      <div class="page-sheet"><img src="/static/pdf-pages/doc-${sourceDocument}-page-${pageNumber}.webp"
        alt="Document ${sourceDocument}, source page ${sourcePage}" ${page <= 8 ? "" : 'loading="lazy"'} decoding="async"></div>
      <button class="page-expand" aria-label="Open bundle page ${page} full screen"><svg viewBox="0 0 24 24"><path d="M8 3H3v5m13-5h5v5M8 21H3v-5m13 5h5v-5"/></svg></button>
      <div class="page-footer"><span>Bundle page ${page}</span><span>Doc ${sourceDocument} · Page ${sourcePage}</span></div>`;
    item.addEventListener("click", () => toggleBoundary(page));
    item.querySelector(".page-expand").addEventListener("click", (event) => {
      event.stopPropagation();
      openPageViewer(page);
    });
    grid.appendChild(item);
  }
  renderSplitSummary();
}

function pageCoordinates(page) {
  const sourceDocument = Math.floor((page - 1) / PAGES_PER_DOCUMENT) + 1;
  const sourcePage = ((page - 1) % PAGES_PER_DOCUMENT) + 1;
  return { sourceDocument, sourcePage, pageNumber: String(sourcePage).padStart(2, "0") };
}

function updatePageViewer() {
  const { sourceDocument, sourcePage, pageNumber } = pageCoordinates(viewerPage);
  const image = $("#viewer-image");
  image.src = `/static/pdf-full/doc-${sourceDocument}-page-${pageNumber}.webp`;
  image.alt = `Bundle page ${viewerPage}, document ${sourceDocument}, source page ${sourcePage}`;
  $("#viewer-title").textContent = `Bundle page ${viewerPage} · Document ${sourceDocument}`;
  $("#viewer-source").textContent = `lorem_ipsum_english_${sourceDocument}.pdf · source page ${sourcePage}`;
  $("#viewer-counter").textContent = `${viewerPage} / ${TOTAL_PAGES}`;
  $("#viewer-prev").disabled = viewerPage === 1;
  $("#viewer-next").disabled = viewerPage === TOTAL_PAGES;
  const boundaryButton = $("#viewer-boundary");
  $("#viewer-canvas").classList.toggle("boundary", boundaries.has(viewerPage));
  boundaryButton.disabled = viewerPage === 1;
  boundaryButton.classList.toggle("active", boundaries.has(viewerPage));
  boundaryButton.innerHTML = boundaries.has(viewerPage)
    ? "<i></i> Document start"
    : "<i></i> Mark document start";
  [viewerPage - 1, viewerPage + 1].filter((page) => page >= 1 && page <= TOTAL_PAGES).forEach((page) => {
    const next = pageCoordinates(page);
    const preload = new Image();
    preload.src = `/static/pdf-full/doc-${next.sourceDocument}-page-${next.pageNumber}.webp`;
  });
}

function openPageViewer(page) {
  viewerPage = page;
  updatePageViewer();
  $("#pdf-viewer").classList.remove("hidden");
  document.body.style.overflow = "hidden";
  $("#viewer-close").focus();
}

function closePageViewer() {
  $("#pdf-viewer").classList.add("hidden");
  document.body.style.overflow = "";
}

function movePageViewer(direction) {
  viewerPage = Math.min(TOTAL_PAGES, Math.max(1, viewerPage + direction));
  updatePageViewer();
}

$("#viewer-prev").addEventListener("click", () => movePageViewer(-1));
$("#viewer-next").addEventListener("click", () => movePageViewer(1));
$("#viewer-close").addEventListener("click", closePageViewer);
$("#viewer-boundary").addEventListener("click", () => {
  toggleBoundary(viewerPage);
  updatePageViewer();
});
$("#viewer-image").addEventListener("click", () => {
  toggleBoundary(viewerPage);
  updatePageViewer();
});
$("#viewer-browser-fullscreen").addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await $("#pdf-viewer").requestFullscreen();
  } catch (_) {
    showToast("Browser fullscreen is unavailable.");
  }
});
document.addEventListener("keydown", async (event) => {
  if ($("#pdf-viewer").classList.contains("hidden")) return;
  if (event.key === "ArrowLeft") movePageViewer(-1);
  if (event.key === "ArrowRight") movePageViewer(1);
  if (event.key === "Escape") {
    if (document.fullscreenElement) await document.exitFullscreen();
    else closePageViewer();
  }
});

function toggleBoundary(page) {
  if (page === 1) {
    showToast("Page 1 always starts the first document.");
    return;
  }
  if (boundaries.has(page)) boundaries.delete(page);
  else boundaries.add(page);
  renderPages();
}

function renderSplitSummary() {
  const starts = [...boundaries].sort((a, b) => a - b);
  $("#document-total").textContent = `${starts.length} document${starts.length === 1 ? "" : "s"}`;
  $("#split-list").innerHTML = starts.map((start, index) => {
    const end = (starts[index + 1] || TOTAL_PAGES + 1) - 1;
    return `<div class="split-doc"><span>0${index + 1}</span><div><b>${documentNames[index] || `Document ${index + 1}`}</b><small>Pages ${start}–${end} · ${end - start + 1} pages</small></div></div>`;
  }).join("");
}

$("#reset-boundaries").addEventListener("click", () => {
  boundaries = new Set(DEFAULT_BOUNDARIES);
  renderPages();
  showToast("Demo boundaries restored.");
});
$("#confirm-split").addEventListener("click", () => {
  showToast(`${boundaries.size} documents ready. Opening pipeline simulation…`);
  setTimeout(() => openView("pipeline"), 650);
});

function setZoom(next) {
  zoom = Math.min(115, Math.max(65, next));
  $("#zoom-value").textContent = `${zoom}%`;
  $("#page-grid").style.setProperty("--page-width", `${Math.round(205 * zoom / 100)}px`);
}
$("#zoom-in").addEventListener("click", () => setZoom(zoom + 10));
$("#zoom-out").addEventListener("click", () => setZoom(zoom - 10));

const uploadZone = $("#upload-zone");
const pdfInput = $("#pdf-input");
uploadZone.addEventListener("click", () => {
  if (uploadZone.classList.contains("upload-disabled")) {
    showToast("Upload disabled in this portfolio demo.");
    return;
  }
  pdfInput.click();
});
["dragenter", "dragover"].forEach((type) => uploadZone.addEventListener(type, (event) => {
  event.preventDefault();
  if (uploadZone.classList.contains("upload-disabled")) return;
  uploadZone.classList.add("dragover");
}));
["dragleave", "drop"].forEach((type) => uploadZone.addEventListener(type, (event) => {
  event.preventDefault();
  uploadZone.classList.remove("dragover");
}));
uploadZone.addEventListener("drop", (event) => {
  if (uploadZone.classList.contains("upload-disabled")) {
    showToast("Upload disabled in this portfolio demo.");
    return;
  }
  useUploadedFile(event.dataTransfer.files[0]);
});
pdfInput.addEventListener("change", () => useUploadedFile(pdfInput.files[0]));

function useUploadedFile(file) {
  if (!file) return;
  if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
    showToast("Choose a PDF file.");
    return;
  }
  showToast("File selected. Portfolio preview remains on the supplied 75-page demo bundle.");
}

const pipelineEvents = [
  "Bundle received and checksum verified.",
  "75 pages extracted from source PDF bundle.",
  "5 document boundaries detected with 84.6% confidence.",
  "Boundary sequence passed consistency validation.",
  "5 isolated PDF documents generated.",
  "Export blocked: document download is disabled in this public demo.",
];

function pipelineTimestamp(startedAt) {
  const elapsed = Date.now() - startedAt;
  const seconds = Math.floor(elapsed / 1000).toString().padStart(2, "0");
  const millis = (elapsed % 1000).toString().padStart(3, "0");
  return `00:${seconds}.${millis}`;
}

function appendPipelineEvent(time, message, className = "") {
  const row = document.createElement("div");
  row.className = className;
  row.innerHTML = `<time>${time}</time><span>${message}</span>`;
  $("#event-terminal").appendChild(row);
  $("#event-terminal").scrollTop = $("#event-terminal").scrollHeight;
}

async function runPipeline() {
  if (pipelineRunning) return;
  pipelineRunning = true;
  const button = $("#run-pipeline");
  const status = $("#pipeline-status");
  const startedAt = Date.now();
  button.disabled = true;
  button.innerHTML = '<span class="play">●</span> Simulation running';
  status.className = "status processing";
  status.innerHTML = "<i></i> Processing";
  $("#pipeline-time").textContent = "Running step 1 of 6";
  $("#pipeline-progress-fill").style.width = "0%";
  $("#event-terminal").innerHTML = "";
  $("#output-empty").classList.remove("hidden");
  $("#generated-files").classList.add("hidden");
  $$(".pipeline-step").forEach((step) => step.classList.remove("active", "done", "blocked"));
  $$(".pipeline-connector").forEach((line) => line.classList.remove("done"));

  for (let index = 0; index < 6; index += 1) {
    const step = $(`.pipeline-step[data-step="${index}"]`);
    step.classList.add("active");
    $("#pipeline-time").textContent = `Running step ${index + 1} of 6`;
    appendPipelineEvent(pipelineTimestamp(startedAt), `${step.querySelector("b").textContent} started…`, "active");
    await new Promise((resolve) => setTimeout(resolve, 720));
    step.classList.remove("active");
    step.classList.add(index === 5 ? "blocked" : "done");
    const connector = step.nextElementSibling;
    if (connector?.classList.contains("pipeline-connector")) connector.classList.add("done");
    $("#pipeline-progress-fill").style.width = `${((index + 1) / 6) * 100}%`;
    appendPipelineEvent(pipelineTimestamp(startedAt), pipelineEvents[index], index === 5 ? "blocked" : "success");
  }

  status.className = "status review";
  status.innerHTML = "<i></i> Complete · Export locked";
  $("#pipeline-time").textContent = `Processed in ${((Date.now() - startedAt) / 1000).toFixed(1)} sec`;
  $("#output-empty").classList.add("hidden");
  $("#generated-files").classList.remove("hidden");
  button.disabled = false;
  button.innerHTML = '<span class="play">↻</span> Run again';
  pipelineRunning = false;
}
$("#run-pipeline").addEventListener("click", runPipeline);

$$('.date-filter button').forEach((button) => button.addEventListener("click", () => {
  $$('.date-filter button').forEach((item) => item.classList.toggle("active", item === button));
  showToast(`${button.textContent} demo period selected.`);
}));

$(".top-actions .secondary").addEventListener("click", () => showToast("Demo report view opened."));

renderPages();
const initialView = location.hash.slice(1);
openView(["overview", "splitter", "pipeline", "analytics"].includes(initialView) ? initialView : "overview", false);
const requestedPage = Number(new URLSearchParams(location.search).get("page"));
if (Number.isInteger(requestedPage) && requestedPage >= 1 && requestedPage <= TOTAL_PAGES) {
  openView("splitter", false);
  openPageViewer(requestedPage);
}
