from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .api import DEFAULT_BASE_URL
from .auth import fetch_qr_url, poll_login
from .types import BotConfig, BotCredentials, BotStatus, IncomingMessage, LoginStatus
from .worker import BotWorker

CredentialLoader = Callable[[str], Awaitable[BotCredentials | None]]
CredentialUpdateHandler = Callable[[str, BotCredentials], Any]
MessageHandler = Callable[[IncomingMessage], Any]
ErrorHandler = Callable[[str, Exception], Any]
LoginStatusHandler = Callable[[LoginStatus], Any]


class BotServer:
    """Multi-user WeChat iLink Bot server.

    Manages a pool of :class:`BotWorker` instances, one per bot account.
    All callbacks are registered via decorators::

        server = BotServer()

        @server.credential_loader
        async def load(bot_id): ...

        @server.on_credential_update
        async def save(bot_id, creds): ...

        @server.on_error
        def on_err(bot_id, exc): ...

        @server.on_login_status
        async def on_login(status: LoginStatus): ...

        @server.on_message
        async def on_msg(msg: IncomingMessage): ...
    """

    def __init__(self) -> None:
        self._credential_loader: CredentialLoader | None = None
        self._credential_update_handler: CredentialUpdateHandler | None = None
        self._on_error: ErrorHandler | None = None
        self._login_status_handlers: list[LoginStatusHandler] = []
        self._handlers: list[MessageHandler] = []
        self._workers: dict[str, BotWorker] = {}
        self._session: httpx.AsyncClient | None = None
        self._initialized = False
        self._login_tasks: dict[str, asyncio.Task[None]] = {}

    # -- decorator registration ----------------------------------------------

    def credential_loader(self, fn: CredentialLoader) -> CredentialLoader:
        """Register the credential loader::

            @server.credential_loader
            async def load(bot_id: str) -> BotCredentials | None: ...
        """
        self._credential_loader = fn
        return fn

    def credential_saver(self, fn: CredentialUpdateHandler) -> CredentialUpdateHandler:
        """Deprecated alias for :meth:`on_credential_update`."""
        return self.on_credential_update(fn)

    def on_credential_update(
        self, handler: CredentialUpdateHandler
    ) -> CredentialUpdateHandler:
        """Register a credential-update handler::

            @server.on_credential_update
            async def save(bot_id: str, creds: BotCredentials) -> None: ...

        Called whenever credentials need to be persisted — after login
        confirmation **and** whenever the long-poll cursor
        (``get_updates_buf``) changes.
        """
        self._credential_update_handler = handler
        return handler

    def on_error(self, handler: ErrorHandler) -> ErrorHandler:
        """Register an error handler::

            @server.on_error
            def handle_error(bot_id: str, error: Exception) -> None: ...
        """
        self._on_error = handler
        return handler

    def on_message(self, handler: MessageHandler) -> MessageHandler:
        """Register a message handler::

            @server.on_message
            async def handle(msg: IncomingMessage) -> None: ...
        """
        self._handlers.append(handler)
        return handler

    def on_login_status(self, handler: LoginStatusHandler) -> LoginStatusHandler:
        """Register a login-status handler::

            @server.on_login_status
            async def handle(status: LoginStatus) -> None:
                if status.status == "confirmed":
                    print("logged in")
        """
        self._login_status_handlers.append(handler)
        return handler

    # -- login ---------------------------------------------------------------

    async def start_login(
        self,
        bot_id: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 120.0,
    ) -> str:
        """Start the QR login flow for *bot_id*.

        Fetches a new QR code, returns the URL immediately, and starts
        background polling for the user to scan and confirm.

        Returns
        -------
        str
            The QR URL the user should open in WeChat to log in.

        Status events arrive via ``@server.on_login_status``:

        * ``scaned``   — User scanned the code; waiting for confirmation.
        * ``confirmed``— Login succeeded; credentials saved, bot started.
        * ``expired``  — QR code expired before scanning.
        * ``timeout``  — ``timeout_s`` elapsed without confirmation.
        * ``error``    — Unexpected error during the flow.
        """
        # Cancel any in-flight login for the same bot
        existing = self._login_tasks.pop(bot_id, None)
        if existing and not existing.done():
            existing.cancel()

        client = await self._ensure_client()

        # Fetch QR code synchronously — the caller gets the URL directly.
        qr_url, qrcode_token = await fetch_qr_url(client, base_url)

        async def _on_status(status: LoginStatus) -> None:
            await self._dispatch_login_status(status)
            if status.status == "confirmed" and status.credentials is not None:
                await self._on_login_confirmed(bot_id, status.credentials)

        task = asyncio.create_task(
            poll_login(client, bot_id, qrcode_token, _on_status, base_url, timeout_s)
        )
        self._login_tasks[bot_id] = task
        task.add_done_callback(lambda t: self._login_tasks.pop(bot_id, None))

        return qr_url

    # -- lifecycle -----------------------------------------------------------

    async def init(self, configs: list[BotConfig]) -> None:
        """Initialize the server with a list of bot configs.

        For each config, credentials are loaded via the registered
        ``credential_loader``.  Bots whose credentials are not yet available
        are silently skipped — call :meth:`start_login` to obtain them.
        """
        if self._initialized:
            raise RuntimeError(
                "BotServer is already initialized. Call shutdown() first."
            )
        if self._credential_loader is None:
            raise RuntimeError(
                "No credential_loader registered. "
                "Use @server.credential_loader to register one before calling init()."
            )

        await self._ensure_client()
        self._initialized = True

        for config in configs:
            try:
                await self._start_worker(config.bot_id)
            except Exception as err:
                self._log(f"Failed to start bot {config.bot_id}: {err}")
                await self._invoke_error_handler(config.bot_id, err)

    async def add_bot(self, config: BotConfig) -> None:
        """Add and start a new bot dynamically after :meth:`init`."""
        self._ensure_initialized()
        if config.bot_id in self._workers:
            raise ValueError(
                f"Bot {config.bot_id} already exists. Remove it first."
            )
        await self._start_worker(config.bot_id)

    async def remove_bot(self, bot_id: str) -> None:
        """Stop and remove a running bot."""
        worker = self._workers.pop(bot_id, None)
        if worker is None:
            raise KeyError(f"Bot {bot_id} not found.")
        await worker.stop()
        self._log(f"Bot {bot_id} removed.")

    async def restart_bot(self, bot_id: str) -> None:
        """Restart a bot: stop it, reload credentials, and start again."""
        self._ensure_initialized()
        old_worker = self._workers.pop(bot_id, None)
        if old_worker is not None:
            await old_worker.stop()
        await self._start_worker(bot_id)

    async def shutdown(self) -> None:
        """Stop all bots, cancel pending logins, and release resources."""
        for task in list(self._login_tasks.values()):
            task.cancel()
        self._login_tasks.clear()

        tasks = [worker.stop() for worker in self._workers.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()

        if self._session is not None:
            await self._session.aclose()
            self._session = None

        self._initialized = False
        self._log("Server shut down.")

    def run(self, configs: list[BotConfig]) -> None:
        """Blocking convenience entry-point."""
        asyncio.run(self._run_blocking(configs))

    # -- status --------------------------------------------------------------

    def get_status(self) -> dict[str, BotStatus]:
        """Return a ``{bot_id: BotStatus}`` mapping for every bot."""
        return {
            bot_id: worker.status
            for bot_id, worker in self._workers.items()
        }

    def get_bot_status(self, bot_id: str) -> BotStatus:
        """Return the status of a single bot."""
        worker = self._workers.get(bot_id)
        if worker is None:
            raise KeyError(f"Bot {bot_id} not found.")
        return worker.status

    # -- messaging -----------------------------------------------------------

    async def reply(self, message: IncomingMessage, text: str) -> None:
        """Reply to an incoming message through the originating bot."""
        worker = self._workers.get(message.bot_id)
        if worker is None:
            raise KeyError(f"Bot {message.bot_id} not found.")
        await worker.reply(message, text)

    async def send(self, bot_id: str, user_id: str, text: str) -> None:
        """Send a message to *user_id* through the specified bot."""
        worker = self._workers.get(bot_id)
        if worker is None:
            raise KeyError(f"Bot {bot_id} not found.")
        await worker.send(user_id, text)

    async def send_typing(self, bot_id: str, user_id: str) -> None:
        """Show "typing …" indicator to *user_id*."""
        worker = self._workers.get(bot_id)
        if worker is None:
            raise KeyError(f"Bot {bot_id} not found.")
        await worker.send_typing(user_id)

    async def stop_typing(self, bot_id: str, user_id: str) -> None:
        """Cancel the "typing …" indicator for *user_id*."""
        worker = self._workers.get(bot_id)
        if worker is None:
            raise KeyError(f"Bot {bot_id} not found.")
        await worker.stop_typing(user_id)

    # -- internals -----------------------------------------------------------

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._session is None:
            self._session = httpx.AsyncClient()
        return self._session

    async def _on_login_confirmed(
        self, bot_id: str, credentials: BotCredentials
    ) -> None:
        await self._fire_credential_update(bot_id, credentials)

        if self._initialized:
            old_worker = self._workers.pop(bot_id, None)
            if old_worker is not None:
                await old_worker.stop()
            try:
                await self._start_worker_with_credentials(bot_id, credentials)
            except Exception as err:
                self._log(f"Failed to start bot {bot_id} after login: {err}")
                await self._invoke_error_handler(bot_id, err)

    async def _start_worker(self, bot_id: str) -> None:
        assert self._credential_loader is not None
        credentials = await self._credential_loader(bot_id)
        if credentials is None:
            self._log(f"No credentials for bot {bot_id}, skipping start.")
            return
        await self._start_worker_with_credentials(bot_id, credentials)

    async def _start_worker_with_credentials(
        self, bot_id: str, credentials: BotCredentials
    ) -> None:
        assert self._session is not None
        worker = BotWorker(
            bot_id=bot_id,
            credentials=credentials,
            session=self._session,
            message_callback=self._dispatch_message,
            error_callback=self._handle_worker_error,
            cursor_update_callback=self._handle_cursor_update,
        )
        self._workers[bot_id] = worker
        worker.start()
        self._log(
            f"Bot {bot_id} started (account: {credentials.account_id})."
        )

    async def _dispatch_login_status(self, status: LoginStatus) -> None:
        for handler in self._login_status_handlers:
            try:
                await self._maybe_await(handler(status))
            except Exception as err:
                self._log(f"on_login_status handler raised: {err}")

    async def _fire_credential_update(
        self, bot_id: str, credentials: BotCredentials
    ) -> None:
        if self._credential_update_handler is None:
            return
        try:
            await self._maybe_await(
                self._credential_update_handler(bot_id, credentials)
            )
        except Exception as err:
            self._log(f"on_credential_update failed for {bot_id}: {err}")

    async def _handle_cursor_update(
        self, bot_id: str, new_cursor: str
    ) -> None:
        """Called by :class:`BotWorker` whenever ``get_updates_buf`` changes."""
        worker = self._workers.get(bot_id)
        if worker is None:
            return
        # Mutate the in-memory credentials so the next persist has the cursor.
        creds = worker.credentials
        creds.get_updates_buf = new_cursor
        await self._fire_credential_update(bot_id, creds)

    async def _dispatch_message(self, message: IncomingMessage) -> None:
        if not self._handlers:
            return
        results = await asyncio.gather(
            *(
                self._call_handler(handler, message)
                for handler in self._handlers
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                await self._invoke_error_handler(message.bot_id, result)

    async def _call_handler(
        self, handler: MessageHandler, message: IncomingMessage
    ) -> None:
        await self._maybe_await(handler(message))

    async def _handle_worker_error(
        self, bot_id: str, error: Exception
    ) -> None:
        await self._invoke_error_handler(bot_id, error)

    async def _invoke_error_handler(
        self, bot_id: str, error: Exception
    ) -> None:
        if self._on_error is None:
            return
        try:
            await self._maybe_await(self._on_error(bot_id, error))
        except Exception:
            pass

    @staticmethod
    async def _maybe_await(result: Any) -> None:
        if inspect.isawaitable(result):
            await result

    async def _run_blocking(self, configs: list[BotConfig]) -> None:
        await self.init(configs)
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.shutdown()

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "BotServer is not initialized. Call init() first."
            )

    def _log(self, message: str) -> None:
        sys.stderr.write(f"[ilink-bot-server] {message}\n")
