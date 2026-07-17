"""FastAPI app: web UI + the background IMAP poller in one process, so a
single healthcheck covers both."""
from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import config, db, poller, web

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = app.state.cfg
    with db.connect(cfg.data_dir):
        pass  # create schema up front

    stop_event = threading.Event()
    thread = None
    if os.environ.get("DISABLE_POLLER") != "1":
        problems = config.validate_for_polling(cfg)
        if problems:
            log.error("Poller NOT started; fix configuration: %s", "; ".join(problems))
        else:
            thread = threading.Thread(
                target=poller.run_forever, args=(cfg, stop_event), daemon=True, name="poller"
            )
            thread.start()
    yield
    stop_event.set()
    if thread:
        thread.join(timeout=10)


def create_app() -> FastAPI:
    cfg = config.load()
    if not cfg.web_password:
        log.warning("WEB_PASSWORD is not set — web UI logins will always fail.")
    app = FastAPI(title="Tax Assistant", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.middleware("http")(web.auth_middleware)
    app.include_router(web.router)
    return app


app = create_app()
