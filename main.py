
import os
import uuid
import json
import asyncio
import logging
import mimetypes
import threading

import pymupdf

from flask import Flask, request, jsonify, render_template
from flask_sock import Sock

from dotenv import load_dotenv

from google import genai
from google.genai import types

from docx import Document

from werkzeug.exceptions import HTTPException


# ============================================================
# ENV
# ============================================================

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")

if not API_KEY:
    raise ValueError(
        "GEMINI_API_KEY is missing from .env"
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("GeminiLiveApp")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)
sock = Sock(app)


# ============================================================
# SETTINGS
# ============================================================

UPLOAD_FOLDER = "uploads"

MODEL = "gemini-3.1-flash-live-preview"

os.makedirs(
    UPLOAD_FOLDER,
    exist_ok=True
)


# ============================================================
# GEMINI CLIENT
# ============================================================

client = genai.Client(
    api_key=API_KEY
)

logger.info(
    "Gemini API key loaded successfully"
)

logger.info(
    "Gemini model: %s",
    MODEL
)


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    logger.debug(
        "Home page requested"
    )

    return render_template(
        "index.html"
    )


# ============================================================
# FAVICON
# ============================================================

@app.route("/favicon.ico")
def favicon():

    return "", 204


# ============================================================
# UPLOAD
# ============================================================

@app.route(
    "/upload",
    methods=["POST"]
)
def upload():

    logger.info(
        "========== NEW UPLOAD =========="
    )

    text = request.form.get(
        "text",
        ""
    )

    files = request.files.getlist(
        "files"
    )

    logger.info(
        "Number of files: %d",
        len(files)
    )

    logger.debug(
        "Initial text: %s",
        text
    )


    if not files and not text.strip():

        return jsonify({
            "error": "Please provide text or at least one file"
        }), 400


    # --------------------------------------------------------
    # SESSION
    # --------------------------------------------------------

    session_id = str(
        uuid.uuid4()
    )

    session_folder = os.path.join(
        UPLOAD_FOLDER,
        session_id
    )

    os.makedirs(
        session_folder,
        exist_ok=True
    )

    logger.info(
        "Created session: %s",
        session_id
    )


    # --------------------------------------------------------
    # SAVE FILES
    # --------------------------------------------------------

    saved_files = []


    for file in files:

        if not file.filename:

            continue


        filename = os.path.basename(
            file.filename
        )


        path = os.path.join(
            session_folder,
            filename
        )


        file.save(
            path
        )


        mime_type = (
            mimetypes.guess_type(
                filename
            )[0]
            or "application/octet-stream"
        )


        saved_files.append({

            "filename": filename,

            "mime_type": mime_type

        })


        logger.info(
            "Saved: %s | %s",
            filename,
            mime_type
        )


    # --------------------------------------------------------
    # SAVE INITIAL TEXT
    # --------------------------------------------------------

    if text.strip():

        text_path = os.path.join(
            session_folder,
            "_initial_text.txt"
        )


        with open(
            text_path,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(text)


    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return jsonify({

        "success": True,

        "session_id": session_id,

        "files": saved_files,

        "text": text

    })


# ============================================================
# SESSION FILES
# ============================================================

def get_session_files(session_id):

    folder = os.path.join(
        UPLOAD_FOLDER,
        session_id
    )


    if not os.path.exists(folder):

        return []


    result = []


    for filename in os.listdir(folder):

        if filename == "_initial_text.txt":

            continue


        path = os.path.join(
            folder,
            filename
        )


        if not os.path.isfile(path):

            continue


        mime_type = (
            mimetypes.guess_type(
                filename
            )[0]
            or "application/octet-stream"
        )


        result.append({

            "filename": filename,

            "path": path,

            "mime_type": mime_type

        })


    return result


# ============================================================
# INITIAL TEXT
# ============================================================

def get_initial_text(session_id):

    path = os.path.join(
        UPLOAD_FOLDER,
        session_id,
        "_initial_text.txt"
    )


    if not os.path.exists(path):

        return ""


    with open(
        path,
        "r",
        encoding="utf-8"
    ) as f:

        return f.read()


# ============================================================
# PREPARE FILE
# ============================================================

def prepare_file(file_info):

    filename = file_info["filename"]

    path = file_info["path"]

    mime_type = file_info["mime_type"]


    logger.info(
        "Preparing file: %s",
        filename
    )


    # ========================================================
    # IMAGE
    # ========================================================

    if mime_type.startswith("image/"):

        with open(
            path,
            "rb"
        ) as f:

            data = f.read()


        logger.info(
            "Image ready: %s (%d bytes)",
            filename,
            len(data)
        )


        return {

            "kind": "image",

            "data": data,

            "mime_type": mime_type

        }


    # ========================================================
    # PDF
    # ========================================================

    if mime_type == "application/pdf":

        logger.info(
            "PDF detected: %s",
            filename
        )


        pdf = pymupdf.open(
            path
        )


        if len(pdf) == 0:

            pdf.close()

            raise ValueError(
                "PDF contains no pages"
            )


        # ----------------------------------------------------
        # ONLY FIRST PAGE
        # ----------------------------------------------------

        page = pdf[0]


        logger.info(
            "Rendering first PDF page"
        )


        pix = page.get_pixmap(
            matrix=pymupdf.Matrix(
                2,
                2
            ),
            alpha=False
        )


        image_data = pix.tobytes(
            "png"
        )


        pdf.close()


        logger.info(
            "PDF first page converted to PNG: %d bytes",
            len(image_data)
        )


        return {

            "kind": "image",

            "data": image_data,

            "mime_type": "image/png"

        }


    # ========================================================
    # DOCX
    # ========================================================

    if mime_type == (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ):

        logger.info(
            "DOCX detected: %s",
            filename
        )


        doc = Document(
            path
        )


        paragraphs = []


        for paragraph in doc.paragraphs:

            value = paragraph.text.strip()


            if value:

                paragraphs.append(
                    value
                )


        text = "\n".join(
            paragraphs
        )


        logger.info(
            "DOCX extracted characters: %d",
            len(text)
        )


        return {

            "kind": "text",

            "data": (
                f"Document: {filename}\n\n"
                f"{text}"
            )

        }


    # ========================================================
    # TXT / TEXT
    # ========================================================

    if mime_type.startswith("text/"):

        with open(
            path,
            "r",
            encoding="utf-8",
            errors="ignore"
        ) as f:

            text = f.read()


        return {

            "kind": "text",

            "data": (
                f"File: {filename}\n\n"
                f"{text}"
            )

        }


    # ========================================================
    # UNKNOWN FILE
    # ========================================================

    logger.warning(
        "Unsupported file type: %s",
        mime_type
    )


    return None


# ============================================================
# SEND FILE
# ============================================================

async def send_file(
    session,
    file_info
):

    prepared = prepare_file(
        file_info
    )


    if not prepared:

        logger.warning(
            "Skipping unsupported file: %s",
            file_info["filename"]
        )

        return


    # ========================================================
    # IMAGE
    # ========================================================

    if prepared["kind"] == "image":

        await session.send_realtime_input(

            video=types.Blob(

                data=prepared["data"],

                mime_type=prepared["mime_type"]

            )

        )


        logger.info(
            "Image sent to Gemini: %s",
            file_info["filename"]
        )


    # ========================================================
    # TEXT
    # ========================================================

    elif prepared["kind"] == "text":

        await session.send_realtime_input(

            text=prepared["data"]

        )


        logger.info(
            "Text sent to Gemini: %s",
            file_info["filename"]
        )


# ============================================================
# SEND ALL INITIAL CONTEXT
# ============================================================

async def send_initial_context(
    session,
    session_id
):

    files = get_session_files(
        session_id
    )


    logger.info(
        "Sending %d files as initial context",
        len(files)
    )


    for file_info in files:

        try:

            await send_file(
                session,
                file_info
            )

        except Exception:

            logger.exception(
                "Failed to send %s",
                file_info["filename"]
            )


    initial_text = get_initial_text(
        session_id
    )


    if initial_text.strip():

        logger.info(
            "Sending initial user text"
        )


        await session.send_realtime_input(

            text=(
                "Additional context from the user:\n\n"
                + initial_text
            )

        )


    logger.info(
        "Initial context finished"
    )


# ============================================================
# SEND BROWSER MESSAGE
# ============================================================

def browser_send(
    ws,
    message
):

    try:

        ws.send(
            json.dumps(message)
        )

        return True

    except Exception:

        logger.exception(
            "Could not send message to browser"
        )

        return False


# ============================================================
# BROWSER → QUEUE
# ============================================================

async def browser_reader(
    ws,
    queue,
    stop_event
):

    logger.info(
        "Browser reader started"
    )


    while not stop_event.is_set():

        try:

            message = await asyncio.to_thread(
                ws.receive
            )

        except Exception:

            logger.exception(
                "Browser receive failed"
            )

            break


        if message is None:

            logger.info(
                "Browser disconnected"
            )

            break


        # ====================================================
        # TEXT JSON
        # ====================================================

        if isinstance(
            message,
            str
        ):

            try:

                data = json.loads(
                    message
                )

            except json.JSONDecodeError:

                logger.warning(
                    "Invalid JSON from browser"
                )

                continue


            message_type = data.get(
                "type"
            )


            # ------------------------------------------------
            # QUESTION
            # ------------------------------------------------

            if message_type == "text":

                question = data.get(
                    "text",
                    ""
                ).strip()


                if not question:

                    continue


                logger.info(
                    "USER QUESTION: %s",
                    question
                )


                await queue.put({

                    "type": "text",

                    "text": question

                })


            # ------------------------------------------------
            # CLOSE
            # ------------------------------------------------

            elif message_type == "close":

                logger.info(
                    "User requested close"
                )


                await queue.put({

                    "type": "close"

                })

                break


            else:

                logger.warning(
                    "Unknown browser message: %s",
                    message_type
                )


        # ====================================================
        # BINARY AUDIO
        # ====================================================

        elif isinstance(
            message,
            bytes
        ):

            logger.debug(
                "Browser audio: %d bytes",
                len(message)
            )


            await queue.put({

                "type": "audio",

                "data": message

            })


    stop_event.set()


    logger.info(
        "Browser reader stopped"
    )


# ============================================================
# HANDLE ONE GEMINI RESPONSE
# ============================================================

async def read_gemini_response(
    session,
    ws,
    state,
    stop_event
):

    logger.info(
        "Gemini response listener active"
    )


    async for response in session.receive():

        # ====================================================
        # SESSION RESUMPTION
        # ====================================================

        if response.session_resumption_update:

            update = (
                response
                .session_resumption_update
            )


            logger.debug(
                "Session resumption update: resumable=%s",
                update.resumable
            )


            if (
                update.resumable
                and update.new_handle
            ):

                state["handle"] = (
                    update.new_handle
                )


                logger.info(
                    "Saved new Gemini resumption handle"
                )


        # ====================================================
        # GO AWAY
        # ====================================================

        if response.go_away:

            logger.warning(
                "Gemini sent GO_AWAY"
            )


            try:

                time_left = response.go_away.time_left

                logger.warning(
                    "Gemini connection will close soon: %s",
                    time_left
                )

            except Exception:

                pass


        # ====================================================
        # SERVER CONTENT
        # ====================================================

        server_content = (
            response.server_content
        )


        if not server_content:

            continue


        # ====================================================
        # MODEL TURN
        # ====================================================

        if server_content.model_turn:

            for part in (
                server_content
                .model_turn
                .parts
            ):

                # --------------------------------------------
                # AUDIO
                # --------------------------------------------

                if part.inline_data:

                    audio = (
                        part.inline_data.data
                    )


                    if audio:

                        logger.debug(
                            "Gemini audio: %d bytes",
                            len(audio)
                        )


                        try:

                            ws.send(
                                audio
                            )

                        except Exception:

                            logger.exception(
                                "Browser audio send failed"
                            )

                            stop_event.set()

                            return


                # --------------------------------------------
                # TEXT
                # --------------------------------------------

                if part.text:

                    logger.debug(
                        "Gemini text: %s",
                        part.text
                    )


        # ====================================================
        # OUTPUT TRANSCRIPTION
        # ====================================================

        if server_content.output_transcription:

            text = (
                server_content
                .output_transcription
                .text
            )


            if text:

                logger.info(
                    "Gemini transcript: %s",
                    text
                )


                if not browser_send(

                    ws,

                    {
                        "type": "text",

                        "text": text

                    }

                ):

                    stop_event.set()

                    return


        # ====================================================
        # TURN COMPLETE
        # ====================================================

        if server_content.turn_complete:

            logger.info(
                "========== TURN COMPLETE =========="
            )

            logger.info(
                "Gemini is still connected and waiting"
            )


            browser_send(

                ws,

                {
                    "type": "done"

                }

            )


# ============================================================
# GEMINI WORKER
# ============================================================

async def gemini_worker(
    ws,
    session_id,
    queue,
    stop_event
):

    state = {

        "handle": None,

        "first_connection": True

    }


    retry_delay = 1


    while not stop_event.is_set():

        session = None


        try:

            # =================================================
            # CONFIG
            # =================================================

            resumption = types.SessionResumptionConfig(

                handle=state["handle"],

        

            )


            config = types.LiveConnectConfig(

                response_modalities=[

                    "AUDIO"

                ],

                output_audio_transcription={},

                session_resumption=resumption,

                system_instruction=(

                    "You are an AI teacher and voice assistant. "

                    "The user can upload documents and images. "

                    "Use those files as context when answering. "

                    "For PDF documents, only the first page is "
                    "provided to you. "

                    "Answer naturally and clearly. "

                    "Your response is played as audio. "

                    "Do not unnecessarily say that you are "
                    "reading a document. "

                    "Remember previous questions and answers. "

                    "The user can ask unlimited follow-up questions. "

                    "IMPORTANT: When a turn is complete, "
                    "wait for the next user question. "
                    "Do not end the session yourself."

                )

            )


            if state["handle"]:

                logger.info(
                    "========================================"
                )

                logger.info(
                    "RECONNECTING GEMINI SESSION"
                )

                logger.info(
                    "Using session resumption handle"
                )

                logger.info(
                    "========================================"

                )

            else:

                logger.info(
                    "========================================"
                )

                logger.info(
                    "STARTING NEW GEMINI LIVE SESSION"
                )

                logger.info(
                    "========================================"
                )


            # =================================================
            # CONNECT
            # =================================================

            async with client.aio.live.connect(

                model=MODEL,

                config=config

            ) as session:

                logger.info(
                    "GEMINI LIVE CONNECTED"
                )


                browser_send(

                    ws,

                    {

                        "type": "gemini_connected"

                    }

                )


                # =================================================
                # INITIAL CONTEXT ONLY ON FIRST CONNECTION
                # =================================================

                if state["first_connection"]:

                    await send_initial_context(

                        session,

                        session_id

                    )


                    state["first_connection"] = False


                retry_delay = 1


                # =================================================
                # PROCESS UNTIL CONNECTION ENDS
                # =================================================

                while not stop_event.is_set():

                    # ---------------------------------------------
                    # Wait for either:
                    #
                    # 1. Browser question
                    # 2. Gemini response
                    #
                    # ---------------------------------------------

                    browser_task = asyncio.create_task(

                        queue.get()

                    )


                    gemini_task = asyncio.create_task(

                        read_gemini_response(

                            session,

                            ws,

                            state,

                            stop_event

                        )

                    )


                    done, pending = await asyncio.wait(

                        [
                            browser_task,
                            gemini_task
                        ],

                        return_when=asyncio.FIRST_COMPLETED

                    )


                    # ---------------------------------------------
                    # BROWSER EVENT
                    # ---------------------------------------------

                    if browser_task in done:

                        event = browser_task.result()


                        gemini_task.cancel()


                        await asyncio.gather(

                            gemini_task,

                            return_exceptions=True

                        )


                        event_type = event["type"]


                        # =========================================
                        # CLOSE
                        # =========================================

                        if event_type == "close":

                            logger.info(
                                "Closing Gemini session because "
                                "browser requested it"
                            )


                            stop_event.set()

                            break


                        # =========================================
                        # TEXT
                        # =========================================

                        if event_type == "text":

                            question = event["text"]


                            logger.info(
                                "Sending question to Gemini: %s",
                                question
                            )


                            await session.send_realtime_input(

                                text=question

                            )


                            logger.info(
                                "Question sent successfully"
                            )


                        # =========================================
                        # AUDIO
                        # =========================================

                        elif event_type == "audio":

                            await session.send_realtime_input(

                                audio=types.Blob(

                                    data=event["data"],

                                    mime_type=(
                                        "audio/pcm;rate=16000"
                                    )

                                )

                            )


                    # ---------------------------------------------
                    # GEMINI EVENT
                    # ---------------------------------------------

                    elif gemini_task in done:

                        browser_task.cancel()


                        await asyncio.gather(

                            browser_task,

                            return_exceptions=True

                        )


                        try:

                            gemini_task.result()

                        except Exception:

                            raise


                        # ------------------------------------------------
                        # If the Gemini receive loop ended normally,
                        # reconnect.
                        # ------------------------------------------------

                        if not stop_event.is_set():

                            logger.warning(
                                "Gemini receive loop ended"
                            )


                            break


        # =========================================================
        # CONNECTION ERROR
        # =========================================================

        except asyncio.CancelledError:

            logger.info(
                "Gemini worker cancelled"
            )

            raise


        except Exception as error:

            if stop_event.is_set():

                break


            logger.exception(
                "Gemini Live connection error: %s",
                error
            )


            browser_send(

                ws,

                {

                    "type": "reconnecting",

                    "message": (
                        "Gemini connection interrupted. "
                        "Reconnecting..."
                    )

                }

            )


            # =====================================================
            # RECONNECT
            # =====================================================

            logger.info(
                "Reconnecting in %d seconds...",
                retry_delay
            )


            await asyncio.sleep(
                retry_delay
            )


            retry_delay = min(
                retry_delay * 2,
                10
            )


    logger.info(
        "Gemini worker stopped"
    )


# ============================================================
# LIVE SESSION
# ============================================================

async def live_session(
    ws,
    session_id
):

    logger.info(
        "========================================"
    )

    logger.info(
        "LIVE SESSION START"
    )

    logger.info(
        "Session ID: %s",
        session_id
    )

    logger.info(
        "========================================"
    )


    queue = asyncio.Queue()


    stop_event = asyncio.Event()


    # ========================================================
    # START TASKS
    # ========================================================

    browser_task = asyncio.create_task(

        browser_reader(

            ws,

            queue,

            stop_event

        )

    )


    gemini_task = asyncio.create_task(

        gemini_worker(

            ws,

            session_id,

            queue,

            stop_event

        )

    )


    try:

        await asyncio.wait(

            [
                browser_task,
                gemini_task
            ],

            return_when=asyncio.FIRST_COMPLETED

        )


    finally:

        stop_event.set()


        browser_task.cancel()

        gemini_task.cancel()


        await asyncio.gather(

            browser_task,

            gemini_task,

            return_exceptions=True

        )


        logger.info(
            "Live session cleanup complete"
        )


# ============================================================
# WEBSOCKET
# ============================================================

@sock.route(
    "/ws/<session_id>"
)
def websocket(
    ws,
    session_id
):

    logger.info(
        "========================================"
    )

    logger.info(
        "NEW BROWSER WEBSOCKET"
    )

    logger.info(
        "Session ID: %s",
        session_id
    )

    logger.info(
        "========================================"
    )


    try:

        asyncio.run(

            live_session(

                ws,

                session_id

            )

        )


    except Exception:

        logger.exception(
            "WebSocket crashed"
        )


    finally:

        logger.info(
            "Browser WebSocket closed"
        )


# ============================================================
# ERROR HANDLER
# ============================================================

@app.errorhandler(Exception)
def handle_error(error):

    if isinstance(
        error,
        HTTPException
    ):

        return error


    logger.exception(
        "Unhandled Flask error"
    )


    return jsonify({

        "error": str(error)

    }), 500


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    logger.info(
        "========================================"
    )

    logger.info(
        "STARTING AI TEACHER"
    )

    logger.info(
        "Model: %s",
        MODEL
    )

    logger.info(
        "Server: http://127.0.0.1:5000"
    )

    logger.info(
        "Flask reloader: OFF"
    )

    logger.info(
        "========================================"
    )


    app.run(

        host="127.0.0.1",

        port=5000,

        debug=False,

        threaded=True,

        use_reloader=False

    )

