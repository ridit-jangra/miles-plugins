import shutil
import time
from pathlib import Path

import psutil

from miles.watchers.inbox import Alert

COOLDOWN = 30 * 60


def hottest() -> float | None:
    try:
        readings = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    temps = [t.current for sensors in readings.values() for t in sensors if t.current]
    return max(temps) if temps else None


def uptime() -> str:
    seconds = int(time.time() - psutil.boot_time())
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return " ".join(p for p in (f"{days} days" if days else "", f"{hours} hours" if hours else "", f"{minutes} minutes") if p)


def snapshot() -> dict:
    usage = shutil.disk_usage(Path.home())
    status = {
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_free_percent": round(usage.free / usage.total * 100, 1),
        "disk_free_gb": round(usage.free / 1e9, 1),
        "uptime": uptime(),
    }
    battery = psutil.sensors_battery()
    if battery:
        status["battery_percent"] = round(battery.percent)
        status["charging"] = battery.power_plugged
    temp = hottest()
    if temp is not None:
        status["hottest_sensor_celsius"] = round(temp)
    return status


def register(api):
    battery_low = float(api.config.get("battery_low", 20))
    battery_critical = float(api.config.get("battery_critical", 10))
    disk_free_percent = float(api.config.get("disk_free_percent", 10))
    temp_high = float(api.config.get("temp_high", 90))
    memory_percent = float(api.config.get("memory_percent", 92))
    last: dict[str, float] = {}

    def due(key: str) -> bool:
        now = time.time()
        if now - last.get(key, 0) < COOLDOWN:
            return False
        last[key] = now
        return True

    @api.tool
    def system_status() -> dict:
        """Report how the computer is doing: CPU and memory use, free disk space, battery, temperature and uptime."""
        return snapshot()

    @api.watch(every="1m", first_run="after")
    def check() -> list[Alert]:
        alerts = []
        battery = psutil.sensors_battery()
        if battery and not battery.power_plugged:
            if battery.percent <= battery_critical and due("battery-critical"):
                alerts.append(Alert(f"Battery is critically low at {round(battery.percent)} percent", "sysmon", "urgent", "battery"))
            elif battery.percent <= battery_low and due("battery-low"):
                alerts.append(Alert(f"Battery is at {round(battery.percent)} percent and not charging", "sysmon", "normal", "battery"))
        usage = shutil.disk_usage(Path.home())
        free = usage.free / usage.total * 100
        if free <= disk_free_percent and due("disk"):
            alerts.append(Alert(f"Disk is almost full, only {free:.0f} percent ({usage.free / 1e9:.0f} GB) free", "sysmon", "normal", "disk"))
        temp = hottest()
        if temp is not None and temp >= temp_high and due("temp"):
            alerts.append(Alert(f"The computer is running hot, {temp:.0f} degrees", "sysmon", "urgent", "temp"))
        used = psutil.virtual_memory().percent
        if used >= memory_percent and due("memory"):
            alerts.append(Alert(f"Memory is nearly full, {used:.0f} percent in use", "sysmon", "normal", "memory"))
        return alerts
