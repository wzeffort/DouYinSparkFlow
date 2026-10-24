"""PostgreSQL coordination; local SQLite deployments retain their old behavior."""
from contextlib import contextmanager
from sqlalchemy import text

# Stable, application-specific advisory lock keys, shared by every node.
TRANSACTION_LOCK = 73194001
SCHEMA_LOCK = 73194002
BROWSER_LOCK = 73194003
NOTIFIER_LOCK = 73194004


@contextmanager
def exclusive(engine, key, *, wait=False):
    if engine.dialect.name != 'postgresql':
        yield None
        return
    # Never return a session-level locked connection to the pool.
    connection = engine.connect().execution_options(isolation_level='AUTOCOMMIT')
    acquired = False
    try:
        if wait:
            connection.execute(text('SELECT pg_advisory_lock(:key)'), {'key': key})
            acquired = True
        else:
            acquired = connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key})
        yield connection if acquired else False
    finally:
        if acquired:
            try:
                connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
            except Exception:
                connection.invalidate()
        connection.close()


class ClusterBrowserSlot:
    """Keep local process ownership plus cluster admission until cleanup finishes.

    A failed supervisor leaves a ten-minute safety delay. Disposable job
    controllers have a six-minute hard lifetime, so a disconnected node cannot
    continue automation while a replacement reconciles its uncertain runs.
    """
    def __init__(self, local, engine):
        self.local, self.engine = local, engine
        self.context = None
        self.connection = None

    @property
    def file(self):
        return self.local.file

    @property
    def quarantined(self):
        return self.local.quarantined

    @quarantined.setter
    def quarantined(self, value):
        self.local.quarantined = value

    def acquire(self):
        if not self.local.acquire():
            return False
        try:
            self.context = exclusive(self.engine, BROWSER_LOCK)
            self.connection = self.context.__enter__()
            if self.connection is False:
                self._close()
                return False
            if self.connection is not None:
                blocked = self.connection.scalar(text(
                    'SELECT blocked_until > CURRENT_TIMESTAMP FROM cluster_browser_guard WHERE id=1'))
                if blocked:
                    self._close()
                    return False
                self.connection.execute(text(
                    "UPDATE cluster_browser_guard SET blocked_until=CURRENT_TIMESTAMP + INTERVAL '10 minutes' WHERE id=1"))
            return True
        except Exception:
            self._close()
            raise

    def check(self):
        if self.connection is not None and self.connection is not False:
            # A heartbeat failure propagates into run_job_process's cleanup.
            self.connection.execute(text('SELECT 1'))

    def release(self):
        if self.quarantined:
            raise RuntimeError('cleanup incomplete: admission stays closed')
        try:
            if self.connection is not None and self.connection is not False:
                self.connection.execute(text('UPDATE cluster_browser_guard SET blocked_until=NULL WHERE id=1'))
        finally:
            self._close()

    def _close(self):
        try:
            if self.context is not None:
                self.context.__exit__(None, None, None)
        finally:
            self.context = self.connection = None
            self.local.release()
