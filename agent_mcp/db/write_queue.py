# Agent-MCP/mcp_template/mcp_server_src/db/write_queue.py
import asyncio
import sqlite3
from typing import Any, Callable, Optional, Awaitable
from ..core.config import logger


class DatabaseWriteQueue:
    """
    A queue system for serializing database write operations to prevent SQLite lock contention.

    This class ensures that all write operations (INSERT, UPDATE, DELETE) are executed
    sequentially while allowing concurrent read operations to proceed normally.
    """

    def __init__(self):
        # NOTE: Queue creation is deferred to `_ensure_running_on_current_loop`.
        # `asyncio.Queue` binds to whichever loop is running at construction
        # time (or to no loop at all if none is running) — and once bound it
        # cannot be awaited from a different loop. Tests run each test in a
        # fresh `asyncio.run` block, so any queue created eagerly here would
        # be poisoned by the time the next test arrives. We rebuild on every
        # `execute_write` call when the bound loop differs from
        # `asyncio.get_running_loop()`.
        self.queue: Optional[asyncio.Queue] = None
        self.worker_task: Optional[asyncio.Task] = None
        self.running: bool = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stats = {
            "total_operations": 0,
            "successful_operations": 0,
            "failed_operations": 0,
            "queue_high_water_mark": 0,
        }

    async def start(self) -> None:
        """Start the write queue worker task on the current event loop.

        Idempotent for the current loop. If `start()` is called from a
        loop that differs from the one the worker is currently bound to
        (e.g. the previous loop already closed and a new one took its
        place), the stale worker is dropped and a fresh one is spun up.
        """
        loop = asyncio.get_running_loop()
        if self.running and self._loop is loop:
            logger.warning("Database write queue is already running")
            return

        self._rebind_to_current_loop(loop)
        logger.info("Database write queue started")

    def _rebind_to_current_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """(Re)create the queue + worker on `loop`.

        Drops any prior worker_task reference without awaiting it — the
        previous loop is assumed dead (which is exactly why we're
        rebinding). The old `asyncio.Queue` is also dropped; any pending
        futures inside it were tied to that dead loop and cannot be
        completed from here.
        """
        self.queue = asyncio.Queue()
        self._loop = loop
        self.running = True
        self.worker_task = loop.create_task(self._worker())

    def _ensure_running_on_current_loop(self) -> None:
        """Lazily start (or rebind) the worker to the running loop.

        Called from `execute_write` so callers don't need to know about
        lifespan ordering. Three cases:

        1. First call ever on this singleton — create queue + worker on
           the running loop.
        2. Running on the same loop the worker was started on — no-op.
        3. Running on a different loop (typical in tests: each
           `asyncio.run` block is a fresh loop; the prior worker_task
           is bound to a closed loop and `await future` would deadlock).
           Drop the stale worker_task reference and rebind.
        """
        loop = asyncio.get_running_loop()
        if self.running and self._loop is loop:
            return
        self._rebind_to_current_loop(loop)

    async def stop(self) -> None:
        """Stop the write queue worker task and process remaining operations.

        Only awaits the worker_task if we're running on the same loop it
        was created on. If `stop()` is called from a loop that doesn't
        own the worker (e.g. teardown after a test ran in a different
        loop), we mark the queue stopped and drop the reference rather
        than risk an 'attached to a different loop' error.
        """
        if not self.running:
            return

        self.running = False

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        same_loop = current_loop is not None and current_loop is self._loop

        if same_loop:
            # Wait for remaining operations to complete
            if self.queue is not None:
                while not self.queue.empty():
                    await asyncio.sleep(0.1)

            # Cancel the worker task
            if self.worker_task is not None:
                self.worker_task.cancel()
                try:
                    await self.worker_task
                except asyncio.CancelledError:
                    pass

        self.worker_task = None
        self.queue = None
        self._loop = None
        logger.info("Database write queue stopped")

    async def execute_write(self, write_operation: Callable[[], Awaitable[Any]]) -> Any:
        """
        Execute a database write operation through the queue.

        Args:
            write_operation: An async function that performs the database write

        Returns:
            The result of the write operation

        Raises:
            Exception: Any exception raised by the write operation
        """
        # Loop-portable bootstrap: if we have no worker yet, or the
        # existing worker is bound to a now-closed loop (typical in
        # tests: each `asyncio.run` block is a fresh loop), lazily
        # rebuild on the current loop. Production code paths still go
        # through `start()` via the application lifespan first, so this
        # branch is a no-op in normal operation.
        self._ensure_running_on_current_loop()

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        assert self.queue is not None  # populated by _ensure_running_on_current_loop
        await self.queue.put((write_operation, future))

        # Update queue stats
        current_size = self.queue.qsize()
        if current_size > self._stats["queue_high_water_mark"]:
            self._stats["queue_high_water_mark"] = current_size

        return await future

    async def _worker(self) -> None:
        """Worker task that processes write operations sequentially.

        Binds to the queue instance that exists at worker-start time. If
        the singleton later rebinds to a different loop (tests across
        `asyncio.run` blocks), this worker's loop has already closed
        and the task is GC'd along with it — the new loop gets a fresh
        worker via `_rebind_to_current_loop`.
        """
        logger.info("Database write queue worker started")
        my_queue = self.queue
        assert my_queue is not None  # caller (_rebind_to_current_loop) just set it

        while self.running and self.queue is my_queue:
            try:
                # Wait for operation with timeout to allow clean shutdown
                operation, future = await asyncio.wait_for(
                    my_queue.get(), timeout=1.0
                )

                if future.cancelled():
                    continue

                self._stats["total_operations"] += 1

                try:
                    # Execute the write operation
                    result = await operation()
                    future.set_result(result)
                    self._stats["successful_operations"] += 1

                except Exception as e:
                    logger.error(f"Database write operation failed: {e}", exc_info=True)
                    future.set_exception(e)
                    self._stats["failed_operations"] += 1

                # Mark task as done
                my_queue.task_done()

            except asyncio.TimeoutError:
                # Timeout is normal - allows checking if we should continue running
                continue
            except Exception as e:
                logger.error(
                    f"Unexpected error in database write worker: {e}", exc_info=True
                )
                continue

        logger.info("Database write queue worker stopped")

    def get_stats(self) -> dict:
        """Get statistics about the write queue."""
        return {
            **self._stats,
            "current_queue_size": self.queue.qsize() if self.queue is not None else 0,
            "is_running": self.running,
        }

    def get_queue_size(self) -> int:
        """Get the current queue size."""
        return self.queue.qsize() if self.queue is not None else 0


# Global write queue instance
_global_write_queue: Optional[DatabaseWriteQueue] = None


def get_write_queue() -> DatabaseWriteQueue:
    """Get the global write queue instance."""
    global _global_write_queue
    if _global_write_queue is None:
        _global_write_queue = DatabaseWriteQueue()
    return _global_write_queue


async def execute_write_operation(operation: Callable[[], Awaitable[Any]]) -> Any:
    """
    Execute a database write operation through the global write queue.

    Args:
        operation: An async function that performs the database write

    Returns:
        The result of the write operation
    """
    queue = get_write_queue()
    return await queue.execute_write(operation)


async def db_write(operation_func: Callable[[], Awaitable[Any]]) -> Any:
    """
    Convenience function to execute database write operations through the queue.

    Args:
        operation_func: An async function that performs the database write

    Returns:
        The result of the write operation
    """
    return await execute_write_operation(operation_func)
