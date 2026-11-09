"""
Advance Quiz Bot — Open Source Project
Original: Gagan (github.com/devgaganin) | Ref: https://t.me/advance_quiz_bot
Reviewed & verified with Claude AI assistance.

⚠️ MODIFIED: AI Solution generation is DISABLED for PDF reports.
PDF reports now contain ONLY: Question + Options + Correct Answer + manual Explanation + Reference.
No AI calls are made when generating PDF reports.
AI is still used ONLY by /aiset (AI quiz generation) and /pdfquiz (PDF→quiz).
"""

from __future__ import annotations

import html as _html_mod
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

PdfStyle = Literal["classic", "modern"]

CREATOR_LINE = "Created by: @Alex_Aditya(साधना).tg"

_SECTION_COLORS = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
    "#DDA0DD", "#98D8C8", "#F7DC6F", "#BB8FCE", "#85C1E9",
]


# ══════════════════════════════════════════════════════════════════════
# DEPRECATED — AI SOLUTION HELPERS (kept only for reuse by /aiset)
# ══════════════════════════════════════════════════════════════════════

def _strip_answer_letter_from_text(text: str, correct_letters: str) -> str:
    if not text:
        return text
    lines = text.split("\n")
    cleaned = []
    for ln in lines:
        stripped = ln.strip().lower()
        if stripped.startswith("✔") and "correct answer" in stripped:
            continue
        if stripped.startswith("correct answer"):
            continue
        if stripped.startswith("✅") and "correct answer" in stripped:
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip()


def _build_prompt(question: str, options: list[str], correct_letters: str, language_hint: str) -> tuple[str, str]:
    opts_lines = "\n".join(f"   {chr(65 + i)}) {opt}" for i, opt in enumerate(options))
    correct_letter = correct_letters.split(",")[0].strip().upper()
    correct_idx = ord(correct_letter[0]) - 65 if correct_letter else 0
    correct_text = options[correct_idx] if 0 <= correct_idx < len(options) else ""

    if language_hint == "Hindi":
        prompt = (
            f"तुम एक experienced Indian competitive exam teacher हो. "
            f"नीचे दिए question का solution लिखो.\n\n"
            f"Question: {question}\n\nOptions:\n{opts_lines}\n\n"
            f"सही उत्तर: {correct_letter}) {correct_text}\n\nSolution:"
        )
        system_msg = "You are a friendly Indian competitive-exam teacher."
    else:
        prompt = (
            f"You are an experienced Indian competitive exam teacher. "
            f"Write the solution for this question.\n\nQuestion: {question}\n\n"
            f"Options:\n{opts_lines}\n\nCorrect Answer: {correct_letter}) {correct_text}\n\nSolution:"
        )
        system_msg = "You are a friendly Indian competitive-exam teacher."
    return system_msg, prompt


def _clean_ai_text(text: str, correct_letters: str) -> str:
    if not text:
        return ""
    text = re.sub(r"</?b>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?strong>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = _strip_answer_letter_from_text(text, correct_letters)
    return text.strip()


def _format_ai_solution_html(ai_text: str) -> str:
    if not ai_text or not ai_text.strip():
        return ""
    text = re.sub(r"<[^>]+>", "", ai_text)
    text = _escape_html(text)
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return ""
    return f'<div class="ai-paragraph">{" ".join(paragraphs)}</div>'


# ══════════════════════════════════════════════════════════════════════
# HTML / MATH HELPERS
# ══════════════════════════════════════════════════════════════════════

def _render_math_in_html(text: str) -> str:
    try:
        import latex2mathml.converter as _lc
    except ImportError:
        return text

    def _display(m: re.Match) -> str:
        try:
            ml = _lc.convert(m.group(1).strip())
            return ml.replace("<math>", '<math display="block">', 1)
        except Exception:
            return m.group(0)

    def _inline(m: re.Match) -> str:
        try:
            return _lc.convert(m.group(1).strip())
        except Exception:
            return m.group(0)

    text = re.sub(r"\$\$([\s\S]+?)\$\$", _display, text)
    text = re.sub(r"(?<!\$)\$(?!\$)([^$\n]+?)(?<!\$)\$(?!\$)", _inline, text)
    return text


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def _safe_html(text: str, render_math: bool = True) -> str:
    if render_math and ("$$" in text or "$" in text):
        rendered = _render_math_in_html(text)
        parts = re.split(r"(<math[\s\S]*?</math>)", rendered)
        escaped = []
        for part in parts:
            if part.startswith("<math"):
                escaped.append(part)
            else:
                escaped.append(part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>"))
        return "".join(escaped)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")


def _build_section_map(sections: Optional[list[dict]]) -> dict[int, str]:
    section_map: dict[int, str] = {}
    if not sections:
        return section_map
    for sec in sections:
        sec_name = sec.get("name", "")
        q_indices = sec.get("question_indices", [])
        if not q_indices and "start" in sec and "end" in sec:
            q_indices = list(range(sec["start"], sec["end"] + 1))
        for qi in q_indices:
            section_map[int(qi)] = sec_name
    return section_map


def _strip_leading_number(text: str) -> str:
    if not text:
        return text
    return re.sub(r"^\s*(?:Q(?:uestion)?\s*[\.\-:]?\s*|प्र(?:श्न)?\s*[\.\-:]?\s*)?\d+\s*[\.\)\-:]\s*", "", text, count=1, flags=re.IGNORECASE)


def _detect_language(text: str) -> str:
    if not text:
        return "English"
    devanagari = sum(1 for ch in text if "\u0900" <= ch <= "\u097F")
    total_letters = sum(1 for ch in text if ch.isalpha())
    if total_letters == 0:
        return "English"
    return "Hindi" if (devanagari / max(total_letters, 1)) > 0.3 else "English"


# ══════════════════════════════════════════════════════════════════════
# PRACTICE SHEET BUILDER — AI DISABLED
# ══════════════════════════════════════════════════════════════════════

def _build_practice_sheet_html(
    questions: list[dict],
    session_polls: dict,
    sections: Optional[list[dict]],
    shuffle_options: bool,
    style: PdfStyle,
    use_ai: bool = False,
    progress_callback=None,
) -> tuple[str, list[tuple[int, str]], list[dict], dict[int, str]]:
    # HARD KILL SWITCH — AI solution always OFF in PDF reports
    use_ai = False

    import random
    section_map = _build_section_map(sections)
    questions_html = ""
    answer_key: list[tuple[int, str]] = []
    solutions: list[dict] = []
    ai_solutions_raw: dict[int, str] = {}
    polls_list = sorted(session_polls.items(), key=lambda x: x[1].get("question_index", 0))
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    last_section = None
    display_num = 0

    logger.info("📊 PDF Report → %d questions, AI solutions DISABLED", len(polls_list))

    for i, (_poll_id, pdata) in enumerate(polls_list):
        q_idx = pdata.get("question_index", i)
        if q_idx < 0 or q_idx >= len(questions):
            logger.warning("⚠️  Skipping poll %s — invalid question_index=%s", _poll_id, q_idx)
            continue
        q = questions[q_idx]
        display_num += 1

        sec_name = section_map.get(q_idx, "")
        if sec_name and sec_name != last_section:
            last_section = sec_name
            safe_sec = sec_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            questions_html += f'<div class="section-banner">\U0001F4C2 {safe_sec}</div>'

        orig_options = list(q.get("options") or [])
        orig_options = [str(o) for o in orig_options if str(o).strip()]
        has_options = len(orig_options) > 0

        orig_correct_id = q.get("correct_option_id", 0)
        raw_correct_ids = orig_correct_id if isinstance(orig_correct_id, list) else [orig_correct_id]
        try:
            raw_correct_ids = [int(c) for c in raw_correct_ids]
        except Exception:
            raw_correct_ids = [0]

        if has_options:
            if shuffle_options:
                idx_list = list(range(len(orig_options)))
                random.shuffle(idx_list)
                shuffled_options = [orig_options[k] for k in idx_list]
                old_to_new = {old: new for new, old in enumerate(idx_list)}
                correct_ids_final = [old_to_new[c] for c in raw_correct_ids if c in old_to_new]
            else:
                shuffled_options = orig_options
                correct_ids_final = [c for c in raw_correct_ids if 0 <= c < len(orig_options)]
        else:
            shuffled_options = []
            correct_ids_final = []
        has_answer = len(correct_ids_final) > 0

        raw_q_text = str(q.get("question", "")).strip()
        clean_q_text = _strip_leading_number(raw_q_text)

        if shuffled_options:
            lengths = [len(f"({letters[j].lower()}) {str(o)}") for j, o in enumerate(shuffled_options)]
            max_opt_len = max(lengths, default=0)
        else:
            max_opt_len = 0

        if max_opt_len <= 12:
            grid_cls = "opt-grid-4"
        elif max_opt_len <= 35:
            grid_cls = "opt-grid-2"
        else:
            grid_cls = "opt-grid-1"

        opts_html = "".join(
            f'<div class="opt-item">({letters[j].lower()}) {_safe_html(str(opt))}</div>'
            for j, opt in enumerate(shuffled_options)
        )

        if has_answer:
            correct_letters = ",".join(letters[c].lower() for c in correct_ids_final if 0 <= c < len(letters))
            if correct_letters:
                answer_key.append((display_num, correct_letters))
            else:
                correct_letters = ""
                has_answer = False
        else:
            correct_letters = ""

        options_for_solution = [
            {"letter": letters[j], "text": _safe_html(str(opt)), "is_correct": j in correct_ids_final}
            for j, opt in enumerate(shuffled_options)
        ]

        manual_expl = str(q.get("explanation", "")).strip()
        if manual_expl.lower() in ("none", "null", "n/a", "-", ""):
            manual_expl = ""

        # NO AI CALL — ai_text always empty
        ai_text = ""

        solutions.append({
            "num": display_num,
            "question": _safe_html(clean_q_text),
            "options": options_for_solution,
            "correct_letters": correct_letters,
            "has_options": has_options,
            "has_answer": has_answer,
            "explanation": _safe_html(manual_expl) if manual_expl else "",
            "reference": _safe_html(str(q.get("reply_text", "")).strip()) if q.get("reply_text") else "",
            "ai_solution": "",
        })

        safe_q = _safe_html(clean_q_text)
        question_line = f'<div class="qs-question"><span class="qs-num">{display_num}.</span> {safe_q}</div>'

        if has_options:
            questions_html += f'<div class="qs-item">{question_line}<div class="qs-options {grid_cls}">{opts_html}</div></div>'
        else:
            questions_html += f'<div class="qs-item qs-item-note">{question_line}</div>'

    return questions_html, answer_key, solutions, ai_solutions_raw


# ══════════════════════════════════════════════════════════════════════
# ANSWER KEY + SOLUTIONS HTML
# ══════════════════════════════════════════════════════════════════════

def _build_answer_key_html(answer_key: list[tuple[int, str]]) -> str:
    if not answer_key:
        return ""
    cells = "".join(f'<span class="ak-cell"><b>{num}.</b> {ans}</span>' for num, ans in answer_key)
    return (
        f'<div class="answer-key-page">'
        f'<div class="answer-key-title">\U0001F511 Answer Key</div>'
        f'<div class="answer-key-grid">{cells}</div>'
        f'<div class="answer-key-note">Ye answers aapke practice ke liye hain.</div>'
        f'</div>'
    )


def _build_solutions_html(solutions: list[dict]) -> str:
    if not solutions:
        return ""
    blocks = []
    for sol in solutions:
        opts_block = ""
        if sol.get("has_options"):
            opts_lines = []
            for opt in sol["options"]:
                if opt["is_correct"]:
                    opts_lines.append(
                        f'<div class="sol-opt sol-opt-correct">'
                        f'<span class="sol-opt-letter">{opt["letter"]})</span> {opt["text"]} '
                        f'<span class="sol-correct-tag">\u2714 Correct</span></div>'
                    )
                else:
                    opts_lines.append(
                        f'<div class="sol-opt"><span class="sol-opt-letter">{opt["letter"]})</span> {opt["text"]}</div>'
                    )
            opts_block = "".join(opts_lines)

        answer_banner = ""
        if sol.get("has_answer") and sol.get("correct_letters"):
            answer_banner = (
                f'<div class="sol-answer"><b>\u2705 Correct Answer:</b> '
                f'<span class="sol-answer-letter">{sol["correct_letters"].upper()}</span></div>'
            )

        expl_block = f'<div class="sol-explanation">{sol["explanation"]}</div>' if sol["explanation"] else ""
        ref_block = f'<div class="sol-reference"><b>\U0001F4DD Reference:</b> {sol["reference"]}</div>' if sol["reference"] else ""
        opts_wrapper = f'<div class="sol-options">{opts_block}</div>' if opts_block else ""

        blocks.append(
            f'<div class="sol-item">'
            f'<div class="sol-question"><span class="sol-num">{sol["num"]}.</span> {sol["question"]}</div>'
            f'{opts_wrapper}{answer_banner}{expl_block}{ref_block}'
            f'</div>'
        )
    return (
        f'<div class="solutions-page">'
        f'<div class="solutions-title">\U0001F4D8 Detailed Solutions</div>'
        f'<div class="solutions-container">{"".join(blocks)}</div>'
        f'</div>'
    )


# ══════════════════════════════════════════════════════════════════════
# CSS
# ══════════════════════════════════════════════════════════════════════

_CLASSIC_CSS = """
@page { size: A4; margin: 1.2cm 1.2cm 1.5cm 1.2cm; @bottom-center { content: "Page " counter(page); font-family: 'Noto Sans', sans-serif; font-size: 8pt; color: #64748b; } }
body { font-family: 'Noto Sans', 'Noto Sans Devanagari', sans-serif; color: #1e293b; line-height: 1.4; font-size: 9pt; background-color: #fff; }
.report-header { text-align: center; border-bottom: 2px solid #1e3a8a; padding-bottom: 10px; margin-bottom: 14px; }
.report-title { font-family: 'Merriweather', serif; font-size: 16pt; color: #1e3a8a; margin: 0 0 4px 0; }
.creator-line { font-size: 9pt; color: #4338ca; font-weight: 600; letter-spacing: 0.3px; margin-top: 2px; }
.creator-line b { color: #1e3a8a; }
.section-banner { column-span: all; background: linear-gradient(90deg, #1e3a8a, #2563eb); color: white; padding: 6px 12px; font-weight: bold; font-size: 10pt; margin: 12px 0 8px 0; border-radius: 4px; letter-spacing: 0.3px; page-break-after: avoid; }
.questions-container { column-count: 2; column-gap: 18px; column-rule: 1px solid #e2e8f0; }
.qs-item { margin-bottom: 9px; padding-bottom: 7px; border-bottom: 1px dotted #e2e8f0; page-break-inside: avoid; break-inside: avoid; }
.qs-question { font-weight: 600; color: #1e293b; font-size: 9pt; line-height: 1.4; margin-bottom: 3px; text-align: justify; }
.qs-num { color: #1e3a8a; font-weight: 700; margin-right: 3px; }
.qs-options { font-size: 8.5pt; color: #334155; line-height: 1.45; display: grid; column-gap: 10px; row-gap: 4px; margin-top: 5px; }
.opt-grid-4 { grid-template-columns: repeat(4, 1fr); }
.opt-grid-2 { grid-template-columns: repeat(2, 1fr); }
.opt-grid-1 { grid-template-columns: 1fr; }
.opt-item { display: block; word-break: break-word; }
.answer-key-page { page-break-before: always; margin-top: 10px; }
.answer-key-title { text-align: center; background: linear-gradient(90deg, #1e3a8a, #2563eb); color: white; padding: 8px 12px; font-weight: bold; font-size: 13pt; border-radius: 4px; margin-bottom: 14px; letter-spacing: 1px; }
.answer-key-grid { column-count: 6; column-gap: 10px; font-size: 10pt; }
.ak-cell { display: block; padding: 3px 6px; margin-bottom: 4px; background: #f1f5f9; border-left: 2px solid #1e3a8a; border-radius: 2px; break-inside: avoid; }
.ak-cell b { color: #1e3a8a; }
.answer-key-note { text-align: center; margin-top: 16px; font-size: 8.5pt; color: #64748b; font-style: italic; }
.solutions-page { page-break-before: always; margin-top: 10px; }
.solutions-title { text-align: center; background: linear-gradient(90deg, #1e3a8a, #2563eb); color: white; padding: 8px 12px; font-weight: bold; font-size: 13pt; border-radius: 4px; margin-bottom: 14px; letter-spacing: 1px; }
.solutions-container { column-count: 2; column-gap: 18px; }
.sol-item { margin-bottom: 10px; padding: 8px; border: 1px solid #cbd5e1; border-left: 3px solid #1e3a8a; border-radius: 4px; background: #fff; page-break-inside: avoid; break-inside: avoid; }
.sol-question { font-weight: 600; color: #1e293b; font-size: 9pt; line-height: 1.4; margin-bottom: 5px; }
.sol-num { color: #1e3a8a; font-weight: 700; margin-right: 3px; }
.sol-options { margin-bottom: 5px; }
.sol-opt { font-size: 8.5pt; color: #475569; padding: 2px 4px; margin-bottom: 2px; border-radius: 3px; line-height: 1.35; }
.sol-opt-letter { font-weight: 700; color: #64748b; margin-right: 4px; }
.sol-opt-correct { background: #dcfce7; color: #14532d; font-weight: 600; border-left: 3px solid #16a34a; }
.sol-opt-correct .sol-opt-letter { color: #16a34a; }
.sol-correct-tag { color: #16a34a; font-size: 7.5pt; font-weight: 700; margin-left: 4px; }
.sol-answer { background: #f0fdf4; border: 1px solid #86efac; padding: 4px 8px; border-radius: 4px; font-size: 9pt; color: #14532d; margin-bottom: 4px; }
.sol-answer-letter { color: #16a34a; font-weight: 800; font-size: 10pt; }
.sol-explanation { background: #fef9c3; border-left: 2px solid #d97706; padding: 5px 8px; font-size: 8.5pt; color: #78350f; border-radius: 0 3px 3px 0; margin-bottom: 4px; line-height: 1.4; }
.sol-reference { background: #eff6ff; border-left: 2px solid #2563eb; padding: 4px 8px; font-size: 8pt; color: #1e3a8a; border-radius: 0 3px 3px 0; margin-bottom: 3px; line-height: 1.35; }
math { font-size: 9pt; } math[display="block"] { display: block; margin: 4px auto; text-align: center; }
.footer-info { text-align: center; font-size: 7pt; color: #94a3b8; margin-top: 16px; border-top: 1px solid #e2e8f0; padding-top: 6px; }
.footer-info .creator-tag { color: #4338ca; font-weight: 600; }
"""

_MODERN_CSS = _CLASSIC_CSS


# ══════════════════════════════════════════════════════════════════════
# MAIN PDF RENDERER — AI DISABLED
# ══════════════════════════════════════════════════════════════════════

def render_quiz_pdf(
    quiz_name: str, chat_title: str, questions: list[dict], leaderboard: list[dict],
    session_polls: dict, neg: float, correct_mark: float, output_path: str,
    sections: Optional[list[dict]] = None, bg_image_b64: Optional[str] = None,
    shuffle_options: bool = False, style: PdfStyle = "classic",
    use_ai: bool = False,
    progress_callback=None,
) -> tuple[bool, dict[int, str]]:
    # HARD KILL SWITCH — AI never runs from PDF path
    use_ai = False

    try:
        from weasyprint import HTML
    except ImportError:
        return False, {}

    t_start = time.time()
    logger.info("▶️  render_quiz_pdf START — quiz=%r, %d questions, AI=DISABLED",
                quiz_name, len(session_polls))

    bg_css = ""
    if bg_image_b64:
        bg_css = f""".watermark-circle {{ position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 220px; height: 220px; background-image: url('{bg_image_b64}'); background-repeat: no-repeat; background-position: center center; background-size: cover; border-radius: 50%; opacity: 0.05; z-index: 0; pointer-events: none; }} body > *:not(.watermark-circle) {{ position: relative; z-index: 1; }}"""

    questions_html, answer_key, solutions, ai_solutions = _build_practice_sheet_html(
        questions, session_polls, sections, shuffle_options, style,
        use_ai=False, progress_callback=progress_callback,
    )
    answer_key_html = _build_answer_key_html(answer_key)
    solutions_html = _build_solutions_html(solutions)
    css = _CLASSIC_CSS
    logger.info("🖨️  HTML built — rendering to PDF with WeasyPrint…")

    watermark_div = '<div class="watermark-circle"></div>' if bg_image_b64 else ""
    creator_line_html = f'<div class="creator-line">✍️ <b>{_escape_html(CREATOR_LINE)}</b></div>'

    html_content = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;600;700&family=Noto+Sans+Devanagari:wght@400;600;700&family=Merriweather:wght@700&display=swap" rel="stylesheet">
<style>{css}{bg_css}</style></head>
<body>
{watermark_div}
<div class="report-header"><h1 class="report-title">{_escape_html(quiz_name or "Quiz Practice Sheet")}</h1>{creator_line_html}</div>
<div class="questions-container">{questions_html}</div>
{answer_key_html}
{solutions_html}
<div class="footer-info">Generated by Quiz Bot &bull; Practice Sheet<br/><span class="creator-tag">✍️ {_escape_html(CREATOR_LINE)}</span></div>
</body></html>"""

    try:
        HTML(string=html_content, base_url=".").write_pdf(output_path)
        logger.info("✅ PDF written in %.1fs → %s (no AI solutions)", time.time() - t_start, output_path)
        return True, {}
    except Exception as e:
        logger.error("WeasyPrint PDF generation failed after %.1fs: %s", time.time() - t_start, e)
        return False, {}


# ══════════════════════════════════════════════════════════════════════
# NOTES RENDERER (2-COLUMN NEWSPAPER STYLE)
# ══════════════════════════════════════════════════════════════════════

_NOTES_SEP_RE = re.compile(r"^\s*[=\-*_~]{4,}\s*$")
_NOTES_BULLET_RE = re.compile(r"^(\s*)[-•·●○▪◆➤►→]\s+(.*)$")
_NOTES_ORDERED_RE = re.compile(r"^(\s*)((?:[A-Za-z]{1,3}|\d{1,3})[.)])\s+(.*)$")
_NOTES_FENCE_RE = re.compile(r"^\s*```")
_NOTES_COLON_HEAD_RE = re.compile(r"^(.{3,90}?)\s*[:：]\s*$")


def _notes_inline(text: str) -> str:
    s = _html_mod.escape(text)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__(.+?)__", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"(?<![_\w])_([^_\n]+?)_(?![_\w])", r"<em>\1</em>", s)
    s = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", s)
    return s


def _parse_notes_text(text: str, default_title: str = "Notes") -> dict:
    if not text:
        return {"title": default_title, "blocks": [], "toc": []}
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks, toc = [], []
    counters = {"h1": 0, "h2": 0, "h3": 0}
    cur_ul, cur_ol = None, None

    def flush_lists():
        nonlocal cur_ul, cur_ol
        if cur_ul:
            blocks.append({"type": "ul", "items": cur_ul})
            cur_ul = None
        if cur_ol:
            blocks.append({"type": "ol", "items": cur_ol})
            cur_ol = None

    def add_heading(level: int, title: str):
        flush_lists()
        counters[f"h{level}"] += 1
        anchor = f"h{level}-{counters[f'h{level}']}"
        blocks.append({"type": f"h{level}", "text": _notes_inline(title), "anchor": anchor})
        if level <= 2:
            toc.append((level, anchor, title))

    def is_headingish(s: str, nxt: str) -> bool:
        if not s or len(s) > 130:
            return False
        if s[0] in "-•·*◆➤►>":
            return False
        if s.endswith((".", "।", ",", ";")):
            return False
        if not any(c.isalpha() for c in s):
            return False
        n = (nxt or "").strip()
        return (not n) or bool(_NOTES_BULLET_RE.match(nxt or "")) or bool(_NOTES_ORDERED_RE.match(nxt or ""))

    i, in_code, code_buf = 0, False, []
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        if _NOTES_FENCE_RE.match(line):
            if in_code:
                blocks.append({"type": "code", "text": "\n".join(code_buf)})
                code_buf, in_code = [], False
            else:
                flush_lists()
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(raw)
            i += 1
            continue

        if _NOTES_SEP_RE.match(line):
            if i + 2 < len(lines) and lines[i + 1].strip() and _NOTES_SEP_RE.match(lines[i + 2]):
                add_heading(1, lines[i + 1].strip())
                i += 3
                continue
            flush_lists()
            blocks.append({"type": "hr"})
            i += 1
            continue

        if not line.strip():
            flush_lists()
            i += 1
            continue

        m = _NOTES_BULLET_RE.match(line)
        if m:
            indent = len(m.group(1).replace("\t", "    "))
            lvl = 0 if indent < 2 else (1 if indent < 4 else 2)
            if cur_ul is None or cur_ol is not None:
                flush_lists()
                cur_ul = []
            cur_ul.append({"level": lvl, "html": _notes_inline(m.group(2))})
            i += 1
            continue

        m = _NOTES_ORDERED_RE.match(line)
        if m:
            indent = len(m.group(1).replace("\t", "    "))
            lvl = 0 if indent < 2 else (1 if indent < 4 else 2)
            if cur_ol is None or cur_ul is not None:
                flush_lists()
                cur_ol = []
            cur_ol.append({"level": lvl, "marker": m.group(2), "html": _notes_inline(m.group(3))})
            i += 1
            continue

        if line.lstrip().startswith(">"):
            flush_lists()
            blocks.append({"type": "quote", "text": _notes_inline(line.lstrip()[1:].strip())})
            i += 1
            continue

        cm = _NOTES_COLON_HEAD_RE.match(line.strip())
        if cm and not line.strip().startswith(("-", "•")):
            add_heading(3, cm.group(1).strip())
            i += 1
            continue

        if is_headingish(line.strip(), lines[i + 1] if i + 1 < len(lines) else ""):
            add_heading(2, line.strip())
            i += 1
            continue

        flush_lists()
        para = [line.strip()]
        j = i + 1
        while j < len(lines):
            nl = lines[j].rstrip()
            if not nl.strip() or _NOTES_SEP_RE.match(nl) or _NOTES_BULLET_RE.match(nl) or _NOTES_ORDERED_RE.match(nl) or _NOTES_FENCE_RE.match(nl):
                break
            if is_headingish(nl.strip(), lines[j + 1] if j + 1 < len(lines) else ""):
                break
            if _NOTES_COLON_HEAD_RE.match(nl.strip()) and len(nl.strip()) < 80:
                break
            para.append(nl.strip())
            j += 1
        blocks.append({"type": "p", "text": _notes_inline(" ".join(para))})
        i = j

    flush_lists()
    if in_code and code_buf:
        blocks.append({"type": "code", "text": "\n".join(code_buf)})
    h1 = next((b["text"] for b in blocks if b["type"] == "h1"), None)
    title = h1 or default_title
    title = re.sub(r"<[^>]+>", "", title)
    return {"title": title, "blocks": blocks, "toc": toc}


_NOTES_PDF_CSS = """
@page { size: A4; margin: 1.2cm 1.2cm 1.4cm 1.2cm; @bottom-center { content: "Page " counter(page) " of " counter(pages); font-family: 'Noto Sans', sans-serif; font-size: 8pt; color: #94a3b8; } }
body { font-family: 'Noto Sans', 'Noto Sans Devanagari', sans-serif; color: #1e293b; font-size: 9.5pt; line-height: 1.5; background: #fff; orphans: 3; widows: 3; }
.notes-cover { page-break-after: always; text-align: center; padding-top: 6cm; }
.cover-icon { font-size: 60pt; margin-bottom: 22pt; }
.cover-title { font-family: 'Merriweather', serif; font-size: 26pt; color: #1e3a8a; margin: 0 0 10pt 0; letter-spacing: -0.3pt; line-height: 1.2; }
.cover-meta { font-size: 10pt; color: #64748b; letter-spacing: 0.4pt; text-transform: uppercase; }
.notes-content { column-count: 2; column-gap: 16pt; column-rule: 1pt solid #e2e8f0; }
.notes-h1 { column-span: all; background: linear-gradient(90deg, #1e3a8a, #2563eb); color: #fff; padding: 6pt 12pt; margin: 12pt 0 8pt 0; font-family: 'Merriweather', serif; font-size: 13pt; font-weight: 700; border-radius: 4pt; letter-spacing: 0.3pt; page-break-after: avoid; }
.notes-h2 { column-span: all; color: #1e3a8a; font-size: 11pt; font-weight: 700; margin: 10pt 0 4pt 0; padding: 4pt 0 4pt 10pt; border-left: 4pt solid #2563eb; background: #eff6ff; border-radius: 0 4pt 4pt 0; page-break-after: avoid; }
.notes-h3 { color: #4338ca; font-size: 10pt; font-weight: 700; margin: 8pt 0 3pt 0; page-break-after: avoid; }
.notes-p { margin: 0 0 6pt 0; text-align: justify; color: #334155; break-inside: avoid; }
.notes-ul, .notes-ol { margin: 2pt 0 6pt 0; padding-left: 2pt; list-style: none; }
.notes-ul li, .notes-ol li { padding-left: 2pt; margin-bottom: 3pt; color: #334155; line-height: 1.5; break-inside: avoid; page-break-inside: avoid; }
.notes-ul li.indent-1 { padding-left: 12pt; }
.notes-ul li.indent-2 { padding-left: 24pt; }
.notes-ol li.indent-1 { padding-left: 12pt; }
.notes-ol li.indent-2 { padding-left: 24pt; }
.notes-ul .bm { color: #2563eb; font-weight: 700; margin-right: 4pt; }
.notes-ol .om { color: #1e3a8a; font-weight: 700; margin-right: 4pt; }
.notes-quote { margin: 6pt 0; padding: 4pt 8pt; border-left: 3pt solid #f59e0b; background: #fef3c7; font-style: italic; color: #78350f; border-radius: 0 4pt 4pt 0; break-inside: avoid; }
.notes-pre { margin: 6pt 0; padding: 6pt 8pt; background: #f1f5f9; border-left: 3pt solid #64748b; border-radius: 4pt; overflow-x: auto; font-family: 'Courier New', monospace; font-size: 8pt; break-inside: avoid; }
.notes-hr { border: none; border-top: 1pt dashed #cbd5e1; margin: 10pt 0; column-span: all; }
code { background: #f1f5f9; padding: 1pt 3pt; border-radius: 3pt; font-family: 'Courier New', monospace; font-size: 8.5pt; }
"""


def _render_notes_body_html(parsed: dict, cover_title: str) -> str:
    parts: list[str] = []
    n_h2 = sum(1 for b in parsed["blocks"] if b["type"] == "h2")

    parts.append(
        '<section class="notes-cover">'
        f'<div class="cover-icon">📓</div>'
        f'<h1 class="cover-title">{_escape_html(cover_title)}</h1>'
        f'<div class="cover-meta">{n_h2} section{"s" if n_h2 != 1 else ""}</div>'
        "</section>"
    )

    parts.append('<div class="notes-content">')

    for b in parsed["blocks"]:
        t = b["type"]
        if t in ("h1", "h2", "h3"):
            lvl = t[1]
            parts.append(f'<h{lvl} class="notes-{t}">{b["text"]}</h{lvl}>')
        elif t == "p":
            parts.append(f'<p class="notes-p">{b["text"]}</p>')
        elif t == "ul":
            items_html = "".join(
                f'<li class="indent-{it["level"]}"><span class="bm">•</span>{it["html"]}</li>'
                for it in b["items"]
            )
            parts.append(f'<ul class="notes-ul">{items_html}</ul>')
        elif t == "ol":
            items_html = "".join(
                f'<li class="indent-{it["level"]}"><span class="om">{_escape_html(it["marker"])}</span>{it["html"]}</li>'
                for it in b["items"]
            )
            parts.append(f'<ol class="notes-ol">{items_html}</ol>')
        elif t == "quote":
            parts.append(f'<blockquote class="notes-quote">{b["text"]}</blockquote>')
        elif t == "code":
            parts.append(f'<pre class="notes-pre"><code>{_escape_html(b["text"])}</code></pre>')
        elif t == "hr":
            parts.append('<hr class="notes-hr">')

    parts.append('</div>')

    return "\n".join(parts)


def render_notes_pdf(
    text: str,
    title: str,
    output_path: str,
    style: PdfStyle = "classic",
) -> bool:
    try:
        from weasyprint import HTML
    except ImportError:
        logger.error("WeasyPrint not installed.")
        return False

    parsed = _parse_notes_text(text, default_title=title)
    body = _render_notes_body_html(parsed, cover_title=title or parsed["title"])

    full_html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;600;700&family=Noto+Sans+Devanagari:wght@400;600;700&family=Merriweather:wght@700&display=swap" rel="stylesheet">
<style>{_NOTES_PDF_CSS}</style>
</head><body>
{body}
</body></html>"""

    try:
        HTML(string=full_html, base_url=".").write_pdf(output_path)
        return True
    except Exception as e:
        logger.error("render_notes_pdf failed: %s", e, exc_info=True)
        return False
