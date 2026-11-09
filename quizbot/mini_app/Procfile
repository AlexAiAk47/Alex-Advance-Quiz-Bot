# Railway (and Heroku-style) process definition.
# The Dockerfile is what Railway builds by default; this file is the
# fallback for builders that read Procfile instead (Nixpacks/Railpack),
# and it must expose a single `web` process on $PORT.
#
# `python run.py` starts BOTH Telegram bots AND the Mini App web server in
# this one process -- required because a PaaS only routes traffic to the
# port the web process binds ($PORT).
web: python run.py
