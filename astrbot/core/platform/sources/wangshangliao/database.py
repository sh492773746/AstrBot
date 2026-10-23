"""Thread-owned SQLite transactions for Wangshangliao business coroutines."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from .wire import ProtocolError


class AsyncDatabase:
    """Keep a complete transaction and all its cursors on one worker thread.

    Args:
        factory: Synchronous connection factory, including additive migrations.
        write: Reserve the writer before reading state used by a mutation.
        guard: Synchronous authority check, called after admission and before commit.
    """

    def __init__(self, factory, *, write=False, guard=None):
        self.factory = factory
        self.write = write
        self.guard = guard
        self.connection = None
        self.executor = None
        self.cancelled = False

    def _open(self):
        self.connection = self.factory()
        if self.write:
            self.connection.execute("BEGIN IMMEDIATE")

    def _finish(self, commit):
        if self.connection is None:
            return
        try:
            if commit and not self.cancelled:
                if self.guard is not None:
                    self.guard()
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
            self.connection = None

    async def _run(self, function, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, partial(function, *args, **kwargs)
        )

    async def _close(self, commit):
        # Closing is queued behind any in-flight SQLite call. Do not abandon a
        # connection or let a cancelled coroutine leave a later committing write.
        closing = asyncio.create_task(self._run(self._finish, commit))
        interrupted = False
        try:
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    self.cancelled = True
                    interrupted = True
            closing.result()
        finally:
            self.executor.shutdown(wait=False)
        if interrupted:
            raise asyncio.CancelledError

    async def __aenter__(self):
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="wsl-database"
        )
        try:
            await self._run(self._open)
            if self.guard is not None:
                self.guard()
            return self
        except BaseException:
            self.cancelled = True
            await self._close(False)
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        await self._close(exc_type is None)

    def _query(self, method, sql, parameters):
        cursor = self.connection.execute(sql, parameters)
        try:
            if method == "execute":
                return cursor.rowcount
            return getattr(cursor, method)()
        finally:
            cursor.close()

    async def execute(self, sql, parameters=()):
        """Execute SQL without returning a thread-owned cursor.

        Args:
            sql: Parameterized statement.
            parameters: Bound statement values.

        Returns:
            Affected row count.
        """
        return await self._run(self._query, "execute", sql, parameters)

    async def fetchone(self, sql, parameters=()):
        """Read one detached row.

        Args:
            sql: Parameterized query.
            parameters: Bound query values.

        Returns:
            Row or None.
        """
        return await self._run(self._query, "fetchone", sql, parameters)

    async def fetchall(self, sql, parameters=()):
        """Read detached query results.

        Args:
            sql: Parameterized query.
            parameters: Bound query values.

        Returns:
            List of rows.
        """
        return await self._run(self._query, "fetchall", sql, parameters)

    async def call(self, function, *args, **kwargs):
        """Run an existing synchronous ledger operation in this transaction.

        Args:
            function: Callable accepting the worker connection first.
            args: Positional ledger arguments.
            kwargs: Keyword ledger arguments.

        Returns:
            Detached operation result.
        """
        return await self._run(function, self.connection, *args, **kwargs)


def account_guard(adapter, authorize=None):
    """Freeze the account and business generation before waiting for a database.

    Args:
        adapter: Adapter whose effective policy owns the operation.
        authorize: Optional synchronous capability or creator check.

    Returns:
        Synchronous guard usable at transaction admission and commit.
    """
    from astrbot.core.star.component_runtime import current_execution

    execution = current_execution.get()
    config = adapter.config
    identity = (
        config["id"],
        adapter.account,
        config.get("_business_epoch"),
        config.get("_business_revision"),
        config.get("_business_active"),
        config.get("enable", True),
    )

    def check():
        if execution is not None:
            execution.fence()
        current = adapter.config
        if identity != (
            current["id"],
            adapter.account,
            current.get("_business_epoch"),
            current.get("_business_revision"),
            current.get("_business_active"),
            current.get("enable", True),
        ):
            raise ProtocolError("database_authority_changed")
        if authorize is not None:
            authorize()

    return check
