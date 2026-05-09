"""
discovery — module filesystem scanning.

``scan`` walks a modules directory and returns a ``DiscoveryResult``
describing every found module (entry point, manifest, template dirs,
migration dirs).

Key exports::

    from hotframe.discovery.scanner import scan, DiscoveryResult

Usage::

    result = scan(Path("/app/modules"), module_id="sales")
"""

__all__: list[str] = []
