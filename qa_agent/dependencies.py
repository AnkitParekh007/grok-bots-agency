"""Bind review/queue receipts to the declared lock and observed interpreter/packages."""
import importlib.metadata
import platform
import sys

from packaging.requirements import Requirement

from . import ROOT
from .util import sha256


def fingerprint():
    with (ROOT / "requirements.lock").open("rb") as source:
        data = source.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("dependency lock exceeds bounded metadata size")
    selected, matches = {}, True
    # Hashes and continuation lines are install metadata, not requirement names.
    for line in data.decode().splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        requirement = Requirement(line.rstrip().removesuffix("\\").strip())
        if requirement.url or len(requirement.specifier) != 1 or next(iter(requirement.specifier)).operator != "==" or "*" in next(iter(requirement.specifier)).version:
            raise ValueError("dependency lock must contain exact named versions")
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            installed = None
        selected[requirement.name] = installed
        if len(selected) > 256:
            raise ValueError("dependency inventory exceeds package count bound")
        matches = matches and installed is not None and requirement.specifier.contains(installed)
    if not selected:
        raise ValueError("dependency lock has no applicable packages")
    return {"lock_sha256": sha256(data), "python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": sys.platform, "packages": selected, "matches_lock": bool(matches)}
