"""Domain layer: the physics and economics, independent of storage and transport.

Nothing in this package imports a database driver, an MQTT client, or a web
framework. That is what lets Phase 0 run the optimizer against CSV fixtures with
no infrastructure at all.
"""
