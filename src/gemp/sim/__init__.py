"""Virtual sensor nodes and dataset replay.

Everything here publishes to the same MQTT topic in the same message shape a
physical ESP32 node would use, so the ingestion path never learns whether a reading
came from a simulation, a replayed public dataset, or real hardware.
"""
