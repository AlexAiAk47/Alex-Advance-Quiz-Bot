"""
Advance Quiz Bot — Open Source Project
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from io import BytesIO

from pyrogram import Client, filters
from pyrogram.types import Message

from quizbot.database import QuizRepository, UserRepository, get_db
from quizbot.shared import config
from quizbot.shared.html.quiz_report import render_quiz_html
from quizbot.shared.utils import is_premium_user
from quizbot.shared.utils.http import get_session, request_json
from quizbot.runner_bot.pdf_reports import render_quiz_pdf

from ..ratelimit import ratelimit
from .file_import import process_uploaded_file

logger = logging.getLogger(__name__)

_TSR_LOCK = asyncio.Lock()

@ratelimit("default")
async def whtml_cmd(c: Client, m: Message) -> None:
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
            caption=(f"**Quiz Report: {quiz.get('quiz_name', qid)}**\n{len(quiz.get('questions', []))} questions"),
        )
        await status.delete()
    except Exception as exc:
        logger.exception("Failed to send whtml report for qid=%s", qid)
        await status.edit_text(f"Failed to send file: {exc}")


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
    title = title_override or "Mock Test"
    return mode, title, quiz_ids


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
        raise RuntimeError("No usable questions (with options) found.")

    payload = {
        "questions_json": questions_payload, "institute_name": "Quiz Creator",
        "tagline": "Test Series", "exam_title": title,
        "solution_display": "inline" if mode == "inline" else "end",
        "quiz_names": [str(q.get("quiz_name") or q.get("qid") or "") for q in quizzes], "async": True,
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
        pstatus, pjob = await request_json("GET", progress_url)
        if not isinstance(pjob, dict):
            raise RuntimeError("PDF API returned an unexpected progress response.")
        job_status = pjob.get("status")
        if job_status == "error":
            raise RuntimeError(f"PDF generation failed: {pjob.get('error', 'unknown error')}")
        if job_status == "done":
            break
        await asyncio.sleep(1.2)
    else:
        raise RuntimeError("PDF generation timed out.")

    session = await get_session()
    async with session.get(download_url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"PDF download failed (HTTP {resp.status}).")
        return await resp.read()


@ratelimit("default")
async def testseries_cmd(c: Client, m: Message) -> None:
    uid = m.from_user.id

    if not config.PDF_API_BASE:
        await m.reply("PDF generation is not configured on this bot (no PDF_API_BASE set). Use /whtml instead.")
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

        quizzes, failed, not_owner = [], [], []
        for qid in quiz_ids:
            quiz = await quiz_repo.get(qid)
            if not quiz or not quiz.get("questions"):
                failed.append(qid)
                continue
            if quiz.get("creator_id") != uid:
                not_owner.append(qid)
                continue
            quizzes.append(quiz)

        if (failed or not_owner) and quizzes:
            notes = []
            if failed: notes.append(f"Not found/empty: `{', '.join(failed)}`")
            if not_owner: notes.append(f"Not created by you: `{', '.join(not_owner)}`")
            await status.edit_text("Some quizzes were skipped:\n" + "\n".join(notes) + f"\n\nProceeding with {len(quizzes)} valid quiz(es)...")
            await asyncio.sleep(2)

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


@ratelimit("default")
async def txt2pdf_cmd(c: Client, m: Message) -> None:
    """/txt2pdf -- Reply to a .txt file to directly generate a PDF."""
    
    # 🔍 DEBUG LINE: Agar ye message nahi aaya, matlab code update nahi hua!
    await m.reply("🔍 Checking file...") 
    
    uid = m.from_user.id
    
    if not await is_premium_user(uid):
        await m.reply("🔒 **Premium required** to use this feature: /pay")
        return
        
    doc = None
    if m.reply_to_message and m.reply_to_message.document:
        doc = m.reply_to_message.document
    elif m.document:
        doc = m.document
        
    if not doc:
        await m.reply(
            "⚠️ **Usage:**\n"
            "Kisi `.txt` file par reply karke `/txt2pdf` bhejo."
        )
        return
        
    if not doc.file_name.lower().endswith(('.txt', '.json')):
        await m.reply("⚠️ Sirf `.txt` aur `.json` files supported hain.")
        return

    status = await m.reply("⏳ **Downloading and parsing file...**")
    
    try:
        file_bytes = await c.download_media(doc.file_id, in_memory=True)
        file_bytes.seek(0)
        content = file_bytes.read()
        
        user = await UserRepository(get_db()).get_or_create(uid)
        remove_words = user.get("remove_words", [])
        questions = []
        
        count, error = process_uploaded_file(content, doc.file_name, questions, remove_words)
        
        if error or not questions:
            await status.edit_text(f"❌ **Error parsing file:** {error or 'No valid questions found. Format check karo.'}")
            return
            
        await status.edit_text(f"⏳ **Generating PDF for {len(questions)} questions...**\n_(10-30 seconds lagenge)_")
        
        fake_polls = {f"poll_{i}": {"question_index": i} for i in range(len(questions))}
        pdf_path = os.path.join(tempfile.gettempdir(), f"txt2pdf_{uid}_{int(time.time())}.pdf")
        quiz_name = doc.file_name.replace('.txt', '').replace('.json', '').replace('_', ' ').title()
        
        loop = asyncio.get_running_loop()
        pdf_ok = await loop.run_in_executor(
            None, 
            render_quiz_pdf, 
            quiz_name, "Text Import", questions, [], fake_polls, 
            0, 1, pdf_path, None, None, False, "classic", False
        )
        
        if pdf_ok and os.path.exists(pdf_path):
            await status.edit_text("📤 **Uploading PDF...**")
            await m.reply_document(
                document=pdf_path,
                caption=f"📄 **PDF Generated!**\n\n❓ **Questions:** {len(questions)}\n📁 **File:** `{doc.file_name}`"
            )
            os.remove(pdf_path)
            await status.delete()
        else:
            await status.edit_text("❌ Failed to generate PDF. Check Railway logs.")
            
    except Exception as e:
        logger.error("txt2pdf_cmd error: %s", e, exc_info=True)
        await status.edit_text(f"❌ **Error:** {str(e)[:100]}")


def register(app: Client) -> None:
    app.on_message(filters.command("whtml") & filters.private)(whtml_cmd)
    app.on_message(filters.command(["testseries", "tsr", "mocktest"]) & filters.private)(testseries_cmd)
    app.on_message(filters.command("txt2pdf") & filters.private)(txt2pdf_cmd)
