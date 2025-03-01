"""
psycopg connection pool base class and functionalities.
"""

# Copyright (C) 2021 The Psycopg Team

from random import random
from typing import Any, Callable, Deque, Dict, Generic, Optional
from typing import TYPE_CHECKING
from collections import Counter, deque

from psycopg.abc import ConnectionType

if TYPE_CHECKING:
    from typing import Counter as TCounter


class BasePool(Generic[ConnectionType]):

    # Used to generate pool names
    _num_pool = 0

    # Stats keys
    _POOL_MIN = "pool_min"
    _POOL_MAX = "pool_max"
    _POOL_SIZE = "pool_size"
    _POOL_AVAILABLE = "pool_available"
    _REQUESTS_WAITING = "requests_waiting"
    _REQUESTS_NUM = "requests_num"
    _REQUESTS_QUEUED = "requests_queued"
    _REQUESTS_WAIT_MS = "requests_wait_ms"
    _REQUESTS_ERRORS = "requests_errors"
    _USAGE_MS = "usage_ms"
    _RETURNS_BAD = "returns_bad"
    _CONNECTIONS_NUM = "connections_num"
    _CONNECTIONS_MS = "connections_ms"
    _CONNECTIONS_ERRORS = "connections_errors"
    _CONNECTIONS_LOST = "connections_lost"

    def __init__(
        self,
        conninfo: str = "",
        *,
        kwargs: Optional[Dict[str, Any]] = None,
        min_size: int = 4,
        max_size: Optional[int] = None,
        name: Optional[str] = None,
        timeout: float = 30.0,
        max_waiting: int = 0,
        max_lifetime: float = 60 * 60.0,
        max_idle: float = 10 * 60.0,
        reconnect_timeout: float = 5 * 60.0,
        reconnect_failed: Optional[
            Callable[["BasePool[ConnectionType]"], None]
        ] = None,
        num_workers: int = 3,
    ):
        if max_size is None:
            max_size = min_size
        if max_size < min_size:
            raise ValueError("max_size must be greater or equal than min_size")
        if not name:
            num = BasePool._num_pool = BasePool._num_pool + 1
            name = f"pool-{num}"

        if num_workers < 1:
            raise ValueError("num_workers must be at least 1")

        self.conninfo = conninfo
        self.kwargs: Dict[str, Any] = kwargs or {}
        self._reconnect_failed: Callable[["BasePool[ConnectionType]"], None]
        self._reconnect_failed = reconnect_failed or (lambda pool: None)
        self.name = name
        self._min_size = min_size
        self._max_size = max_size
        self.timeout = timeout
        self.max_waiting = max_waiting
        self.reconnect_timeout = reconnect_timeout
        self.max_lifetime = max_lifetime
        self.max_idle = max_idle
        self.num_workers = num_workers

        self._nconns = min_size  # currently in the pool, out, being prepared
        self._pool: Deque[ConnectionType] = deque()
        self._stats: "TCounter[str]" = Counter()

        # Min number of connections in the pool in a max_idle unit of time.
        # It is reset periodically by the ShrinkPool scheduled task.
        # It is used to shrink back the pool if maxcon > min_size and extra
        # connections have been acquired, if we notice that in the last
        # max_idle interval they weren't all used.
        self._nconns_min = min_size

        # Flag to allow the pool to grow only one connection at time. In case
        # of spike, if threads are allowed to grow in parallel and connection
        # time is slow, there won't be any thread available to return the
        # connections to the pool.
        self._growing = False

        # _close should be the last property to be set in the state
        # to avoid warning on __del__ in case __init__ fails.
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__module__}.{self.__class__.__name__}"
            f" {self.name!r} at 0x{id(self):x}>"
        )

    @property
    def min_size(self) -> int:
        return self._min_size

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def closed(self) -> bool:
        """`!True` if the pool is closed."""
        return self._closed

    def get_stats(self) -> Dict[str, int]:
        """
        Return current stats about the pool usage.
        """
        rv = dict(self._stats)
        rv.update(self._get_measures())
        return rv

    def pop_stats(self) -> Dict[str, int]:
        """
        Return current stats about the pool usage.

        After the call, all the counters are reset to zero.
        """
        stats, self._stats = self._stats, Counter()
        rv = dict(stats)
        rv.update(self._get_measures())
        return rv

    def _get_measures(self) -> Dict[str, int]:
        """
        Return immediate measures of the pool (not counters).
        """
        return {
            self._POOL_MIN: self._min_size,
            self._POOL_MAX: self._max_size,
            self._POOL_SIZE: self._nconns,
            self._POOL_AVAILABLE: len(self._pool),
        }

    @classmethod
    def _jitter(cls, value: float, min_pc: float, max_pc: float) -> float:
        """
        Add a random value to *value* between *min_pc* and *max_pc* percent.
        """
        return value * (1.0 + ((max_pc - min_pc) * random()) + min_pc)


class ConnectionAttempt:
    """Keep the state of a connection attempt."""

    INITIAL_DELAY = 1.0
    DELAY_JITTER = 0.1
    DELAY_BACKOFF = 2.0

    def __init__(self, *, reconnect_timeout: float):
        self.reconnect_timeout = reconnect_timeout
        self.delay = 0.0
        self.give_up_at = 0.0

    def update_delay(self, now: float) -> None:
        """Calculate how long to wait for a new connection attempt"""
        if self.delay == 0.0:
            self.give_up_at = now + self.reconnect_timeout
            self.delay = BasePool._jitter(
                self.INITIAL_DELAY, -self.DELAY_JITTER, self.DELAY_JITTER
            )
        else:
            self.delay *= self.DELAY_BACKOFF

        if self.delay + now > self.give_up_at:
            self.delay = max(0.0, self.give_up_at - now)

    def time_to_give_up(self, now: float) -> bool:
        """Return True if we are tired of trying to connect. Meh."""
        return self.give_up_at > 0.0 and now >= self.give_up_at
