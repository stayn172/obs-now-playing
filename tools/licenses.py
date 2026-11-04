"""Collect third-party license texts for a release folder.

Walks the runtime dependency tree of requirements.txt (with environment
markers evaluated for the build platform), copies each package's license
files into <out>/licenses/<package>/, and writes THIRD_PARTY_NOTICES.txt.
Bundled fonts already ship their OFL texts in web/assets/fonts.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from importlib import metadata

from packaging.requirements import Requirement

LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice|authors)([._-].*)?$", re.I)


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_distributions(requirements_file: str) -> list[metadata.Distribution]:
    """requirements.txt plus everything those packages require, for this OS."""
    todo: list[str] = []
    with open(requirements_file, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            req = Requirement(line)
            if req.marker is None or req.marker.evaluate():
                todo.append(req.name)
    seen: dict[str, metadata.Distribution] = {}
    while todo:
        name = _canon(todo.pop())
        if name in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[name] = dist
        for spec in dist.requires or []:
            req = Requirement(spec)
            # skip optional extras; keep platform-specific deps that apply here
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                todo.append(req.name)
    return sorted(seen.values(), key=lambda d: _canon(d.metadata["Name"]))


def license_name(dist: metadata.Distribution) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr
    lic = (md.get("License") or "").strip()
    if lic and len(lic) < 80 and "\n" not in lic:
        return lic
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    return ", ".join(classifiers) or "see license files"


def license_files(dist: metadata.Distribution) -> list[str]:
    out = []
    for f in dist.files or []:
        if LICENSE_FILE.match(f.name) and (".dist-info" in str(f) or "licenses" in str(f).lower()):
            p = str(dist.locate_file(f))
            if os.path.isfile(p):
                out.append(p)
    return out


def python_license() -> str | None:
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for p in (os.path.join(sys.base_prefix, "LICENSE.txt"),
              os.path.join(sys.base_prefix, "lib", ver, "LICENSE.txt")):
        if os.path.isfile(p):
            return p
    return None


def write_notices(out_dir: str, requirements_file: str) -> list[str]:
    lic_dir = os.path.join(out_dir, "licenses")
    shutil.rmtree(lic_dir, ignore_errors=True)
    os.makedirs(lic_dir)
    rows = [("Python", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", "PSF-2.0")]
    py = python_license()
    if py:
        os.makedirs(os.path.join(lic_dir, "python"))
        shutil.copy2(py, os.path.join(lic_dir, "python", "LICENSE.txt"))
    dists = runtime_distributions(requirements_file)
    try:  # the PyInstaller bootloader ships inside the executable
        dists.append(metadata.distribution("pyinstaller"))
    except metadata.PackageNotFoundError:
        pass
    for dist in dists:
        name = dist.metadata["Name"]
        rows.append((name, dist.version, license_name(dist)))
        files = license_files(dist)
        if files:
            d = os.path.join(lic_dir, _canon(name))
            os.makedirs(d, exist_ok=True)
            for p in files:
                shutil.copy2(p, os.path.join(d, os.path.basename(p)))
    width = max(len(r[0]) for r in rows) + 2
    vwidth = max(len(r[1]) for r in rows) + 2
    lines = [
        "Third-party software included in this release",
        "=" * 46,
        "",
        "Full license texts are in the licenses/ folder. Bundled fonts (Inter,",
        "Montserrat, Poppins, Roboto; SIL Open Font License 1.1) keep their texts in",
        "_internal/web/assets/fonts/.",
        "",
        "The PyInstaller bootloader is GPL-2.0 with an exception that allows",
        "distributing the resulting program under any license.",
        "",
        "LGPL components (e.g. pystray, python-xlib) are included unmodified; their",
        "source is available from PyPI, and this app's source (MIT) lets you rebuild",
        "it with modified versions of them.",
        "",
    ]
    lines += [f"{n:<{width}}{v:<{vwidth}}{l}" for n, v, l in rows]
    with open(os.path.join(out_dir, "THIRD_PARTY_NOTICES.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return [r[0] for r in rows]


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    names = write_notices(sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "dist", "obs-now-playing"),
                          os.path.join(root, "requirements.txt"))
    print("notices for:", ", ".join(names))
