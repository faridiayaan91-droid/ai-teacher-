

import asyncio
import json
import logging
import os
import shutil
import threading
import time
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_sock import Sock
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from google import genai

import demo_teacher
import pages
from gemini_session import live_session

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("GeminiLiveApp")

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    logger.warning("GEMINI_API_KEY is not set — the demo teacher will stand "
                   "in for the live voice lesson.")

# Absolute origin used for canonical/OG/sitemap URLs (set in production).
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
SESSION_MAX_AGE_SECONDS = 24 * 3600  # abandoned-session sweep threshold

UPLOAD_FOLDER = "uploads"
MAX_TOTAL_UPLOAD_BYTES = 100 * 1024 * 1024   # whole request cap
MAX_FILE_BYTES = 25 * 1024 * 1024            # per-file cap

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_TOTAL_UPLOAD_BYTES
# The workspace UI is still under active development; pick up template
# edits without a restart. Harmless in production (small stat calls).
app.config["TEMPLATES_AUTO_RELOAD"] = True
sock = Sock(app)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
client = genai.Client(api_key=API_KEY) if API_KEY else None


# --------------------------------------------------------------------------
# Upload validation (pure function — also unit-tested)
# --------------------------------------------------------------------------

def validate_upload_file(filename, size):
    """Return an error string for a rejected file, or None if acceptable.

    Mirrors the browser-side checks in static/js/app.js:
    extension allow-list + 25 MB per-file cap. Path traversal is neutralized
    by secure_filename before anything is written to disk.
    """
    if not filename:
        return "missing filename"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in pages.ALLOWED_EXTENSIONS:
        return (f"unsupported file type '{ext or '(none)'}' — allowed: "
                + ", ".join(sorted(pages.ALLOWED_EXTENSIONS)))
    if size is not None and size > MAX_FILE_BYTES:
        return f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"
    return None


def _file_size(file_storage):
    try:
        file_storage.stream.seek(0, os.SEEK_END)
        return file_storage.stream.tell()
    except Exception:
        return None
    finally:
        file_storage.stream.seek(0)


def _unique_stored_name(session_folder, safe_name):
    """Avoid collisions when two uploads sanitize to the same name."""
    candidate = safe_name
    counter = 1
    while os.path.exists(os.path.join(session_folder, "originals", candidate)):
        stem, ext = os.path.splitext(safe_name)
        candidate = f"{stem}-{counter}{ext}"
        counter += 1
    return candidate


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("home.html", base_url=BASE_URL)


@app.route("/lesson/<session_id>")
def lesson(session_id):
    # The workspace is a private, per-session view of the student's own
    # material: rendered noindex and excluded from the sitemap.
    if pages.get_manifest(UPLOAD_FOLDER, session_id) is None:
        return render_template("home.html", base_url=BASE_URL,
                               missing_lesson=True), 404
    return render_template("lesson.html", session_id=session_id)


@app.route("/session/<session_id>/end", methods=["POST"])
def end_session(session_id):
    """Clear this session's uploads from the server (end-of-lesson cleanup)."""
    folder = os.path.join(UPLOAD_FOLDER, session_id)
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
        logger.info("Cleared session %s at the student's request", session_id)
    return jsonify({"success": True})


@app.route("/robots.txt")
def robots():
    body = ("User-agent: *\n"
            "Allow: /\n"
            "Disallow: /lesson/\n"
            "Disallow: /files/\n"
            "Disallow: /session/\n\n"
            f"Sitemap: {BASE_URL}/sitemap.xml\n")
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/sitemap.xml")
def sitemap():
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f"  <url><loc>{BASE_URL}/</loc></url>\n"
           "</urlset>\n")
    return xml, 200, {"Content-Type": "application/xml; charset=utf-8"}


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "img/favicon.svg",
                               mimetype="image/svg+xml")


@app.route("/upload", methods=["POST"])
def upload():
    text = request.form.get("text", "")
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files and not text.strip():
        return jsonify({"error": "Please provide text or at least one file"}), 400

    # Validate everything BEFORE writing anything to disk: one bad file
    # rejects the request with a clear reason (the browser mirrors these
    # checks, so this path is defense in depth).
    rejections = []
    for file in files:
        error = validate_upload_file(file.filename, _file_size(file))
        if error:
            rejections.append({"filename": file.filename, "reason": error})
    if rejections:
        return jsonify({"error": "Some files were rejected",
                        "rejected": rejections}), 400

    session_id = str(uuid.uuid4())
    session_folder = os.path.join(UPLOAD_FOLDER, session_id)
    os.makedirs(os.path.join(session_folder, "originals"), exist_ok=True)

    upload_order = []
    for file in files:
        original_name = file.filename
        safe_name = secure_filename(os.path.basename(original_name))
        if not safe_name:  # e.g. entirely non-ASCII name
            ext = os.path.splitext(original_name)[1].lower()
            safe_name = "uploaded_file" + ext
        safe_name = _unique_stored_name(session_folder, safe_name)
        file.save(os.path.join(session_folder, "originals", safe_name))
        upload_order.append({"stored": safe_name, "original": original_name})
        logger.info("Stored upload %s as %s", original_name, safe_name)

    if text.strip():
        with open(os.path.join(session_folder, pages.INITIAL_TEXT_NAME), "w",
                  encoding="utf-8") as f:
            f.write(text)

    with open(os.path.join(session_folder, pages.ORDER_NAME), "w",
              encoding="utf-8") as f:
        json.dump(upload_order, f, ensure_ascii=False, indent=2)

    pages.init_manifest(session_folder)
    threading.Thread(target=pages.run_pipeline, args=(session_folder,),
                     daemon=True, name=f"pipeline-{session_id[:8]}").start()

    return jsonify({
        "success": True,
        "session_id": session_id,
        "files": upload_order,
        "text": text,
    })


@app.route("/session/<session_id>/manifest")
def session_manifest(session_id):
    manifest = pages.get_manifest(UPLOAD_FOLDER, session_id)
    if manifest is None:
        return jsonify({"error": "Unknown session"}), 404
    return jsonify(manifest)


@app.route("/files/<session_id>/<path:relative_path>")
def session_file(session_id, relative_path):
    # Only the processed pages/ subfolder is served; send_from_directory
    # additionally refuses any path that escapes the session directory.
    if not relative_path.startswith("pages/"):
        return jsonify({"error": "Not found"}), 404
    folder = os.path.join(UPLOAD_FOLDER, session_id)
    if not os.path.isdir(folder):
        return jsonify({"error": "Unknown session"}), 404
    return send_from_directory(folder, relative_path)


@sock.route("/ws/<session_id>")
def websocket(ws, session_id):
    manifest = pages.get_manifest(UPLOAD_FOLDER, session_id)
    if manifest is None:
        try:
            ws.send('{"type": "error", "message": "Unknown session."}')
        except Exception:
            pass
        return
    try:
        if client is None:
            asyncio.run(demo_teacher.demo_session(ws, session_id,
                                                   UPLOAD_FOLDER))
        else:
            asyncio.run(live_session(ws, session_id, client, UPLOAD_FOLDER))
    except Exception:
        logger.exception("WebSocket crashed")


@app.errorhandler(413)
def too_large(error):
    return jsonify({"error": f"Upload too large — total limit is "
                              f"{MAX_TOTAL_UPLOAD_BYTES // (1024 * 1024)} MB"}), 413


@app.errorhandler(Exception)
def handle_error(error):
    if isinstance(error, HTTPException):
        return error
    logger.exception("Unhandled Flask error")
    return jsonify({"error": str(error)}), 500


def _sweep_old_sessions():
    """Best-effort cleanup of sessions abandoned without an explicit end."""
    now = time.time()
    removed = 0
    try:
        names = os.listdir(UPLOAD_FOLDER)
    except OSError:
        return
    for name in names:
        path = os.path.join(UPLOAD_FOLDER, name)
        try:
            if (os.path.isdir(path)
                    and now - os.path.getmtime(path) > SESSION_MAX_AGE_SECONDS):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Swept %d session folder(s) older than %d hours",
                    removed, SESSION_MAX_AGE_SECONDS // 3600)


if __name__ == "__main__":
    _sweep_old_sessions()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True,
            use_reloader=False)
