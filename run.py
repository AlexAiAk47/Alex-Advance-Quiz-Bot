#!/usr/bin/env python3
"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import os
import platform
import subprocess
import ctypes.util
import shutil

from quizbot.database import init_db, close_db
from quizbot.shared import config
from quizbot.shared.utils.http import close_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("launcher")

# Silence noisy third-party debug logs by default.
for noisy in ("httpx", "httpcore", "apscheduler", "pymongo"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def auto_install_dependencies():
    """Auto-install WeasyPrint system dependencies on Linux if missing."""
    if platform.system() != "Linux":
        return

    # Agar apt-get available nahi hai (jaise Heroku par), toh skip karein
    if not shutil.which("apt-get"):
        logger.info("apt-get not found. Skipping auto-install (Use Aptfile if on Heroku).")
        return

    # Check karein ki library pehle se installed hai ya nahi
    if ctypes.util.find_library("gobject-2.0") is not None:
        return  # Pehle se hai, kuch karne ki zaroorat nahi

    logger.warning("Missing system libraries for PDF/HTML generation. Auto-installing...")
    
    libs = [
        "libpango-1.0-0", "libpangocairo-1.0-0", "libcairo2",
        "libgdk-pixbuf-2.0-0", "shared-mime-info", "libffi-dev"
    ]

    try:
        # Check karein ki script root (sudo) par chal rahi hai ya normal user
        use_sudo = os.geteuid() != 0
        prefix = ["sudo"] if use_sudo else []

        logger.info("Running apt-get update...")
        subprocess.run(prefix + ["apt-get", "update", "-y"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        logger.info("Installing PDF libraries...")
        subprocess.run(prefix + ["apt-get", "install", "-y"] + libs, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        logger.info("✅ Libraries installed successfully!")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Auto-install failed. Please run manually: sudo apt-get install -y {' '.join(libs)}")
        logger.error(f"Error: {e}")
    except Exception as e:
        logger.error(f"❌ Error during auto-install: {e}")


async def _run_creator_bot() -> None:
    from quizbot.creator_bot.bot import run_creator_bot

    logger.info("Starting Creator Bot (Pyrogram)...")
    await run_creator_bot()


async def _run_runner_bot() -> None:
    from quizbot.runner_bot.bot import run_runner_bot

    logger.info("Starting Runner Bot (python-telegram-bot)...")
    await run_runner_bot()


async def _run_mini_app() -> None:
    from quizbot.mini_app.server import run_mini_app_server

    logger.info("Starting Mini App server (FastAPI)...")
    await run_mini_app_server()


async def main(only: str | None) -> None:
    problems = config.validate(bot=only or "both")
    if problems:
        for p in problems:
            logger.error("Config problem: %s", p)
        logger.error("Fix the above in your .env file (see .env.example) before starting.")
        raise SystemExit(1)

    logger.info("Connecting to MongoDB (db=%s) ...", config.MONGODB_DB_NAME)
    await init_db(config.MONGODB_URI, config.MONGODB_DB_NAME)
    logger.info("Database ready.")

    want_mini_app = only == "miniapp" or (only is None and config.RUN_MINI_APP)

    tasks: list[asyncio.Task] = []
    if only in (None, "creator"):
        tasks.append(asyncio.create_task(_run_creator_bot(), name="creator_bot"))
    if only in (None, "runner"):
        tasks.append(asyncio.create_task(_run_runner_bot(), name="runner_bot"))
    if want_mini_app:
        tasks.append(asyncio.create_task(_run_mini_app(), name="mini_app"))

    if want_mini_app and only is not None:
        logger.info(
            "Mini App listening on %s:%s (public URL: %s)",
            config.MINI_APP_HOST,
            config.MINI_APP_PORT,
            config.MINI_APP_DOMAIN or "<not set -- attach a domain / set MINI_APP_DOMAIN>",
        )
    elif want_mini_app:
        if config.MINI_APP_DOMAIN:
            logger.info("Mini App public URL: %s (Play buttons enabled)", config.MINI_APP_DOMAIN)
        else:
            logger.warning(
                "Mini App server is running on port %s but no public URL is known yet, "
                "so the bots will skip their 'Play' buttons. Attach a public domain to "
                "this service or set MINI_APP_DOMAIN, then redeploy.",
                config.MINI_APP_PORT,
            )

    stop_event = asyncio.Event()

    def _handle_signal(*_args):
        logger.info("Shutdown signal received, stopping bots...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows

    crashed: BaseException | None = None
    try:
        done, pending = await asyncio.wait(
            [*tasks, asyncio.ensure_future(stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None:
                crashed = exc
                logger.error("Component %r crashed: %s", t.get_name(), exc)
                logger.debug("Traceback for %r:", t.get_name(), exc_info=exc)
    finally:
        logger.info("Shutting down...")
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_session()
        await close_db()
        logger.info("Shutdown complete.")

    if crashed is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    # Start hone se pehle libraries check karein aur install karein
    auto_install_dependencies()

    parser = argparse.ArgumentParser(description="Run the Advance Quiz Bot platform.")
    parser.add_argument(
        "--only", choices=["creator", "runner", "miniapp"], default=None,
        help="Run only one component (default: run both bots, plus the "
             "Mini App server too if MINI_APP_DOMAIN is configured).",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args.only))
    except KeyboardInterrupt:
        pass
