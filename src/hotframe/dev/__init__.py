"""
dev — development-mode utilities for the Hub runtime.

``ModuleWatcher`` is a filesystem watcher (built on watchfiles/watchdog)
that monitors the modules directory for source changes and triggers
hot-reload of affected modules via ``ModuleRuntime.hot_reload``. Only
active in development (``Settings.DEBUG=True``); in production this
subpackage is a no-op.

Key exports::

    from hotframe.dev.autoreload import ModuleWatcher

Usage::

    if settings.DEBUG:
        watcher = ModuleWatcher(modules_dir, runtime)
        await watcher.start()
"""
