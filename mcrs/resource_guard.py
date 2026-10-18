"""Low-frequency, fail-safe resource policy for background research work.

Sampling and policy are deliberately separated so tests can use deterministic
snapshots. This module never terminates another process or starts a busy loop.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import threading
import time
from typing import Callable
import sys


@dataclass(frozen=True)
class ResourceLimits:
    cpu_percent: float = 80.0
    cpu_sustain_seconds: float = 60.0
    min_available_ram_bytes: int = 2 * 1024**3
    max_system_memory_percent: float = 90.0
    max_process_rss_bytes: int = 2 * 1024**3
    max_process_growth_bytes: int = 512 * 1024**2

    def validate(self):
        if not 1 <= self.cpu_percent <= 100 or not 1 <= self.max_system_memory_percent <= 100:
            raise ValueError("Resource percentages must be between 1 and 100.")
        if not 1 <= self.cpu_sustain_seconds <= 3600:
            raise ValueError("CPU sustain duration must be between 1 second and one hour.")
        for value in (self.min_available_ram_bytes, self.max_process_rss_bytes,
                      self.max_process_growth_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Memory limits must be non-negative whole bytes.")


@dataclass(frozen=True)
class ResourceSnapshot:
    sampled_at: float
    system_cpu_percent: float | None = None
    available_ram_bytes: int | None = None
    system_memory_percent: float | None = None
    process_rss_bytes: int | None = None


@dataclass(frozen=True)
class ResourceDecision:
    allow_new_work: bool
    status: str
    reasons: tuple[str, ...] = ()
    snapshot: ResourceSnapshot | None = None


class ResourceGuard:
    """Evaluates new-work admission; active calls are allowed to finish."""

    def __init__(self, limits: ResourceLimits | None = None, *, clock: Callable[[], float] = time.monotonic,
                 min_sample_interval_seconds: float = 10.0):
        self.limits = limits or ResourceLimits()
        self.limits.validate()
        self._clock = clock
        if min_sample_interval_seconds < 5:
            raise ValueError("Resource sampling cadence must be at least five seconds.")
        self.min_sample_interval_seconds = float(min_sample_interval_seconds)
        self._cpu_high_since: float | None = None
        self._baseline_rss: int | None = None
        self._lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._last_sample_at: float | None = None
        self._last_decision: ResourceDecision | None = None

    def check(self, sampler: Callable[[], ResourceSnapshot] | None = None) -> ResourceDecision:
        """Sample at most once per configured interval; intervening reads reuse the result."""
        sampler = sampler or sample_resources
        now = float(self._clock())
        with self._sample_lock:
            if (self._last_decision is not None and self._last_sample_at is not None and
                    now - self._last_sample_at < self.min_sample_interval_seconds):
                return self._last_decision
            snapshot = sampler()
            self._last_sample_at = now
            self._last_decision = self.evaluate(snapshot)
            return self._last_decision

    def evaluate(self, snapshot: ResourceSnapshot) -> ResourceDecision:
        reasons = []
        now = float(snapshot.sampled_at)
        limits = self.limits
        with self._lock:
            cpu = snapshot.system_cpu_percent
            if cpu is not None and cpu >= limits.cpu_percent:
                if self._cpu_high_since is None:
                    self._cpu_high_since = now
                if now - self._cpu_high_since >= limits.cpu_sustain_seconds:
                    reasons.append("system_cpu_sustained")
            else:
                self._cpu_high_since = None

            rss = snapshot.process_rss_bytes
            if rss is not None:
                if self._baseline_rss is None:
                    self._baseline_rss = rss
                if rss >= limits.max_process_rss_bytes:
                    reasons.append("process_memory_high")
                if rss - self._baseline_rss >= limits.max_process_growth_bytes:
                    reasons.append("process_memory_growth")

        available = snapshot.available_ram_bytes
        if available is not None and available < limits.min_available_ram_bytes:
            reasons.append("system_ram_low")
        total_percent = snapshot.system_memory_percent
        if total_percent is not None and total_percent >= limits.max_system_memory_percent:
            reasons.append("system_memory_high")
        if reasons:
            status = "low_memory" if any(x in reasons for x in
                ("system_ram_low", "system_memory_high", "process_memory_high", "process_memory_growth")) else "busy"
            return ResourceDecision(False, status, tuple(dict.fromkeys(reasons)), snapshot)
        return ResourceDecision(True, "available", (), snapshot)

    def browser_allowed(self, snapshot: ResourceSnapshot) -> ResourceDecision:
        decision = self.evaluate(snapshot)
        if snapshot.system_memory_percent is not None and snapshot.system_memory_percent >= self.limits.max_system_memory_percent:
            return ResourceDecision(False, "low_memory", tuple(dict.fromkeys(decision.reasons + ("browser_memory_limit",))), snapshot)
        return decision


def sample_resources() -> ResourceSnapshot:
    """Best-effort host sample; unsupported measurements remain unknown, not fabricated."""
    now = time.monotonic()
    try:
        import psutil  # optional; absence must not break the core application
        cpu = float(psutil.cpu_percent(interval=None))
        memory = psutil.virtual_memory()
        process = psutil.Process(os.getpid())
        return ResourceSnapshot(now, cpu, int(memory.available), float(memory.percent),
                                int(process.memory_info().rss))
    except (ImportError, OSError, RuntimeError, AttributeError):
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]
                def value(self):
                    return (int(self.dwHighDateTime) << 32) | int(self.dwLowDateTime)

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetSystemTimes.argtypes = [ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
            kernel32.GetSystemTimes.restype = wintypes.BOOL
            kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
            kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
            cpu = None
            global _CPU_PREVIOUS, _CPU_LOCK
            if kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                current = (idle.value(), kernel.value(), user.value())
                with _CPU_LOCK:
                    previous = _CPU_PREVIOUS
                    _CPU_PREVIOUS = current
                if previous:
                    total = (current[1] - previous[1]) + (current[2] - previous[2])
                    idle_delta = current[0] - previous[0]
                    if total > 0:
                        cpu = max(0.0, min(100.0, 100.0 * (total - idle_delta) / total))

            memory = MEMORYSTATUSEX()
            memory.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                raise OSError("Global memory sample unavailable")
            process = PROCESS_MEMORY_COUNTERS()
            process.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            rss = None
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(process), process.cb):
                rss = int(process.WorkingSetSize)
            return ResourceSnapshot(now, cpu, int(memory.ullAvailPhys), float(memory.dwMemoryLoad), rss)
        except (OSError, AttributeError, TypeError, ValueError):
            return ResourceSnapshot(now)
    return ResourceSnapshot(now)


_CPU_PREVIOUS = None
_CPU_LOCK = threading.Lock()


class ResourceMonitor:
    """Opt-in sampler with a bounded cadence and explicit shutdown."""

    def __init__(self, guard: ResourceGuard | None = None, *, sampler=sample_resources,
                 interval_seconds: float = 15.0, on_decision=None):
        if interval_seconds < 5:
            raise ValueError("Resource sampling must be at least five seconds apart.")
        self.guard = guard or ResourceGuard()
        self.sampler = sampler
        self.interval_seconds = float(interval_seconds)
        self.on_decision = on_decision
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        with self._lifecycle_lock:
            if self.running:
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="zisu-resource-monitor", daemon=True)
            self._thread.start()
            return True

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            decision = self.guard.evaluate(self.sampler())
            if self.on_decision:
                self.on_decision(decision)

    def stop(self, timeout: float = 2.0):
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(max(0.0, timeout))
        with self._lifecycle_lock:
            if self._thread is thread and (not thread or not thread.is_alive()):
                self._thread = None
