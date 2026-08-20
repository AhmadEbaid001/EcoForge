"""Optional outbound notification when a fault is detected (F11).

The technical review's alerting fix is three parts: a row in the `anomaly` table, a
red marker on the map, and "an optional outbound webhook - roughly five lines". The
first two shipped in Phase 2. This is the third, and it is more than five lines for
one reason: it must never be able to slow down or break ingestion.

What it replaces is the proposal's SMTP alerting service, which F11 cut: nobody
reads email during a presentation, and a mail server is a whole component whose only
demonstrable behaviour is one nobody would watch. This file is the entire outbound
notification story, and the rest of F11 is labelled at `gemp.api.main` (the collapse
to one process) and `gemp.sim.node` (WireGuard, described rather than built).

The ingester is a single-threaded loop. A synchronous POST inside it means a slow or
black-holed endpoint stalls the write path, and readings queue in memory until the
broker's redelivery makes it worse. So posting happens on a worker thread behind a
bounded queue, and when the queue is full notifications are DROPPED and counted
rather than buffered. Losing an alert is a nuisance; losing readings is data loss,
and the anomaly is in the database either way.

Configured by environment only - `GEMP_WEBHOOK_URL`. Unset means the whole path is
inert. It is deliberately not settable through the API: a notification target that
can be changed by whoever can reach the service is an exfiltration primitive.

    GEMP_WEBHOOK_URL=https://hooks.example.internal/gemp
    GEMP_WEBHOOK_MIN_SEVERITY=critical
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger("gemp.ingest.webhook")

# Ordered, so a minimum severity is a comparison rather than a set membership test.
SEVERITY_ORDER = {"medium": 0, "high": 1, "critical": 2}

# Deep enough to absorb a burst from one flush, shallow enough that a dead endpoint
# cannot cost meaningful memory. One flush is at most `ingest_batch_rows` readings and
# in practice a handful are anomalies.
QUEUE_DEPTH = 256

# A demonstration network is not the public internet; a hung connection here must not
# keep a worker thread parked for a minute.
TIMEOUT_S = 5.0


# urllib will happily open a `file://` URL, and `urlopen` on one reads the file.
# GEMP_WEBHOOK_URL is operator configuration rather than user input, so this is not
# a request-forgery hole - but a typo or a copied line that turns the notifier into
# a local file reader should fail at startup with a reason, not at the first anomaly
# with a stack trace in a worker thread.
ALLOWED_SCHEMES = frozenset({"http", "https"})


def _http_url_or_none(url: str | None) -> str:
    """The configured target if it is an http(s) URL with a host, else empty."""
    candidate = (url or "").strip()
    if not candidate:
        return ""

    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.netloc:
        log.error(
            "GEMP_WEBHOOK_URL is %r, which is not an http or https URL with a host. "
            "Notifications are disabled.", candidate,
        )
        return ""
    return candidate


@dataclass
class Notification:
    building_id: str
    ts: datetime
    observed_kw: float
    expected_kw: float
    robust_z: float
    severity: str
    kind: str

    def payload(self) -> dict:
        return {
            "event": "anomaly.detected",
            "building_id": self.building_id,
            "ts": self.ts.isoformat(),
            "observed_kw": round(self.observed_kw, 3),
            "expected_kw": round(self.expected_kw, 3),
            "robust_z": round(self.robust_z, 2),
            "severity": self.severity,
            "kind": self.kind,
        }


class Webhook:
    """Fire-and-forget notifier. Safe to construct when no URL is configured."""

    def __init__(self, url: str | None, min_severity: str = "critical"):
        self.url = _http_url_or_none(url)
        self.min_severity = SEVERITY_ORDER.get(min_severity.lower(), 2)
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.suppressed = 0

        self._queue: queue.Queue[Notification | None] = queue.Queue(maxsize=QUEUE_DEPTH)
        self._worker: threading.Thread | None = None
        if self.enabled:
            self._worker = threading.Thread(
                target=self._run, name="gemp-webhook", daemon=True
            )
            self._worker.start()
            log.info("webhook enabled, min severity %s", min_severity)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def notify(self, notification: Notification) -> None:
        """Queue one notification. Never raises, never blocks."""
        if not self.enabled:
            return
        if SEVERITY_ORDER.get(notification.severity, 0) < self.min_severity:
            self.suppressed += 1
            return

        try:
            self._queue.put_nowait(notification)
        except queue.Full:
            # The endpoint cannot keep up. Drop and count: the anomaly is already
            # stored, and blocking here would stall the ingester's write path.
            self.dropped += 1
            if self.dropped in (1, 10, 100):
                log.warning("webhook queue full, dropped %d notifications", self.dropped)

    def stop(self, timeout: float = 5.0) -> None:
        if self._worker is None:
            return
        self._queue.put(None)
        self._worker.join(timeout=timeout)

    # -- worker ---------------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._post(item)

    def _post(self, notification: Notification) -> None:
        body = json.dumps(notification.payload()).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "gemp/0.1"},
            method="POST",
        )
        try:
            # `_http_url_or_none` rejected everything that was not http(s)-with-a-host
            # before this notifier was ever constructed.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # nosec B310
                if 200 <= response.status < 300:
                    self.sent += 1
                    return
                self.failed += 1
                log.warning("webhook returned %s", response.status)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # No retry. A failing endpoint during a demonstration would otherwise
            # produce a retry storm competing with the work that matters.
            self.failed += 1
            if self.failed in (1, 10, 100):
                log.warning("webhook post failed (%d so far): %s", self.failed, exc)
