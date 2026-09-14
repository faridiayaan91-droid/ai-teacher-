
import asyncio
import logging
import re

import pages
from gemini_session import browser_reader, browser_send, wait_for_first_page

logger = logging.getLogger("GeminiLiveApp.demo")


def _lead(text, limit=240):
    """First sentence-ish window of a text page, for the demo teacher's line."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    if not cut.endswith((".", "!", "?")):
        cut += "…"
    return cut


def page_line(index, total, page, content, initial_text):
    number = index + 1
    source = page.get("source_file") or "your notes"
    if page["kind"] == "image":
        body = (f"Page {number} of {total} is a page image from {source}. "
                "In a live lesson I read it directly. For now, look it over "
                "and tell me what stands out on it.")
    else:
        lead = _lead(content) or "This page looks mostly empty."
        body = (f"Page {number} of {total} starts like this: {lead} "
                "That is the gist of it. What do you make of it so far?")
    prefix = ""
    if index == 0 and initial_text.strip():
        prefix = (f"You asked me to: {initial_text.strip()}. "
                  "I will keep that in mind as we go. ")
    suffix = (" When you are ready for the next page, say the word."
              if number < total else
              " That is the last page, so ask me anything before we finish.")
    return prefix + body + suffix


def question_reply():
    return ("Good question. In this demo I can only follow the pages. Add a "
            "Gemini API key and I will answer it properly, out loud. Shall "
            "we keep going with the page in front of you?")


async def _teach(ws, upload_root, session_id, index, initial_text):
    manifest = pages.get_manifest(upload_root, session_id)
    if manifest is None:
        browser_send(ws, {"type": "error", "message": "Session not found."})
        return
    page_list = manifest.get("pages", [])
    total = len(page_list)

    if not 0 <= index < total:
        if manifest.get("status") == "processing":
            browser_send(ws, {"type": "page_pending", "index": index})
        else:
            browser_send(ws, {"type": "error",
                              "message": f"Page {index + 1} does not exist."})
        return

    page = page_list[index]
    try:
        content = pages.load_page_content(
            pages.session_dir(upload_root, session_id), page)
    except Exception:
        logger.exception("Demo teacher could not load page %d", index)
        browser_send(ws, {"type": "error",
                          "message": "Could not load that page."})
        return

    browser_send(ws, {"type": "page_current", "index": index, "total": total})
    browser_send(ws, {"type": "text",
                      "text": page_line(index, total, page, content,
                                        initial_text)})


async def demo_worker(ws, session_id, queue, stop_event, upload_root):
    state = {"current": 0}

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

    browser_send(ws, {"type": "gemini_connected"})
    browser_send(ws, {"type": "mode", "mode": "demo"})

    initial_text = pages.read_initial_text(
        pages.session_dir(upload_root, session_id))

    if manifest.get("pages"):
        await _teach(ws, upload_root, session_id, 0, initial_text)
    else:
        note = initial_text.strip()
        browser_send(ws, {"type": "text", "text": (
            "There are no pages in this lesson, "
            + (f"but you left me a note: {note}. " if note else
               "and no note either. ")
            + "Start talking, and I will follow along as best a demo can.")})

    while not stop_event.is_set():
        event = await queue.get()
        if event["type"] == "close":
            stop_event.set()
            break
        if event["type"] == "goto":
            state["current"] = event["index"]
            await _teach(ws, upload_root, session_id, state["current"],
                         initial_text)
        elif event["type"] == "text":
            browser_send(ws, {"type": "text", "text": question_reply()})
        # "audio" events (mic PCM) are ignored in demo mode.


async def demo_session(ws, session_id, upload_root):
    queue = asyncio.Queue()
    stop_event = asyncio.Event()
    browser_task = asyncio.create_task(
        browser_reader(ws, queue, stop_event))
    demo_task = asyncio.create_task(
        demo_worker(ws, session_id, queue, stop_event, upload_root))
    try:
        await asyncio.wait([browser_task, demo_task],
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_event.set()
        browser_task.cancel()
        demo_task.cancel()
        await asyncio.gather(browser_task, demo_task,
                             return_exceptions=True)
