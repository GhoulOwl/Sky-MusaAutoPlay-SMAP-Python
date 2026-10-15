"""Sample a single inference process; unified-memory counters must not be added."""

from __future__ import annotations

import threading
import time


class RuntimeProfile:
    def __init__(self):
        import psutil
        import torch

        self.psutil, self.torch = psutil, torch
        self.process = psutil.Process()
        self.start = time.perf_counter()
        self.done = threading.Event()
        self.peak = dict(rss_bytes=0, mps_allocated_bytes=0, mps_driver_bytes=0)
        self.minimum_available = psutil.virtual_memory().available
        self.start_swap = psutil.swap_memory().used
        self.sample()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def sample(self):
        self.peak["rss_bytes"] = max(self.peak["rss_bytes"], self.process.memory_info().rss)
        self.minimum_available = min(self.minimum_available, self.psutil.virtual_memory().available)
        if self.torch.backends.mps.is_available():
            self.peak["mps_allocated_bytes"] = max(
                self.peak["mps_allocated_bytes"], self.torch.mps.current_allocated_memory()
            )
            self.peak["mps_driver_bytes"] = max(
                self.peak["mps_driver_bytes"], self.torch.mps.driver_allocated_memory()
            )

    def loop(self):
        while not self.done.wait(0.05):
            self.sample()

    def finish(self):
        self.done.set()
        self.thread.join(timeout=1)
        self.sample()
        return dict(
            resource_profile=dict(
                peak_sampled=self.peak,
                sample_interval_ms=50,
                minimum_system_available_bytes=self.minimum_available,
                system_swap_start_bytes=self.start_swap,
                system_swap_end_bytes=self.psutil.swap_memory().used,
                worker_seconds=time.perf_counter() - self.start,
                note="RSS and MPS driver allocations overlap unified memory; do not add them",
            )
        )
