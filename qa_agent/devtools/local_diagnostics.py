"""Read-only local hardware/Ollama metadata. Never installs or generates text."""
import csv
import ctypes
import hashlib
import io
import json
import os
import platform
import re
import shutil
# Operator-only read-only hardware diagnostics; no web/model dispatch.
import subprocess  # nosec B404
from pathlib import Path

from ..util import now, redact


def command(args, timeout=20):
    try:
        # Fixed internal hardware-command call sites; shell=False, bounded timeout.
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)  # nosec B603
        return {"ok": p.returncode == 0, "stdout": redact(p.stdout.strip()),
                "error": None if p.returncode == 0 else "command failed"}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "stdout": "", "error": type(e).__name__}


def nvidia_binary():
    candidates = [shutil.which("nvidia-smi"), r"C:\Windows\System32\nvidia-smi.exe",
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def memory_sample():
    """System memory only, not Ollama process memory; null on unsupported hosts."""
    if platform.system() != "Windows":
        return None
    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong) for n in ("total", "available", "page_total", "page_available",
                                            "virtual_total", "virtual_available", "extended")]
    s = Status(); s.length = ctypes.sizeof(s)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
        return None
    return {"total_bytes": s.total, "available_bytes": s.available, "load_percent": s.load}


def gpu_sample():
    binary = nvidia_binary()
    if not binary:
        return None
    r = command([binary, "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"], 5)
    if not r["ok"]:
        return {"error": r["error"]}
    # Preserve unavailable/N/A values rather than inventing zero utilization.
    return list(csv.reader(io.StringIO(r["stdout"])))


def hardware():
    info = {"observed_at": now(), "os": platform.platform(), "python": platform.python_version(),
            "system_memory": memory_sample(), "nvidia_sample_columns": ["name", "total_mib", "used_mib", "utilization_percent"],
            "nvidia": gpu_sample(), "dedicated_vram_note": "Win32 AdapterRAM is a driver-reported field, not reliable dedicated VRAM for integrated/WDDM adapters."}
    disk = shutil.disk_usage(Path.cwd())
    info["workspace_disk"] = {"total_bytes": disk.total, "free_bytes": disk.free}
    if platform.system() == "Windows":
        script = """[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$ErrorActionPreference='Stop'
@{cpu=@(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors);
video=@(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion)} | ConvertTo-Json -Depth 5 -Compress
"""
        r = command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], 30)
        try:
            info["windows"] = json.loads(r["stdout"]) if r["ok"] else {"error": r["error"]}
        except ValueError:
            info["windows"] = {"error": "invalid hardware output"}
    else:
        info["windows"] = {"error": "not the target Windows environment"}
    binary = nvidia_binary()
    banner = command([binary], 5)["stdout"] if binary else ""
    cuda = re.search(r"CUDA Version:\s*([\d.]+)", banner)
    info["driver_cuda_max_supported"] = cuda.group(1) if cuda else None
    info["cuda_note"] = "Driver maximum compatibility, not an installed CUDA toolkit measurement."
    nvcc = shutil.which("nvcc")
    info["cuda_toolkit"] = command([nvcc, "--version"], 5) if nvcc else None
    cli = shutil.which("ollama")
    if not cli:
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"
        cli = str(candidate) if candidate.is_file() else None
    info["ollama_cli"] = command([cli, "--version"], 5) if cli else {"ok": False, "error": "not found"}
    return info


def ollama_metadata(provider):
    """All requests use the existing audited native adapter and its allowlist."""
    result = {"observed_at": now(), "ready": False, "generation_performed": False,
              "provider": "ollama", "model": provider.model, "endpoint": provider.base_url}
    try:
        result["runtime_version"] = provider.request_metadata("GET", "/api/version").json().get("version")
        result["models"] = provider.discover()
        selected = next((m for m in result["models"] if m["id"] == provider.model), None)
        if not selected:
            raise ValueError("configured model is not installed; no download attempted")
        show = provider.request_metadata("POST", "/api/show", {"model": provider.model}).json()
        result["details"] = show.get("details")
        result["template_sha256"] = hashlib.sha256(show["template"].encode()).hexdigest() if isinstance(show.get("template"), str) else None
        result["model_parameters"] = show.get("parameters")
        result["capabilities"] = provider.inspect_model()
        result["memory"] = provider.memory()
        result["digest"] = selected.get("version")
        result["ready"] = bool(result["runtime_version"] and result["digest"])
        if not result["ready"]:
            result["error"] = "runtime version or installed-model digest unknown"
    except Exception as e:
        result["error"] = redact(f"{type(e).__name__}: {e}")[:500]
    return result
