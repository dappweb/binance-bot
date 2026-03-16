"""Health Monitor - Monitors bot health and triggers auto-recovery."""

import asyncio
import time
from typing import Optional

from utils.logger import get_logger

logger = get_logger("health")


class HealthMonitor:
    """Monitors the health of bot components and triggers recovery."""

    def __init__(self):
        self._component_heartbeats: dict[str, float] = {}
        self._component_timeouts: dict[str, float] = {}
        self._alerts: list[dict] = []
        self._max_alerts = 100
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._recovery_callbacks: dict[str, list] = {}
        self._consecutive_failures: dict[str, int] = {}

    def register_component(
        self,
        name: str,
        timeout: float = 30.0,
        recovery_callback=None,
    ):
        """Register a component to monitor.

        Args:
            name: Component name
            timeout: Max seconds without heartbeat before alert
            recovery_callback: Async function to call for recovery
        """
        self._component_heartbeats[name] = time.monotonic()
        self._component_timeouts[name] = timeout
        self._consecutive_failures[name] = 0
        if recovery_callback:
            self._recovery_callbacks.setdefault(name, []).append(recovery_callback)
        logger.debug(f"Monitoring component: {name} (timeout: {timeout}s)")

    def heartbeat(self, name: str):
        """Record a heartbeat from a component."""
        self._component_heartbeats[name] = time.monotonic()
        self._consecutive_failures[name] = 0

    async def start(self, check_interval: float = 10.0):
        """Start the health check loop."""
        self._running = True
        self._check_task = asyncio.create_task(
            self._check_loop(check_interval)
        )
        logger.info("Health monitor started")

    async def stop(self):
        """Stop the health monitor."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        logger.info("Health monitor stopped")

    async def _check_loop(self, interval: float):
        """Periodic health check loop."""
        while self._running:
            try:
                await self._run_checks()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")
                await asyncio.sleep(interval)

    async def _run_checks(self):
        """Check all registered components."""
        now = time.monotonic()

        for name, last_hb in self._component_heartbeats.items():
            timeout = self._component_timeouts.get(name, 30.0)
            elapsed = now - last_hb

            if elapsed > timeout:
                self._consecutive_failures[name] = self._consecutive_failures.get(name, 0) + 1
                failures = self._consecutive_failures[name]

                self._add_alert(
                    "warning" if failures < 3 else "critical",
                    name,
                    f"No heartbeat for {elapsed:.0f}s (timeout: {timeout}s, "
                    f"consecutive failures: {failures})",
                )

                # Trigger recovery if available
                if name in self._recovery_callbacks:
                    for callback in self._recovery_callbacks[name]:
                        try:
                            logger.warning(
                                f"Triggering recovery for {name} "
                                f"(attempt #{failures})"
                            )
                            await callback()
                            # Reset heartbeat after recovery attempt
                            self._component_heartbeats[name] = time.monotonic()
                        except Exception as e:
                            logger.error(f"Recovery failed for {name}: {e}")

    def _add_alert(self, level: str, component: str, message: str):
        """Add an alert."""
        alert = {
            "time": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "component": component,
            "message": message,
        }
        self._alerts.append(alert)

        # Cap alerts list
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts:]

        if level == "critical":
            logger.critical(f"[{component}] {message}")
        else:
            logger.warning(f"[{component}] {message}")

    @property
    def is_healthy(self) -> bool:
        """Check if all components are healthy."""
        now = time.monotonic()
        for name, last_hb in self._component_heartbeats.items():
            timeout = self._component_timeouts.get(name, 30.0)
            if (now - last_hb) > timeout:
                return False
        return True

    @property
    def status(self) -> dict:
        """Get health status of all components."""
        now = time.monotonic()
        components = {}
        for name, last_hb in self._component_heartbeats.items():
            timeout = self._component_timeouts.get(name, 30.0)
            elapsed = now - last_hb
            components[name] = {
                "healthy": elapsed <= timeout,
                "last_heartbeat_ago": elapsed,
                "timeout": timeout,
                "consecutive_failures": self._consecutive_failures.get(name, 0),
            }
        return {
            "is_healthy": self.is_healthy,
            "components": components,
            "recent_alerts": self._alerts[-5:],
        }
