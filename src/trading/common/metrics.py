from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Counter:
    value: float = 0.0

    def inc(self, n: float = 1.0) -> None:
        self.value += n


@dataclass
class Gauge:
    value: float = 0.0

    def set(self, v: float) -> None:
        self.value = v


@dataclass
class _Registry:
    counters: dict[tuple[str, tuple[tuple[str, str], ...]], Counter] = field(default_factory=dict)
    gauges: dict[tuple[str, tuple[tuple[str, str], ...]], Gauge] = field(default_factory=dict)
    help_texts: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        items = tuple(sorted((labels or {}).items()))
        return (name, items)

    def counter(self, name: str, help_text: str, labels: dict[str, str] | None = None) -> Counter:
        with self.lock:
            self.help_texts.setdefault(name, help_text)
            key = self._key(name, labels)
            if key not in self.counters:
                self.counters[key] = Counter()
            return self.counters[key]

    def gauge(self, name: str, help_text: str, labels: dict[str, str] | None = None) -> Gauge:
        with self.lock:
            self.help_texts.setdefault(name, help_text)
            key = self._key(name, labels)
            if key not in self.gauges:
                self.gauges[key] = Gauge()
            return self.gauges[key]

    def render(self) -> str:
        with self.lock:
            parts: list[str] = []
            emitted_help: set[str] = set()

            def _labels_fmt(items: tuple[tuple[str, str], ...]) -> str:
                if not items:
                    return ""
                inner = ",".join(f'{k}="{v}"' for k, v in items)
                return "{" + inner + "}"

            groups: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
            for (name, items), c in self.counters.items():
                groups[name].append(("counter", c.value, _labels_fmt(items)))
            for (name, items), g in self.gauges.items():
                groups[name].append(("gauge", g.value, _labels_fmt(items)))

            for name in sorted(groups):
                mtype = groups[name][0][0]
                if name not in emitted_help:
                    parts.append(f"# HELP {name} {self.help_texts.get(name, '')}")
                    parts.append(f"# TYPE {name} {mtype}")
                    emitted_help.add(name)
                for _, value, lbl in groups[name]:
                    parts.append(f"{name}{lbl} {value}")
            parts.append("")
            return "\n".join(parts)


REGISTRY = _Registry()


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/metrics", "/metrics/"):
            self.send_response(404)
            self.end_headers()
            return
        body = REGISTRY.render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def start_metrics_server(port: int, bind_host: str = "0.0.0.0") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind_host, port), _MetricsHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="metrics-server")
    t.start()
    time.sleep(0.05)
    return server
