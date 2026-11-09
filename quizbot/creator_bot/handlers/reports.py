"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from functools import partial
from io import BytesIO

from pyrogram import Client, filters
from pyrogram.types import Message

from quizbot.database import QuizRepository, UserRepository, get_db
from quizbot.shared import config
from quizbot.shared.html.quiz_report import render_quiz_html, render_notes_html
from quizbot.shared.utils import is_premium_user
from quizbot.shared.utils.http import get_session, request_json
from quizbot.runner_bot.pdf_reports import render_quiz_pdf, render_notes_pdf

from ..ratelimit import ratelimit
from .file_import import process_uploaded_file

logger = logging.getLogger(__name__)

_TSR_LOCK = asyncio.Lock()

# pending_txt_actions: uid -> "pdf" | "html" | "set" | "aiset" | "notes"
pending_txt_actions = {}


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────

def _doc_fname(doc) -> str:
    """doc.file_name can be None on some forwarded files."""
    return (getattr(doc, "file_name", None) or "").strip()


def _safe_stem(fname: str, fallback: str = "notes") -> str:
    stem = (fname or "").rsplit(".", 1)[0].strip() or fallback
    stem = "".join(c for c in stem if c.isalnum() or c in (" ", "-", "_")).strip()
    return stem or fallback


def _allowed_ext(fname: str, allow_exts: tuple[str, ...]) -> bool:
    if not fname:
        return True  # Telegram document without a name — trust the user
    low = fname.lower()
    return any(low.endswith(ext) for ext in allow_exts)


async def _decode_bytes(raw: bytes) -> str | None:
    """Try common encodings; return None if all fail."""
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


# ──────────────────────────────────────────────────────────────────────
# /whtml  /wpdf  /wset  (existing — unchanged behaviour)
# ──────────────────────────────────────────────────────────────────────

@ratelimit("default")
async def whtml_cmd(c: Client, m: Message) -> None:
    """/whtml <quiz_id> -- generate and send an interactive HTML report."""
    parts = m.text.split()
    if len(parts) < 2:
        await m.reply("Usage: `/whtml QUIZID`")
        return
    qid = parts[1].strip()

    status = await m.reply("Fetching quiz data...")
    quiz = await QuizRepository(get_db()).get(qid)
    if not quiz:
        await status.edit_text("Quiz not found.")
        return
    if quiz.get("creator_id") != m.from_user.id:
        await status.edit_text("This is not your quiz.")
        return

    try:
        html_bytes, filename = await render_quiz_html(quiz, mode="exam")
    except Exception as exc:
        logger.exception("render_quiz_html failed for qid=%s", qid)
        await status.edit_text(f"Error generating report: {exc}")
        return

    await status.edit_text("Uploading report...")
    buf = BytesIO(html_bytes)
    buf.name = filename
    try:
        await m.reply_document(
            document=buf,
            file_name=filename,
            caption=(
                f"**Quiz Report: {quiz.get('quiz_name', qid)}**\n"
                f"{len(quiz.get('questions', []))} questions"
            ),
        )
        await status.delete()
    except Exception as exc:
        logger.exception("Failed to send whtml report for qid=%s", qid)
        await status.edit_text(f"Failed to send file: {exc}")


async def wpdf_cmd(c: Client, m: Message) -> None:
    """/wpdf <quiz_id> -- generate and send a PDF report directly from Quiz ID."""
    parts = m.text.split()
    if len(parts) < 2:
        await m.reply("Usage: `/wpdf QUIZID`")
        return
    qid = parts[1].strip()

    status = await m.reply("⏳ Fetching quiz data...")

    quiz = await QuizRepository(get_db()).get(qid)
    if not quiz:
        await status.edit_text("Quiz not found.")
        return
    if quiz.get("creator_id") != m.from_user.id:
        await status.edit_text("This is not your quiz.")
        return

    questions = quiz.get("questions", [])
    if not questions:
        await status.edit_text("Quiz has no questions.")
        return

    await status.edit_text("⏳ Generating PDF... (10-30 seconds lagenge)")

    try:
        fake_polls = {f"poll_{i}": {"question_index": i} for i in range(len(questions))}
        pdf_path = os.path.join(tempfile.gettempdir(), f"quiz_pdf_{qid}_{int(time.time())}.pdf")

        quiz_name = quiz.get("quiz_name", "Quiz Report")
        safe_name = "".join(c for c in quiz_name if c.isalnum() or c in (' ', '-', '_')).strip() or qid
        file_name = f"{safe_name}.pdf"

        loop = asyncio.get_running_loop()

        # AI OFF for normal commands
        pdf_ok, _ai_solutions = await loop.run_in_executor(
            None,
            render_quiz_pdf,
            quiz_name, "Quiz Creator", questions, [], fake_polls,
            quiz.get("negative_marks", 0), quiz.get("correct_marks", 1),
            pdf_path, quiz.get("sections"), None, False, "classic", False,
        )

        if pdf_ok and os.path.exists(pdf_path):
            await status.edit_text("📤 Uploading PDF...")
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            buf = BytesIO(pdf_bytes)
            buf.name = file_name
            await m.reply_document(
                document=buf,
                file_name=file_name,
                caption=f"📄 **PDF Report: {quiz_name}**\n{len(questions)} questions",
            )
            os.remove(pdf_path)
            await status.delete()
        else:
            await status.edit_text("❌ Failed to generate PDF. Check logs.")
    except Exception as exc:
        logger.exception("wpdf_cmd failed for qid=%s", qid)
        await status.edit_text(f"❌ Error: {exc}")


async def wset_cmd(c: Client, m: Message) -> None:
    """/wset <quiz_id> -- generate and send both PDF and HTML reports."""
    parts = m.text.split()
    if len(parts) < 2:
        await m.reply("Usage: `/wset QUIZID`")
        return
    qid = parts[1].strip()

    status = await m.reply("Fetching quiz data...")
    quiz = await QuizRepository(get_db()).get(qid)
    if not quiz:
        await status.edit_text("Quiz not found.")
        return
    if quiz.get("creator_id") != m.from_user.id:
        await status.edit_text("This is not your quiz.")
        return

    questions = quiz.get("questions", [])
    if not questions:
        await status.edit_text("Quiz has no questions.")
        return

    quiz_name = quiz.get("quiz_name", "Quiz Report")
    safe_name = "".join(c for c in quiz_name if c.isalnum() or c in (' ', '-', '_')).strip() or qid

    # ---- 1. PDF ----
    await status.edit_text("⏳ **Generating PDF (1/2)...**")
    ai_solutions: dict = {}
    try:
        fake_polls = {f"poll_{i}": {"question_index": i} for i in range(len(questions))}
        pdf_path = os.path.join(tempfile.gettempdir(), f"quiz_pdf_{qid}_{int(time.time())}.pdf")
        file_name_pdf = f"{safe_name}.pdf"

        loop = asyncio.get_running_loop()
        pdf_ok, ai_solutions = await loop.run_in_executor(
            None,
            render_quiz_pdf,
            quiz_name, "Quiz Creator", questions, [], fake_polls,
            quiz.get("negative_marks", 0), quiz.get("correct_marks", 1),
            pdf_path, quiz.get("sections"), None, False, "classic", False,
        )

        if pdf_ok and os.path.exists(pdf_path):
            await status.edit_text("📤 **Uploading PDF...**")
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            buf = BytesIO(pdf_bytes)
            buf.name = file_name_pdf
            await m.reply_document(
                document=buf,
                file_name=file_name_pdf,
                caption=f"📄 **PDF Report: {quiz_name}**\n{len(questions)} questions",
            )
            os.remove(pdf_path)
        else:
            await m.reply("❌ Failed to generate PDF. Check logs.")
    except Exception as exc:
        logger.exception("wset PDF failed for qid=%s", qid)
        await m.reply(f"❌ PDF Error: {exc}")

    # ---- 2. HTML ----
    await status.edit_text("⏳ **Generating HTML (2/2)...**")
    try:
        html_bytes, file_name_html = await render_quiz_html(
            quiz, mode="exam", ai_solutions=ai_solutions
        )
        file_name_html = f"{safe_name}.html"

        await status.edit_text("📤 **Uploading HTML...**")
        buf = BytesIO(html_bytes)
        buf.name = file_name_html
        await m.reply_document(
            document=buf,
            file_name=file_name_html,
            caption=f"🌐 **HTML Report: {quiz_name}**\n{len(questions)} questions",
        )
    except Exception as exc:
        logger.exception("wset HTML failed for qid=%s", qid)
        await m.reply(f"❌ HTML Error: {exc}")

    await status.delete()


# ──────────────────────────────────────────────────────────────────────
# /testseries  (existing — unchanged)
# ──────────────────────────────────────────────────────────────────────

def _parse_testseries_args(raw: str) -> tuple[str, str, list[str]]:
    tokens = raw.split()
    mode = "keyonly"
    title_override = None
    quiz_ids: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low.startswith("mode="):
            val = low.split("=", 1)[1]
            if val in ("inline", "keyonly"):
                mode = val
        elif low.startswith("title="):
            title_override = tok.split("=", 1)[1].replace("_", " ")
        else:
            quiz_ids.append(tok.strip())
    return mode, title_override or "Mock Test", quiz_ids


async def _generate_pdf_via_api(quizzes: list[dict], mode: str, title: str, poll_timeout: int = 180) -> bytes:
    questions_payload = []
    for quiz in quizzes:
        for q in quiz.get("questions", []):
            options = [str(o) for o in q.get("options", []) if o]
            if not options:
                continue
            questions_payload.append(
                {
                    "question": str(q.get("question", "")).strip(),
                    "options": options,
                    "correct_option_id": q.get("correct_option_id", q.get("correct_option", 0)),
                    "explanation": str(q.get("explanation") or "").strip(),
                }
            )
    if not questions_payload:
        raise RuntimeError("No usable questions (with options) found in the given quiz(es).")

    payload = {
        "questions_json": questions_payload,
        "institute_name": "Quiz Creator",
        "tagline": "Test Series",
        "exam_title": title,
        "solution_display": "inline" if mode == "inline" else "end",
        "quiz_names": [str(q.get("quiz_name") or q.get("qid") or "") for q in quizzes],
        "async": True,
    }

    base = config.PDF_API_BASE.rstrip("/")
    status, job = await request_json("POST", f"{base}/api/generate", json_body=payload)
    if status != 200 or not isinstance(job, dict):
        raise RuntimeError(f"PDF API rejected the request (HTTP {status}): {job}")
    if job.get("error"):
        raise RuntimeError(f"PDF API error: {job['error']}")

    progress_url = base + job["progress_url"]
    download_url = base + job["download_url"]

    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        _pstatus, pjob = await request_json("GET", progress_url)
        if not isinstance(pjob, dict):
            raise RuntimeError("PDF API returned an unexpected progress response.")
        job_status = pjob.get("status")
        if job_status == "error":
            raise RuntimeError(f"PDF generation failed: {pjob.get('error', 'unknown error')}")
        if job_status == "done":
            break
        await asyncio.sleep(1.2)
    else:
        raise RuntimeError("PDF generation timed out -- try fewer quizzes.")

    session = await get_session()
    async with session.get(download_url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"PDF download failed (HTTP {resp.status}).")
        return await resp.read()


@ratelimit("default")
async def testseries_cmd(c: Client, m: Message) -> None:
    uid = m.from_user.id

    if not config.PDF_API_BASE:
        await m.reply("PDF generation is not configured on this bot (no PDF_API_BASE set). Use /whtml or /wpdf instead.")
        return

    if _TSR_LOCK.locked():
        await m.reply("**System busy** -- Please try again shortly.")
        return

    if not await is_premium_user(uid):
        await m.reply("**Premium feature** -- Use /pay.")
        return

    raw = m.text.split(maxsplit=1)[1].strip() if len(m.text.split(maxsplit=1)) > 1 else ""
    if not raw:
        await m.reply("Usage: `/testseries QID1 [mode=inline|keyonly] [title=Your_Title]`")
        return

    mode, title, quiz_ids = _parse_testseries_args(raw)
    if not quiz_ids:
        await m.reply("No quiz IDs found.")
        return

    async with _TSR_LOCK:
        status = await m.reply(f"Fetching {len(quiz_ids)} quiz(es)...")
        quiz_repo = QuizRepository(get_db())

        quizzes = []
        for qid in quiz_ids:
            quiz = await quiz_repo.get(qid)
            if not quiz or not quiz.get("questions"):
                continue
            if quiz.get("creator_id") != uid:
                continue
            quizzes.append(quiz)

        if not quizzes:
            await status.edit_text("No valid quizzes found.")
            return

        total_q = sum(len(q.get("questions", [])) for q in quizzes)
        await status.edit_text(f"Generating PDF...\n\nQuizzes: **{len(quizzes)}**\nQuestions: **{total_q}**")

        try:
            pdf_bytes = await _generate_pdf_via_api(quizzes, mode, title)
        except Exception as exc:
            await status.edit_text(f"PDF generation failed: {exc}")
            return

        await status.edit_text("Uploading PDF...")
        filename = f"MockTest_{'_'.join(quiz_ids[:3])}.pdf"
        buf = BytesIO(pdf_bytes)
        buf.name = filename
        try:
            await m.reply_document(document=buf, file_name=filename, caption=f"**{title}**")
            await status.delete()
        except Exception as exc:
            await status.edit_text(f"Failed to send file: {exc}")


# ──────────────────────────────────────────────────────────────────────
# /txt2pdf  /txt2html  /txt2set  /aiset — quiz format (with graceful fallback)
# ──────────────────────────────────────────────────────────────────────

async def process_txt_file(c: Client, m: Message, doc, action: str):
    uid = m.from_user.id
    status = await m.reply("⏳ **Downloading and parsing file...**")

    try:
        file_bytes = await c.download_media(doc.file_id, in_memory=True)
        file_bytes.seek(0)
        content = file_bytes.read()

        user = await UserRepository(get_db()).get_or_create(uid)
        remove_words = user.get("remove_words", [])
        questions: list = []

        # Try to parse as quiz; if it fails, gracefully fall back to notes
        try:
            _count, error = process_uploaded_file(content, _doc_fname(doc), questions, remove_words)
        except Exception as e:
            logger.warning("process_uploaded_file raised: %s", e)
            error = str(e)

        if error or not questions:
            logger.info(
                "Quiz parse produced 0 questions (%s) — falling back to notes mode",
                error or "empty",
            )
            await status.edit_text(
                "ℹ️ **Valid quiz format nahi mila** — notes mode me convert kar raha hoon..."
            )
            await process_notes_file(c, m, doc, _status_already_sent=True)
            return

        base_name = _safe_stem(_doc_fname(doc), fallback=f"quiz_{uid}")
        quiz_name = base_name.replace("_", " ").title()

        use_ai = (action == "aiset")
        ai_solutions: dict = {}

        # ---- PDF ----
        if action in ("pdf", "set", "aiset"):
            progress_cb = None

            if use_ai:
                loop = asyncio.get_running_loop()
                _throttle = {"last": 0.0}
                MIN_INTERVAL = 3.0  # seconds — Telegram edit-rate safe

                def _build_progress_text(done, total, ok, qn, success):
                    bar_len = 12
                    filled = int(bar_len * done / max(total, 1))
                    bar = "█" * filled + "░" * (bar_len - filled)
                    pct = int(100 * done / max(total, 1))
                    icon = "✅" if success else "❌"
                    return (
                        f"⏳ **Generating PDF with AI solutions...**\n\n"
                        f"🧠 **Progress:** {done}/{total} ({pct}%)\n"
                        f"`{bar}`\n\n"
                        f"✅ Success: **{ok}** | ❌ Failed: **{done - ok}**\n"
                        f"📝 Last: Q{qn} {icon}"
                    )

                def _progress_cb(done, total, ok, qn, success):
                    now = time.time()
                    is_first = done == 1
                    is_last = done == total
                    # Throttle: skip edits that fire too quickly (except first & last)
                    if not (is_first or is_last or (now - _throttle["last"] >= MIN_INTERVAL)):
                        return
                    _throttle["last"] = now
                    text = _build_progress_text(done, total, ok, qn, success)

                    async def _do():
                        try:
                            await status.edit_text(text)
                        except Exception as e:
                            err = str(e).lower()
                            if "not modified" in err:
                                return
                            logger.debug("progress edit skipped: %s", e)

                    try:
                        asyncio.run_coroutine_threadsafe(_do(), loop)
                    except Exception as e:
                        logger.debug("run_coroutine_threadsafe failed: %s", e)

                progress_cb = _progress_cb

                await status.edit_text(
                    f"⏳ **Generating PDF with AI solutions for {len(questions)} questions...**\n"
                    f"_(Har question pe AI call hota hai, isliye kuch minutes lag sakte hain)_\n\n"
                    f"🧠 **Progress:** 0/{len(questions)}"
                )
            else:
                await status.edit_text(
                    f"⏳ **Generating PDF for {len(questions)} questions...**\n_(10-30 seconds lagenge)_"
                )

            fake_polls = {f"poll_{i}": {"question_index": i} for i in range(len(questions))}
            pdf_path = os.path.join(tempfile.gettempdir(), f"txt2pdf_{uid}_{int(time.time())}.pdf")
            file_name_pdf = f"{base_name}.pdf"

            loop = asyncio.get_running_loop()
            pdf_ok, ai_solutions = await loop.run_in_executor(
                None,
                partial(
                    render_quiz_pdf,
                    quiz_name, "Text Import", questions, [], fake_polls,
                    0, 1, pdf_path, None, None, False, "classic", use_ai,
                    progress_callback=progress_cb,
                ),
            )

            if use_ai and pdf_ok:
                try:
                    await status.edit_text(
                        f"✅ **AI Solutions Generated!**\n"
                        f"🧠 **{len(ai_solutions)}** solutions ready\n\n"
                        f"📄 Building PDF file..."
                    )
                except Exception:
                    pass

            if pdf_ok and os.path.exists(pdf_path):
                await status.edit_text("📤 **Uploading PDF...**")
                with open(pdf_path, "rb") as f:
                    pdf_bytes = f.read()
                buf = BytesIO(pdf_bytes)
                buf.name = file_name_pdf
                await m.reply_document(
                    document=buf,
                    file_name=file_name_pdf,
                    caption=(
                        f"📄 **PDF Generated!**\n\n"
                        f"❓ **Questions:** {len(questions)}\n"
                        f"📁 **File:** `{_doc_fname(doc) or 'text'}`"
                    ),
                )
                os.remove(pdf_path)
            else:
                await m.reply("❌ Failed to generate PDF. Please check logs.")

        # ---- HTML ----
        if action in ("html", "set", "aiset"):
            if action in ("set", "aiset"):
                await m.reply("⏳ **Ab HTML generate kar rahe hain...**")

            await status.edit_text(f"⏳ **Generating HTML for {len(questions)} questions...**")
            file_name_html = f"{base_name}.html"
            quiz_data = {
                "qid": f"TXT{int(time.time())}",
                "quiz_name": quiz_name,
                "questions": questions,
                "negative_marks": 0,
                "correct_marks": 1,
                "sections": None,
                "creator_id": uid,
                "quiz_type": "free",
            }
            html_bytes, _ = await render_quiz_html(
                quiz_data, mode="exam", ai_solutions=ai_solutions if use_ai else None
            )
            await status.edit_text("📤 **Uploading HTML...**")
            buf = BytesIO(html_bytes)
            buf.name = file_name_html
            await m.reply_document(
                document=buf,
                file_name=file_name_html,
                caption=(
                    f"🌐 **HTML Generated!**\n\n"
                    f"❓ **Questions:** {len(questions)}\n"
                    f"📁 **File:** `{_doc_fname(doc) or 'text'}`"
                ),
            )

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error("process_txt_file error: %s", e, exc_info=True)
        try:
            await status.edit_text(f"❌ **Error:** {str(e)[:100]}")
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
# NEW: /txt2notes  — any txt → clean notes PDF + HTML
# ──────────────────────────────────────────────────────────────────────

async def process_notes_file(c: Client, m: Message, doc, _status_already_sent: bool = False) -> None:
    """Any plain text file → notes-style PDF + notes-style HTML (no quiz)."""
    uid = m.from_user.id
    status = await m.reply("⏳ **Preparing notes...**")
    try:
        file_bytes = await c.download_media(doc.file_id, in_memory=True)
        file_bytes.seek(0)
        raw = file_bytes.read()

        text = await _decode_bytes(raw)
        if text is None or not text.strip():
            await status.edit_text(
                "❌ File khaali hai ya decode nahi ho saki (encoding issue)."
            )
            return

        fname = _doc_fname(doc)
        base_name = _safe_stem(fname, fallback=f"notes_{uid}")
        clean_title = base_name.replace("_", " ").replace("-", " ").strip().title() or "Notes"

        # ---- PDF ----
        await status.edit_text(f"⏳ **Generating notes PDF...**\n_(File: {fname or 'text'})_")
        pdf_path = os.path.join(tempfile.gettempdir(), f"notes_{uid}_{int(time.time())}.pdf")
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(
            None, render_notes_pdf, text, clean_title, pdf_path, "classic"
        )
        if ok and os.path.exists(pdf_path):
            await status.edit_text("📤 **Uploading notes PDF...**")
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            buf = BytesIO(pdf_bytes)
            buf.name = f"{base_name}.pdf"
            await m.reply_document(
                document=buf,
                file_name=f"{base_name}.pdf",
                caption=(
                    f"📄 **Notes PDF**\n\n"
                    f"📝 {clean_title}\n"
                    f"📁 `{fname or 'text file'}`"
                ),
            )
            os.remove(pdf_path)
        else:
            await m.reply("❌ PDF generation failed. Check logs.")

        # ---- HTML ----
        await status.edit_text("⏳ **Generating notes HTML...**")
        html_bytes, _fname_html = render_notes_html(text, clean_title)
        buf2 = BytesIO(html_bytes)
        buf2.name = f"{base_name}.html"
        await m.reply_document(
            document=buf2,
            file_name=f"{base_name}.html",
            caption=(
                f"🌐 **Notes HTML (single-file, offline-ready)**\n\n"
                f"📝 {clean_title}\n"
                f"💡 *Search box + TOC + dark mode + print-friendly*"
            ),
        )

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error("process_notes_file error: %s", e, exc_info=True)
        try:
            await status.edit_text(f"❌ **Error:** {str(e)[:150]}")
        except Exception:
            pass


async def txt2notes_cmd(c: Client, m: Message) -> None:
    """/txt2notes — reply to any text file (or send after the command) → notes PDF + HTML."""
    uid = m.from_user.id
    if not await is_premium_user(uid):
        await m.reply("🔒 **Premium required** to use this feature: /pay")
        return

    doc = None
    if m.reply_to_message and m.reply_to_message.document:
        doc = m.reply_to_message.document
    elif m.document:
        doc = m.document

    if doc:
        fname = _doc_fname(doc)
        exts = (".txt", ".md", ".json", ".csv", ".log", ".text")
        if fname and not _allowed_ext(fname, exts):
            await m.reply(
                "⚠️ Sirf text files bhejein: `.txt`, `.md`, `.json`, `.csv`, `.log`"
            )
            return
        await process_notes_file(c, m, doc)
    else:
        pending_txt_actions[uid] = "notes"
        await m.reply(
            "📁 **Please send the file now.**\n\n"
            "_(Koi bhi text file — structured notes, class notes, raw paragraphs — sab chalega)_"
        )


# ──────────────────────────────────────────────────────────────────────
# /txt2pdf  /txt2html  /txt2set  /aiset — command entry points
# ──────────────────────────────────────────────────────────────────────

async def _txt_cmd_common(c: Client, m: Message, action: str, pending_msg: str) -> None:
    uid = m.from_user.id
    if not await is_premium_user(uid):
        await m.reply("🔒 **Premium required** to use this feature: /pay")
        return

    doc = None
    if m.reply_to_message and m.reply_to_message.document:
        doc = m.reply_to_message.document
    elif m.document:
        doc = m.document

    if doc:
        fname = _doc_fname(doc)
        if fname and not _allowed_ext(fname, (".txt", ".json")):
            await m.reply("⚠️ Sirf `.txt` aur `.json` files supported hain.")
            return
        await process_txt_file(c, m, doc, action)
    else:
        pending_txt_actions[uid] = action
        await m.reply(pending_msg)


async def txt2pdf_cmd(c: Client, m: Message) -> None:
    await _txt_cmd_common(c, m, "pdf",
                          "📁 **Please send the `.txt` file now.**\n\n*(File ko normal message ki tarah bhej dein)*")


async def txt2html_cmd(c: Client, m: Message) -> None:
    await _txt_cmd_common(c, m, "html",
                          "📁 **Please send the `.txt` file now.**\n\n*(File ko normal message ki tarah bhej dein)*")


async def txt2set_cmd(c: Client, m: Message) -> None:
    await _txt_cmd_common(c, m, "set",
                          "📁 **Please send the `.txt` file now.**\n\n*(PDF aur HTML dono ek sath banenge)*")


async def aiset_cmd(c: Client, m: Message) -> None:
    await _txt_cmd_common(c, m, "aiset",
                          "📁 **Please send the `.txt` file now.**\n\n*(PDF aur HTML dono AI Solution ke sath banenge)*")


# ──────────────────────────────────────────────────────────────────────
# Pending file handler — with bug fixes
# ──────────────────────────────────────────────────────────────────────

def pending_txt_filter():
    async def func(_, __, m: Message) -> bool:
        return bool(m.from_user) and m.from_user.id in pending_txt_actions

    return filters.create(func)


async def handle_pending_txt_file(c: Client, m: Message) -> None:
    uid = m.from_user.id
    action = pending_txt_actions.get(uid)
    if not action:
        return
    if not m.document:
        return

    doc = m.document
    fname = _doc_fname(doc)

    if action == "notes":
        allowed = (".txt", ".md", ".json", ".csv", ".log", ".text")
    else:
        allowed = (".txt", ".json")

    if fname and not _allowed_ext(fname, allowed):
        # FIX: always clear state so user isn't stuck forever
        pending_txt_actions.pop(uid, None)
        cmd_name = "txt2notes" if action == "notes" else f"txt2{action}"
        await m.reply(
            "⚠️ Invalid file type. Kripya command phir se chalayein.\n"
            f"Allowed for /{cmd_name}: " + ", ".join(allowed)
        )
        return

    # Clear state BEFORE processing so a failed processing doesn't leave user stuck
    pending_txt_actions.pop(uid, None)

    if action == "notes":
        await process_notes_file(c, m, doc)
    else:
        await process_txt_file(c, m, doc, action)


# ──────────────────────────────────────────────────────────────────────
# register
# ──────────────────────────────────────────────────────────────────────

def register(app: Client) -> None:
    app.on_message(filters.command("whtml") & filters.private)(whtml_cmd)
    app.on_message(filters.command(["wpdf", "pdf"]) & filters.private)(wpdf_cmd)
    app.on_message(filters.command("wset") & filters.private)(wset_cmd)
    app.on_message(filters.command(["testseries", "tsr", "mocktest"]) & filters.private)(testseries_cmd)
    app.on_message(filters.command("txt2pdf") & filters.private)(txt2pdf_cmd)
    app.on_message(filters.command("txt2html") & filters.private)(txt2html_cmd)
    app.on_message(filters.command("txt2set") & filters.private)(txt2set_cmd)
    app.on_message(filters.command("aiset") & filters.private)(aiset_cmd)
    # NEW — any text → notes
    app.on_message(filters.command(["txt2notes", "notes"]) & filters.private)(txt2notes_cmd)

    app.on_message(filters.document & filters.private & pending_txt_filter())(handle_pending_txt_file)
