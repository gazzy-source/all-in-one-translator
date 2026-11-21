"""Entrypoint: `python -m translator_bot` (or the `translator-bot` console script)."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys

from aiogram import Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from translator_bot.app import App
from translator_bot.config import ConfigError, Settings, load_settings
from translator_bot.handlers import build_routers, register_commands

# chat_member: membership changes (bot must be admin) -> a removed recipient loses access immediately
ALLOWED_UPDATES = ["message", "edited_message", "my_chat_member", "chat_member", "callback_query", "inline_query"]

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {"ts": self.formatTime(record), "level": record.levelname, "logger": record.name,
               "msg": record.getMessage()}
        doc.update({k: v for k, v in record.__dict__.items() if k not in _STD})
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str, ensure_ascii=False)


class ExtraFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = {k: v for k, v in record.__dict__.items() if k not in _STD}
        return f"{base} {extra}" if extra else base


def setup_logging(level: str, as_json: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if as_json else ExtraFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    # httpx logs full request URLs at INFO; keep third-party noise (and any key-bearing URLs) out
    for noisy in ("httpx", "httpcore", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def health_app(app: App) -> web.Application:
    async def healthz(_: web.Request) -> web.Response:
        ok = app.pipeline is not None
        body = {"ok": ok, "providers": sorted(app.providers), "guard_blocked": app.guard.blocked,
                "queues": {"text": app.pipeline.text_lane.size, "media": app.pipeline.media_lane.size}
                if app.pipeline else {}}
        return web.json_response(body, status=200 if ok else 503)

    wapp = web.Application()
    wapp.router.add_get("/healthz", healthz)
    return wapp


async def run(settings: Settings) -> None:
    app = App(settings)
    await app.start()
    dp = Dispatcher()
    for router in build_routers(app):
        dp.include_router(router)
    await register_commands(app)
    log = logging.getLogger("translator_bot")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    runner: web.AppRunner | None = None
    try:
        if settings.run_mode == "webhook":
            assert settings.webhook_url and settings.webhook_secret
            wapp = health_app(app)
            SimpleRequestHandler(dispatcher=dp, bot=app.bot,
                                 secret_token=settings.webhook_secret.get_secret_value()).register(wapp, path="/webhook")
            setup_application(wapp, dp, bot=app.bot)
            runner = web.AppRunner(wapp)
            await runner.setup()
            await web.TCPSite(runner, settings.listen_host, settings.listen_port).start()
            await app.bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret.get_secret_value(),
                                      allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False)
            log.info("webhook mode", extra={"listen": f"{settings.listen_host}:{settings.listen_port}"})
            await stop.wait()
        else:
            if settings.health_port:
                runner = web.AppRunner(health_app(app))
                await runner.setup()
                await web.TCPSite(runner, settings.listen_host, settings.health_port).start()
            await app.bot.delete_webhook(drop_pending_updates=False)
            log.info("polling mode", extra={"bot": app.bot_username})
            polling = asyncio.create_task(dp.start_polling(app.bot, allowed_updates=ALLOWED_UPDATES,
                                                           handle_signals=False, close_bot_session=False))
            stopper = asyncio.create_task(stop.wait())
            await asyncio.wait({polling, stopper}, return_when=asyncio.FIRST_COMPLETED)
            await dp.stop_polling()
            await asyncio.gather(polling, return_exceptions=True)
            stopper.cancel()
    finally:
        log.info("shutting down: draining queues")
        if runner is not None:
            await runner.cleanup()
        await app.stop()


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None
    setup_logging(settings.log_level, settings.log_json)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
