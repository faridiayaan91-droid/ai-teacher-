

import asyncio
import json
import logging
import os
import time

from google.genai import types

import pages

logger = logging.getLogger("GeminiLiveApp.live")

# Model identifiers change over time; verified against current Gemini Live
# API docs and release notes. Override with GEMINI_LIVE_MODEL in .env
# without touching the code.
MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
VOICE_NAME = os.getenv("GEMINI_LIVE_VOICE", "Aoede")

# How long the worker waits for the page pipeline to produce page 1
# (or to finish with zero pages) before giving up.
FIRST_PAGE_TIMEOUT_SECONDS = 60

SYSTEM_PROMPT = (
    "You are a patient, warm, one-on-one tutor going through a student's "
    "own class notes with them, page by page, in a live voice conversation.\n"
    "The student's material has been converted into numbered pages; you "
    "receive one page at a time, each introduced with a short text cue that "
    "tells you which page it is.\n"
    "When you receive a page: teach it. Walk through what is on it out loud, "
    "explain the underlying concepts, give a small example or memory hook "
    "where useful, and finish by checking understanding with one quick "
    "question. Do not read the page verbatim unless asked.\n"
    "When you finish explaining a page and the student has no further "
    "questions, tell them they can move on to the next page, then WAIT for "
    "them to confirm — the student controls page turns, never assume they "
    "moved on.\n"
    "The student may interrupt you at any time with a spoken or typed "
    "question, including questions about earlier pages — you remember every "
    "page you have seen in this session.\n"
    "Keep answers focused and conversational, like a real tutor talking, "
    "not like a document reader. Your response is played as audio. "
    "Never end the session yourself; when a turn is complete, wait quietly "
    "for the next student question."
)

# --------------------------------------------------------------------------
# Page feeding
# --------------------------------------------------------------------------

async def send_page_content(session, page, content):
    """Send one page's content to Gemini (one argument per call, as the
    Live API requires)."""
    if page["kind"] == "image":
        await session.send_realtime_input(
            video=types.Blob(data=content, mime_type=page["mime_type"]))
    else:
        await session.send_realtime_input(text=content)


def page_cue(index, total, label, revisited, last, reconnected):
    """The short text cue that tells Gemini what page it is looking at."""
    number = index + 1
    if reconnected:
        lead = (f"[The connection was briefly interrupted. The student is "
                f"again viewing page {number} of {total}: {label}. "
                f"Continue teaching this page.]")
    elif revisited:
        lead = (f"[The student went back to page {number} of {total}: "
                f"{label}, which was already covered. Recap it briefly and "
                f"ask what they would like to revisit.]")
    else:
        lead = (f"[The student is now viewing page {number} of {total}: "
                f"{label}. Teach this page.]")
    if last:
        lead += " This is the final page of the uploaded material."
    return lead


# --------------------------------------------------------------------------
# Browser-side plumbing (from the prototype, plus goto + user_text)
# --------------------------------------------------------------------------

def browser_send(ws, message):
    try:
        ws.send(json.dumps(message))
        return True
    except Exception:
        logger.exception("Could not send message to browser")
        return False


async def browser_reader(ws, queue, stop_event):
    """Read browser messages (JSON text or binary audio) into the queue."""
    while not stop_event.is_set():
        try:
            message = await asyncio.to_thread(ws.receive)
        except Exception:
            break
        if message is None:
            break
        if isinstance(message, str):
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            message_type = data.get("type")
            if message_type == "text":
                question = data.get("text", "").strip()
                if question:
                    await queue.put({"type": "text", "text": question})
            elif message_type == "goto":
                try:
                    index = int(data.get("index"))
                except (TypeError, ValueError):
                    continue
                await queue.put({"type": "goto", "index": index})
            elif message_type == "close":
                await queue.put({"type": "close"})
                break
        elif isinstance(message, bytes):
            await queue.put({"type": "audio", "data": message})
    stop_event.set()


# --------------------------------------------------------------------------
# Gemini-side reader (from the prototype, plus input transcription and
# interruption forwarding)
# --------------------------------------------------------------------------

async def read_gemini_response(session, ws, state, stop_event):
    async for response in session.receive():
        if response.session_resumption_update:
            update = response.session_resumption_update
            if update.resumable and update.new_handle:
                state["handle"] = update.new_handle
        if response.go_away:
            pass  # reconnect loop handles the disconnect when it arrives

        server_content = response.server_content
        if not server_content:
            continue

        if server_content.interrupted:
            if not browser_send(ws, {"type": "interrupted"}):
                stop_event.set()
                return

        if server_content.input_transcription:
            text = server_content.input_transcription.text
            if text and not browser_send(ws, {"type": "user_text",
                                             "text": text}):
                stop_event.set()
                return

        if server_content.model_turn:
            for part in server_content.model_turn.parts:
                if part.inline_data:
                    audio = part.inline_data.data
                    if audio:
                        try:
                            ws.send(audio)
                        except Exception:
                            stop_event.set()
                            return
        if server_content.output_transcription:
            text = server_content.output_transcription.text
            if text:
                if not browser_send(ws, {"type": "text", "text": text}):
                    stop_event.set()
                    return
        if server_content.turn_complete:
            browser_send(ws, {"type": "done"})


# --------------------------------------------------------------------------
# Navigation handling
# --------------------------------------------------------------------------

async def teach_page(session, ws, state, upload_root, session_id, index,
                     reconnected=False):
    """Feed the given page to Gemini and tell the browser it is current.

    Returns True when the page was actually sent; False when it is not
    (yet) available, in which case a page_pending message was sent.
    """
    manifest = pages.get_manifest(upload_root, session_id)
    if manifest is None:
        browser_send(ws, {"type": "error", "message": "Session not found."})
        return False
    page_list = manifest.get("pages", [])
    total = len(page_list)

    if not 0 <= index < total:
        if manifest.get("status") == "processing":
            browser_send(ws, {"type": "page_pending", "index": index})
        else:
            browser_send(ws, {"type": "error",
                              "message": f"Page {index + 1} does not exist."})
        return False

    page = page_list[index]
    revisited = index in state["sent_pages"]
    last = index == total - 1 and manifest.get("status") == "ready"

    if index != state["current_page"] or reconnected:
        try:
            content = pages.load_page_content(
                pages.session_dir(upload_root, session_id), page)
            await send_page_content(session, page, content)
            await session.send_realtime_input(text=page_cue(
                index, total, page["label"], revisited, last, reconnected))
            state["sent_pages"].add(index)
        except Exception:
            logger.exception("Failed feeding page %d", index)
            browser_send(ws, {"type": "error",
                              "message": "Could not load that page."})
            return False

    state["current_page"] = index
    browser_send(ws, {"type": "page_current", "index": index, "total": total})
    return True


async def wait_for_first_page(upload_root, session_id):
    """Block until the pipeline exposes its first page (or finishes with
    none). Returns the manifest, or None on timeout/missing session."""
    deadline = time.monotonic() + FIRST_PAGE_TIMEOUT_SECONDS
    while True:
        manifest = pages.get_manifest(upload_root, session_id)
        if manifest is None:
            return None
        if manifest.get("status") == "error":
            return manifest
        if manifest.get("pages") or manifest.get("status") == "ready":
            return manifest
        if time.monotonic() > deadline:
            return None
        await asyncio.sleep(0.25)


# --------------------------------------------------------------------------
# The worker (prototype structure preserved: reconnect + backoff + resumption)
# --------------------------------------------------------------------------

async def gemini_worker(ws, session_id, queue, stop_event, client,
                        upload_root):
    state = {"handle": None, "first_connection": True,
             "current_page": None, "sent_pages": set()}
    retry_delay = 1

    # Do not open the Gemini connection until page 1 is available (or the
    # pipeline finished with a text-only session).
    manifest = await wait_for_first_page(upload_root, session_id)
    if manifest is None:
        browser_send(ws, {"type": "error",
                          "message": "Your material is still being processed. "
                                     "Please try again in a moment."})
        stop_event.set()
        return
    if manifest.get("status") == "error":
        browser_send(ws, {"type": "error",
                          "message": manifest.get("error")
                          or "Processing failed."})
        stop_event.set()
        return

    while not stop_event.is_set():
        try:
            resumption = types.SessionResumptionConfig(handle=state["handle"])
            config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                input_audio_transcription={},
                output_audio_transcription={},
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=VOICE_NAME))),
                session_resumption=resumption,
                system_instruction=SYSTEM_PROMPT,
            )
            async with client.aio.live.connect(model=MODEL, config=config) \
                    as session:
                browser_send(ws, {"type": "gemini_connected"})
                if state["first_connection"]:
                    initial_text = pages.read_initial_text(
                        pages.session_dir(upload_root, session_id))
                    if initial_text.strip():
                        await session.send_realtime_input(
                            text="Additional context from the student:\n\n"
                                 + initial_text.strip())
                    if manifest.get("pages"):
                        await teach_page(session, ws, state, upload_root,
                                         session_id, 0)
                    else:
                        await session.send_realtime_input(text=(
                            "[The student provided the context above but no "
                            "page files. Begin the lesson: briefly introduce "
                            "the material and ask what they would like to "
                            "focus on.]"))
                    state["first_connection"] = False
                elif state["current_page"] is not None:
                    # Reconnected mid-lesson: re-feed the current page so the
                    # lesson continues even if the resumption handle failed.
                    await teach_page(session, ws, state, upload_root,
                                     session_id, state["current_page"],
                                     reconnected=True)
                retry_delay = 1
                while not stop_event.is_set():
                    browser_task = asyncio.create_task(queue.get())
                    gemini_task = asyncio.create_task(
                        read_gemini_response(session, ws, state, stop_event))
                    done, pending = await asyncio.wait(
                        [browser_task, gemini_task],
                        return_when=asyncio.FIRST_COMPLETED)
                    if browser_task in done:
                        event = browser_task.result()
                        gemini_task.cancel()
                        await asyncio.gather(gemini_task,
                                            return_exceptions=True)
                        event_type = event["type"]
                        if event_type == "close":
                            stop_event.set()
                            break
                        if event_type == "text":
                            await session.send_realtime_input(
                                text=event["text"])
                        elif event_type == "goto":
                            await teach_page(session, ws, state, upload_root,
                                             session_id, event["index"])
                        elif event_type == "audio":
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=event["data"],
                                    mime_type="audio/pcm;rate=16000"))
                    elif gemini_task in done:
                        browser_task.cancel()
                        await asyncio.gather(browser_task,
                                             return_exceptions=True)
                        gemini_task.result()
                        if not stop_event.is_set():
                            break
        except asyncio.CancelledError:
            raise
        except Exception:
            if stop_event.is_set():
                break
            logger.exception("Gemini connection error for %s", session_id)
            browser_send(ws, {"type": "reconnecting",
                              "message": "Gemini connection interrupted. "
                                         "Reconnecting..."})
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 10)


async def live_session(ws, session_id, client, upload_root):
    queue = asyncio.Queue()
    stop_event = asyncio.Event()
    browser_task = asyncio.create_task(
        browser_reader(ws, queue, stop_event))
    gemini_task = asyncio.create_task(
        gemini_worker(ws, session_id, queue, stop_event, client,
                      upload_root))
    try:
        await asyncio.wait([browser_task, gemini_task],
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_event.set()
        browser_task.cancel()
        gemini_task.cancel()
        await asyncio.gather(browser_task, gemini_task,
                             return_exceptions=True)
