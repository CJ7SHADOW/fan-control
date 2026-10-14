#!/usr/bin/python3
##
## fan-control.py
##
## Drives IPMI fan zones from CPU (and optionally HDD) temperatures using fan curves.
## Supports TrueNAS CORE / pfSense (FreeBSD) and Proxmox (Debian) on Supermicro X10
## and Dell iDRAC 8 boards. Settings live in config.ini next to this script and are
## re-read automatically whenever the file changes.
##
## Written to run on Python 3.6+ (TrueNAS CORE ships 3.9).

import ast
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from configparser import ConfigParser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.ini")

# rc.d / init tasks can start us with a minimal PATH; make sure ipmitool, smartctl,
# sysctl and sensors can be found on every supported OS.
os.environ["PATH"] = os.environ.get("PATH", "") + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REFRESH_SECONDS = 60       # re-send the current speed this often even if unchanged, in case the BMC reset it
ERROR_REPEAT_SECONDS = 300  # identical errors are logged at most once per this many seconds

VALID_OS = ("TrueNAS", "pfSense", "Proxmox")
VALID_PLATFORMS = ("SM_X10", "iDRAC_Gen08")
VALID_FOCUS = ("CPU", "Both")
VALID_FREQUENCY = ("Every", "On_Change", "On_Panic")

log = logging.getLogger("fan-control")


class ConfigError(Exception):
    pass


##
## Configuration
##

def config_mtime():
    return os.stat(CONFIG_PATH).st_mtime


def parse_curve(text, name):
    try:
        curve = ast.literal_eval(text)
        points = sorted((float(t), float(s)) for t, s in curve)
    except (ValueError, SyntaxError, TypeError) as exc:
        raise ConfigError("fan_curve '{}' is not a list of [temp, speed] pairs: {}".format(name, exc))
    if len(points) < 2:
        raise ConfigError("fan_curve '{}' needs at least two points".format(name))
    return points


def load_config():
    parser = ConfigParser()
    if not parser.read(CONFIG_PATH):
        raise ConfigError("cannot read {}".format(CONFIG_PATH))
    try:
        si = parser["system_info"]
        cfg = {
            "os": si["system_os"],
            "platform": si["ipmi_type"],
            "linked": si.getboolean("single_zone"),
            "focus": si["temp_focus"],
            "disks": list(ast.literal_eval(si["disks"])),
            # Supermicro zone numbers. Most boards: Zone 0 = FAN1-n (CPU/system), Zone 1 = FANA-x (peripheral).
            # Swap these if your board/chassis is wired the other way round.
            "cpu_zone": si.getint("cpu_zone", fallback=0),
            "hdd_zone": si.getint("hdd_zone", fallback=1),
            "cpu_curve": parse_curve(parser["fan_curve"]["cpu"], "cpu"),
            "hdd_curve": parse_curve(parser["fan_curve"]["hdd"], "hdd"),
            "min_speed": parser.getint("fan_limits", "min_speed", fallback=0),
            "max_speed": parser.getint("fan_limits", "max_speed", fallback=100),
            "hdd_max_temp": parser.getfloat("hdd_panic", "max_temp"),
            "panic_addition": parser.getint("hdd_panic", "panic_addition"),
            "cpu_timer": parser.getfloat("detect_timers", "cpu_timer"),
            "hdd_timer": parser.getfloat("detect_timers", "hdd_timer"),
            "cpu_average_seconds": parser.getfloat("smoothing", "cpu_average_seconds", fallback=10),
            "ramp_down_delay": parser.getfloat("smoothing", "ramp_down_delay", fallback=15),
            "ramp_down_step": parser.getint("smoothing", "ramp_down_step", fallback=5),
            "log_frequency": parser["log_config"]["frequency"],
        }
    except KeyError as exc:
        raise ConfigError("missing setting {} in {}".format(exc, CONFIG_PATH))
    except (ValueError, SyntaxError) as exc:
        raise ConfigError("bad value in {}: {}".format(CONFIG_PATH, exc))

    if cfg["os"] not in VALID_OS:
        raise ConfigError("system_os must be one of {}".format(", ".join(VALID_OS)))
    if cfg["platform"] not in VALID_PLATFORMS:
        raise ConfigError("ipmi_type must be one of {}".format(", ".join(VALID_PLATFORMS)))
    if cfg["focus"] not in VALID_FOCUS:
        raise ConfigError("temp_focus must be one of {}".format(", ".join(VALID_FOCUS)))
    if cfg["log_frequency"] not in VALID_FREQUENCY:
        raise ConfigError("frequency must be one of {}".format(", ".join(VALID_FREQUENCY)))
    if not 0 <= cfg["min_speed"] <= cfg["max_speed"] <= 100:
        raise ConfigError("fan_limits must satisfy 0 <= min_speed <= max_speed <= 100")
    if cfg["cpu_timer"] <= 0 or cfg["hdd_timer"] <= 0:
        raise ConfigError("detect_timers must be greater than 0")
    for key in ("cpu_zone", "hdd_zone"):
        if not 0 <= cfg[key] <= 255:
            raise ConfigError("{} must be a zone number between 0 and 255".format(key))
    if cfg["ramp_down_step"] < 1:
        raise ConfigError("ramp_down_step must be at least 1")
    if cfg["platform"] == "iDRAC_Gen08" and not cfg["linked"]:
        log.warning("Config: iDRAC only supports a single fan zone; treating single_zone as True")
        cfg["linked"] = True
    if not cfg["linked"] and cfg["cpu_zone"] == cfg["hdd_zone"]:
        raise ConfigError("cpu_zone and hdd_zone are the same zone; set single_zone = True instead")
    return cfg


def describe_config(cfg):
    lines = [
        "Config loaded from {}".format(CONFIG_PATH),
        "  System OS: {os}   Platform: {platform}   Linked zones: {linked}   Focus: {focus}".format(**cfg),
    ]
    if not cfg["linked"]:
        lines.append("  CPU zone: {cpu_zone}   HDD zone: {hdd_zone}".format(**cfg))
    lines += [
        "  Drives: {}".format(", ".join(cfg["disks"]) if cfg["focus"] == "Both" else "(not monitored, focus is CPU)"),
        "  CPU curve: {}".format(format_curve(cfg["cpu_curve"])),
        "  HDD curve: {}".format(format_curve(cfg["hdd_curve"])),
        "  Fan limits: {min_speed}-{max_speed}%   HDD panic: {hdd_max_temp:g}C (+{panic_addition}%)".format(**cfg),
        "  Timers: CPU every {cpu_timer:g}s, HDD every {hdd_timer:g}s".format(**cfg),
        "  Smoothing: CPU temp averaged over {cpu_average_seconds:g}s, ramp down after {ramp_down_delay:g}s"
        " in steps of {ramp_down_step}%".format(**cfg),
        "  Log frequency: {log_frequency}".format(**cfg),
    ]
    return "\n".join(lines)


def format_curve(curve):
    return " ".join("{:g}C={:g}%".format(t, s) for t, s in curve)


##
## Temperature sources
##

def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


def run_checked(cmd):
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError("{} failed (exit {}): {}".format(
            " ".join(cmd), result.returncode, (result.stderr or result.stdout).strip()[:300]))
    return result.stdout


def get_cpu_temp(op_sys):
    """Average temperature across all CPU cores, in C."""
    temps = []
    if op_sys == "Proxmox":
        data = json.loads(run_checked(["sensors", "-j"]))
        for chip, chip_data in data.items():
            if not isinstance(chip_data, dict):
                continue
            if chip.startswith("coretemp"):       # Intel: one entry per core
                wanted = [k for k in chip_data if k.startswith("Core ")]
            elif chip.startswith("k10temp"):      # AMD: per-CCD if present, otherwise Tctl
                wanted = [k for k in chip_data if k.startswith("Tccd")] or [k for k in chip_data if k in ("Tdie", "Tctl")][:1]
            else:
                continue
            for label in wanted:
                for key, value in chip_data[label].items():
                    if key.endswith("_input"):
                        temps.append(float(value))
    elif op_sys in ("TrueNAS", "pfSense"):
        output = run_checked(["sysctl", "-a", "dev.cpu"])
        temps = [float(t) for t in re.findall(r"^dev\.cpu\.\d+\.temperature:\s*(-?[\d.]+)C", output, re.M)]
    if not temps:
        raise RuntimeError("no CPU temperature readings found (FreeBSD: is the coretemp/amdtemp module loaded? "
                           "Linux: is lm-sensors installed?)")
    return sum(temps) / len(temps)


_smartctl_json = None  # None = not tested yet


def read_disk_temp(dev):
    """Return (temperature or None, state) where state is 'ok', 'standby' or an error message.

    Uses -n standby so sleeping drives are skipped rather than spun up.
    """
    global _smartctl_json
    path = dev if dev.startswith("/dev/") else "/dev/" + dev

    if _smartctl_json is not False:
        result = run(["smartctl", "-n", "standby", "-A", "-j", path])
        if "standby" in (result.stdout + result.stderr).lower() and result.returncode & 2:
            return None, "standby"
        try:
            data = json.loads(result.stdout)
            _smartctl_json = True
        except ValueError:
            data = None
            if _smartctl_json is None:  # first try failed to produce JSON: smartctl older than 7.0
                _smartctl_json = False
        if data is not None:
            temp = disk_temp_from_json(data)
            if temp is not None:
                return temp, "ok"
            messages = [m.get("string", "") for m in data.get("smartctl", {}).get("messages", [])]
            return None, "; ".join(m for m in messages if m) or "no temperature reported"

    result = run(["smartctl", "-n", "standby", "-A", path])
    text = result.stdout + result.stderr
    if "standby" in text.lower() and result.returncode & 2:
        return None, "standby"
    temp = disk_temp_from_text(result.stdout)
    if temp is not None:
        return temp, "ok"
    return None, (text.strip().splitlines() or ["no temperature reported"])[-1][:200]


def disk_temp_from_json(data):
    current = data.get("temperature", {}).get("current")
    if isinstance(current, (int, float)):
        return float(current)
    nvme = data.get("nvme_smart_health_information_log", {}).get("temperature")
    if isinstance(nvme, (int, float)):
        return float(nvme)
    for attr in data.get("ata_smart_attributes", {}).get("table", []):
        if attr.get("id") in (194, 190):
            raw = attr.get("raw", {})
            match = re.match(r"\s*(\d+)", str(raw.get("string", raw.get("value", ""))))
            if match:
                return float(match.group(1))
    return None


def disk_temp_from_text(text):
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 10 and fields[0] in ("194", "190"):
            match = re.match(r"(\d+)", fields[9])
            if match:
                return float(match.group(1))
    match = (re.search(r"Current Drive Temperature:\s+(\d+)\s*C", text)   # SAS
             or re.search(r"^Temperature:\s+(\d+)\s*Celsius", text, re.M))  # NVMe
    return float(match.group(1)) if match else None


def read_disks(disks):
    temps, standby, errors = {}, [], {}
    for dev in disks:
        temp, state = read_disk_temp(dev)
        if temp is not None:
            temps[dev] = temp
        elif state == "standby":
            standby.append(dev)
        else:
            errors[dev] = state
    return temps, standby, errors


##
## Fan curve math
##

def curve_speed(curve, temp):
    """Linear interpolation along the curve; holds the end values outside its range."""
    if temp <= curve[0][0]:
        return curve[0][1]
    for (t0, s0), (t1, s1) in zip(curve, curve[1:]):
        if temp <= t1:
            if t1 == t0:
                return s1
            return s0 + (temp - t0) * (s1 - s0) / (t1 - t0)
    return curve[-1][1]


def clamp_speed(speed, cfg):
    return int(round(min(max(speed, cfg["min_speed"]), cfg["max_speed"])))


class Smoother:
    """Raise fan speed immediately; lower it only after the target has stayed lower for
    ramp_down_delay seconds, and then by at most ramp_down_step percent per adjustment.
    Stops the fan hunting up and down on short temperature spikes."""

    def __init__(self):
        self.current = None
        self.lower_since = None

    def reset(self):
        self.current = None
        self.lower_since = None

    def update(self, target, now, cfg):
        if self.current is None or target >= self.current:
            self.current = target
            self.lower_since = None
        else:
            if self.lower_since is None:
                self.lower_since = now
            if now - self.lower_since >= cfg["ramp_down_delay"]:
                self.current = max(target, self.current - cfg["ramp_down_step"])
        return self.current


class RollingAverage:
    def __init__(self):
        self.samples = deque()

    def reset(self):
        self.samples.clear()

    def add(self, now, value, window):
        self.samples.append((now, value))
        while self.samples and now - self.samples[0][0] > window:
            self.samples.popleft()
        if not self.samples:  # window of 0 means no averaging
            return value
        return sum(v for _, v in self.samples) / len(self.samples)


##
## IPMI
##

class Fans:
    def __init__(self, platform):
        self.platform = platform

    def ipmi(self, *args):
        run_checked(["ipmitool", "raw"] + list(args))

    def take_control(self):
        if self.platform == "SM_X10":
            # "Full" mode: the BMC stops adjusting fans itself and holds whatever duty we set
            self.ipmi("0x30", "0x45", "0x01", "0x01")
        elif self.platform == "iDRAC_Gen08":
            # disable iDRAC automatic fan control
            self.ipmi("0x30", "0x30", "0x01", "0x00")
        time.sleep(2)

    def set_zone(self, zone, speed):
        if self.platform != "SM_X10":
            raise RuntimeError("per-zone control is only supported on SM_X10")
        self.ipmi("0x30", "0x70", "0x66", "0x01", "0x{:02x}".format(zone), "0x{:02x}".format(speed))

    def set_all(self, speed):
        if self.platform == "SM_X10":
            self.set_zone(0, speed)
            self.set_zone(1, speed)
        elif self.platform == "iDRAC_Gen08":
            self.ipmi("0x30", "0x30", "0x02", "0xff", "0x{:02x}".format(speed))

    def failsafe(self):
        """Put cooling back in the BMC's hands at a safe level after an error."""
        if self.platform == "SM_X10":
            self.ipmi("0x30", "0x45", "0x01", "0x01")  # Full mode resets every zone to 100%
        elif self.platform == "iDRAC_Gen08":
            self.ipmi("0x30", "0x30", "0x01", "0x01")  # re-enable iDRAC automatic control

    def release(self):
        """Return fan control to the BMC when the script stops."""
        if self.platform == "SM_X10":
            self.ipmi("0x30", "0x45", "0x01", "0x00")  # Standard mode: BMC's own curve
        elif self.platform == "iDRAC_Gen08":
            self.ipmi("0x30", "0x30", "0x01", "0x01")


##
## Main loop
##

class ErrorLimiter:
    def __init__(self):
        self.last = {}

    def should_log(self, key, now):
        if now - self.last.get(key, -1e9) >= ERROR_REPEAT_SECONDS:
            self.last[key] = now
            return True
        return False


def setup_logging():
    """Log settings are read once at startup (changing them needs a restart)."""
    parser = ConfigParser()
    try:
        parser.read(CONFIG_PATH)
        section = parser["log_config"]
        logging.basicConfig(format=section["format"], datefmt=section["date_format"],
                            filename=section["file_name"], level=logging.INFO)
    except Exception:
        logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)
        log.warning("Could not read log settings from %s; logging to stderr", CONFIG_PATH)


def handle_signal(signum, _frame):
    raise SystemExit("received signal {}".format(signum))


class Controller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.fans = Fans(cfg["platform"])
        self.mtime = config_mtime()
        self.cpu_avg = RollingAverage()
        self.smoothers = {}
        self.sent = {}           # target key -> (speed, time sent)
        self.hdd_target = None
        self.hdd_summary = ""
        self.next_hdd = 0.0
        self.have_control = False
        self.in_failsafe = False
        self.errors = ErrorLimiter()
        self.last_disk_problems = None

    def reset_state(self):
        self.cpu_avg.reset()
        self.smoothers = {}
        self.sent = {}
        self.hdd_target = None
        self.next_hdd = 0.0

    def reload_config_if_changed(self, now):
        try:
            mtime = config_mtime()
        except OSError as exc:
            if self.errors.should_log("config-stat", now):
                log.error("Config: cannot read %s (%s); keeping current settings", CONFIG_PATH, exc)
            return
        if mtime == self.mtime:
            return
        self.mtime = mtime
        try:
            cfg = load_config()
        except ConfigError as exc:
            log.error("Config: %s -- keeping previous settings until the file is fixed", exc)
            return
        if cfg["platform"] != self.cfg["platform"]:
            self.fans = Fans(cfg["platform"])
            self.have_control = False
        self.cfg = cfg
        self.reset_state()
        log.info(describe_config(cfg))

    def send(self, key, speed, now):
        last = self.sent.get(key)
        if last and last[0] == speed and now - last[1] < REFRESH_SECONDS:
            return False
        if key == "all":
            self.fans.set_all(speed)
        else:
            self.fans.set_zone(key, speed)
        changed = last is None or last[0] != speed
        self.sent[key] = (speed, now)
        return changed

    def smooth(self, key, target, now):
        return self.smoothers.setdefault(key, Smoother()).update(target, now, self.cfg)

    def check_disks(self, now):
        cfg = self.cfg
        temps, standby, errors = read_disks(cfg["disks"])
        problems = (tuple(sorted(errors.items())), tuple(standby))
        if errors and (problems != self.last_disk_problems or self.errors.should_log("disks", now)):
            for dev, message in sorted(errors.items()):
                log.warning("HDD: no temperature from %s (%s); skipping it", dev, message)
        if standby and problems != self.last_disk_problems:
            log.info("HDD: in standby, not woken: %s", ", ".join(standby))
        self.last_disk_problems = problems

        panic = False
        if temps:
            avg = sum(temps.values()) / len(temps)
            hottest = max(temps, key=temps.get)
            if temps[hottest] >= cfg["hdd_max_temp"]:
                panic = True
                target = curve_speed(cfg["hdd_curve"], temps[hottest]) + cfg["panic_addition"]
            else:
                target = curve_speed(cfg["hdd_curve"], avg)
            self.hdd_summary = "HDD Temp: Average = {:.1f}C, Max = {:g}C ({})".format(avg, temps[hottest], hottest)
            self.hdd_target = clamp_speed(target, cfg)
            if panic:
                log.warning("HDD Temp: %s is at %gC (limit %gC) -> HDD fans at least %d%%",
                            hottest, temps[hottest], cfg["hdd_max_temp"], self.hdd_target)
        elif errors:
            # could not read anything: hold the last known speed, or go to max if we never had one
            if self.hdd_target is None:
                self.hdd_target = cfg["max_speed"]
            self.hdd_summary = "HDD Temp: no readings"
        else:
            # every drive is spun down, so they are cool
            self.hdd_target = clamp_speed(min(s for _, s in cfg["hdd_curve"]), cfg)
            self.hdd_summary = "HDD Temp: all drives in standby"
        return panic

    def tick(self, now):
        cfg = self.cfg
        # read temperatures before touching the fans, so a failing sensor never leaves
        # the BMC in manual mode without a speed (it stays in failsafe instead)
        cpu_temp = get_cpu_temp(cfg["os"])
        cpu_avg = self.cpu_avg.add(now, cpu_temp, cfg["cpu_average_seconds"])
        cpu_target = clamp_speed(curve_speed(cfg["cpu_curve"], cpu_avg), cfg)

        hdd_checked = False
        if cfg["focus"] == "Both" and now >= self.next_hdd:
            self.check_disks(now)
            self.next_hdd = now + cfg["hdd_timer"]
            hdd_checked = True
        use_hdd = cfg["focus"] == "Both" and self.hdd_target is not None

        if not self.have_control:
            self.fans.take_control()
            self.have_control = True
            now = time.monotonic()

        cpu_line = "CPU Temp: {:.1f}C (avg {:.1f}C)".format(cpu_temp, cpu_avg)
        every = cfg["log_frequency"] == "Every"
        on_change = cfg["log_frequency"] == "On_Change"

        if cfg["linked"]:
            target = max(cpu_target, self.hdd_target) if use_hdd else cpu_target
            speed = self.smooth("all", target, now)
            changed = self.send("all", speed, now)
            source = "HDD" if use_hdd and self.hdd_target > cpu_target else "CPU"
            if every or (on_change and changed):
                parts = [cpu_line] + ([self.hdd_summary] if use_hdd else [])
                log.info("%s -> Fans: %d%% (driven by %s)", ", ".join(parts), speed, source)
        else:
            speed = self.smooth(cfg["cpu_zone"], cpu_target, now)
            changed = self.send(cfg["cpu_zone"], speed, now)
            if every or (on_change and changed):
                log.info("%s -> Zone %d fans: %d%%", cpu_line, cfg["cpu_zone"], speed)
            if use_hdd:
                hdd_speed = self.smooth(cfg["hdd_zone"], self.hdd_target, now)
                changed = self.send(cfg["hdd_zone"], hdd_speed, now)
                if (every and hdd_checked) or (on_change and changed):
                    log.info("%s -> Zone %d fans: %d%%", self.hdd_summary, cfg["hdd_zone"], hdd_speed)

    def go_failsafe(self, now, exc):
        key = "error:{}".format(exc)
        if self.errors.should_log(key, now):
            log.error("Error during control loop: %s", exc, exc_info=True)
        if not self.in_failsafe:
            log.warning("Failsafe: handing fans back to the BMC at full/automatic speed until readings recover")
            try:
                self.fans.failsafe()
            except Exception as fs_exc:
                log.error("Failsafe command failed too: %s", fs_exc)
            self.in_failsafe = True
            self.have_control = False
            self.reset_state()

    def run(self):
        next_tick = time.monotonic()
        while True:
            now = time.monotonic()
            try:
                self.reload_config_if_changed(now)
                self.tick(now)
                if self.in_failsafe:
                    log.info("Recovered from error; resuming fan control")
                    self.in_failsafe = False
            except Exception as exc:
                self.go_failsafe(now, exc)
            # schedule against the clock so slow smartctl runs don't stretch the interval
            next_tick += self.cfg["cpu_timer"]
            delay = next_tick - time.monotonic()
            if delay < 0:
                next_tick = time.monotonic()
                delay = 0
            time.sleep(delay)


def main():
    setup_logging()
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("Config: %s -- not starting", exc)
        print("fan-control: {}".format(exc), file=sys.stderr)
        return 1

    log.info("==== Fan control starting (assuming direct control) ====")
    log.info(describe_config(cfg))
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    controller = Controller(cfg)
    try:
        controller.run()
    except SystemExit as exc:
        log.info("Stopping: %s", exc)
    finally:
        try:
            controller.fans.release()
            log.info("Fan control handed back to the BMC")
        except Exception as exc:
            log.error("Could not hand fan control back to the BMC: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
