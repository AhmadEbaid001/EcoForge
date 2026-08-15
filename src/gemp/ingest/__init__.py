"""Ingestion: MQTT in, signed rows out.

The MQTT topic is the hardware seam. A physical ESP32 with a clamp-on current
sensor publishes the same message shape to the same topic, and nothing downstream
changes - which is the proposal's claim in Section 2.6, made concrete.
"""
