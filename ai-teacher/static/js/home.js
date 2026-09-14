/*
 * home.js — the AI Lecture start screen (no framework, no build step).
 *
 * Mirrors the server-side validation in app.py: extension allow-list,
 * 25 MB per file, 100 MB total. On submit it POSTs to /upload and sends
 * the student to /lesson/<session_id>.
 */
"use strict";

(function () {
  const ALLOWED_EXT = ["pdf", "docx", "txt", "md", "png", "jpg", "jpeg",
                       "webp", "gif", "bmp"];
  const MAX_FILE_MB = 25;
  const MAX_TOTAL_MB = 100;

  const $ = (id) => document.getElementById(id);

  const state = { files: [], submitting: false };

  /* ----- tiny helpers ----- */

  function extOf(name) {
    const dot = name.lastIndexOf(".");
    return dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
  }

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function validateFile(file) {
    const ext = extOf(file.name);
    if (!ALLOWED_EXT.includes(ext)) {
      return `can't read "${ext ? "." + ext : file.name}" files — use PDF, Word, a photo, or plain text`;
    }
    if (file.size > MAX_FILE_MB * 1024 * 1024) {
      return `is ${formatSize(file.size)}, over the ${MAX_FILE_MB} MB limit — try a photo of the pages instead`;
    }
    return null;
  }

  /* ----- the file list ----- */

  function addFiles(fileList) {
    const errors = [];
    for (const file of fileList) {
      const error = validateFile(file);
      if (error) { errors.push(`"${file.name}" ${error}`); continue; }
      if (state.files.some((f) => f.name === file.name
                           && f.size === file.size)) {
        continue; // exact duplicate
      }
      state.files.push(file);
    }
    renderFiles();
    if (errors.length) showErrors(errors);
    else $("upload-errors").hidden = true;
    refreshStart();
  }

  function renderFiles() {
    const list = $("file-chips");
    list.innerHTML = "";
    state.files.forEach((file, i) => {
      const li = document.createElement("li");

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = file.name;
      li.appendChild(name);

      const size = document.createElement("span");
      size.className = "size";
      size.textContent = formatSize(file.size);
      li.appendChild(size);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove";
      remove.textContent = "\u00d7";
      remove.setAttribute("aria-label", `Remove ${file.name}`);
      remove.addEventListener("click", () => {
        state.files.splice(i, 1);
        renderFiles();
        refreshStart();
      });
      li.appendChild(remove);

      list.appendChild(li);
    });
    list.hidden = state.files.length === 0;

    $("file-summary").textContent = state.files.length
      ? `${state.files.length} file${state.files.length > 1 ? "s" : ""} ready`
      : "";
  }

  function showErrors(errors) {
    const list = $("upload-errors");
    list.innerHTML = "";
    errors.forEach((message) => {
      const li = document.createElement("li");
      li.textContent = message;
      list.appendChild(li);
    });
    list.hidden = false;
  }

  function refreshStart() {
    const total = state.files.reduce((sum, f) => sum + f.size, 0);
    if (state.files.length && total > MAX_TOTAL_MB * 1024 * 1024) {
      showErrors([`together those files pass ${MAX_TOTAL_MB} MB — trim the set and try again`]);
      $("start-btn").disabled = true;
      return;
    }
    if (!state.files.length) $("upload-errors").hidden = true;
    $("start-btn").disabled =
      state.files.length === 0 && !$("note").value.trim();
  }

  /* ----- submit ----- */

  async function submit(event) {
    event.preventDefault();
    if (state.submitting) return;

    const button = $("start-btn");
    const label = $("start-label");
    state.submitting = true;
    button.disabled = true;
    label.textContent = "Setting things up";

    const formData = new FormData();
    state.files.forEach((file) => formData.append("files", file));
    formData.append("text", $("note").value);

    try {
      const response = await fetch("/upload", {
        method: "POST", body: formData });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.session_id) {
        const detail = payload.rejected
          ? " (" + payload.rejected.map((r) => r.filename).join(", ") + ")"
          : "";
        showErrors([payload.error
                    || `the upload didn't go through (${response.status})${detail}`]);
        state.submitting = false;
        button.disabled = false;
        label.textContent = "Start the lesson";
        return;
      }
      window.location.href = `/lesson/${payload.session_id}`;
    } catch {
      showErrors(["couldn't reach the server. Check the connection and try again."]);
      state.submitting = false;
      button.disabled = false;
      label.textContent = "Start the lesson";
    }
  }

  /* ----- wiring ----- */

  $("file-input").addEventListener("change", (event) => {
    addFiles(event.target.files);
    event.target.value = "";
  });

  const dropzone = $("dropzone");
  ["dragenter", "dragover"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragover");
    }));
  ["dragleave", "drop"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragover");
    }));
  dropzone.addEventListener("drop", (event) => {
    if (event.dataTransfer && event.dataTransfer.files.length) {
      addFiles(event.dataTransfer.files);
    }
  });

  $("note").addEventListener("input", refreshStart);
  $("lesson-form").addEventListener("submit", submit);

  /* the quiet confirmation note after a lesson ends */
  const params = new URLSearchParams(window.location.search);
  if (params.get("ended") === "1") {
    const notice = document.createElement("p");
    notice.className = "notice";
    notice.setAttribute("role", "status");
    notice.textContent = "Lesson ended, and your files were cleared. Start " +
      "another whenever you're ready.";
    const sheet = document.querySelector(".sheet");
    sheet.insertBefore(notice, sheet.querySelector(".title"));
  }

  refreshStart();
})();
