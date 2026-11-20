# SPDX-License-Identifier: Apache-2.0
"""Install rez with rez's own install.py, using a uv-managed Python.

Sources:

* ``release``: a GitHub release. From 3.4.0 on, releases carry a source archive
  ``<tag>.tar.gz`` and a sigstore bundle with a GitHub build provenance
  attestation, signed by the release workflow. The archive is checked against
  the sha256 digest GitHub reports and, when a bundle exists, verified with
  sigstore. Older releases fall back to GitHub's generated tag tarball.
* ``git``: any ref (branch, tag, commit) of a GitHub repo, e.g. a fork/PR branch.
* ``path``: a local checkout, optionally installed editable.

install.py creates a venv from the interpreter that runs it, so the venv
references that interpreter. The interpreters therefore live inside the box
(``UV_PYTHON_INSTALL_DIR``) instead of the user's global uv store.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_REPO = "AcademySoftwareFoundation/rez"
SUPPORTED_PYTHONS = ("3.11", "3.12", "3.13")
DEFAULT_PYTHON = "3.11"
SIGSTORE_REQ = "sigstore==4.5.1"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"


class InstallError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[rezbox] {msg}", flush=True)


# -- locations -----------------------------------------------------------------

def platform_tag() -> str:
    """e.g. windows-amd64, linux-x86_64, darwin-arm64."""
    return f"{platform.system().lower()}-{platform.machine().lower()}"


@dataclass(frozen=True)
class Layout:
    home: Path  # .rezbox

    @property
    def downloads(self) -> Path:
        return self.home / "downloads"

    @property
    def sources(self) -> Path:
        return self.home / "src"

    @property
    def pythons(self) -> Path:
        return self.home / "python" / platform_tag()

    @property
    def installs(self) -> Path:
        return self.home / "rez" / platform_tag()

    def install_dir(self, label: str) -> Path:
        return self.installs / label


def bin_dir(install_dir: Path) -> Path:
    """Directory holding only the rez tools (what goes on PATH)."""
    scripts = "Scripts" if platform.system() == "Windows" else "bin"
    return install_dir / scripts / "rez"


# -- GitHub ----------------------------------------------------------------------

def _github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def github_api(path: str) -> Any:
    req = urllib.request.Request("https://api.github.com" + path)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "rez-in-a-box")
    token = _github_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise InstallError(f"GitHub: not found: {path}") from e
        if e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0":
            raise InstallError("GitHub API rate limit exceeded; set GITHUB_TOKEN") from e
        if e.code == 401:
            raise InstallError("GitHub rejected the token in GITHUB_TOKEN/GH_TOKEN") from e
        raise InstallError(f"GitHub API {path}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise InstallError(f"GitHub API {path}: {e.reason}") from e


def list_releases(repo: str, limit: int = 15) -> list[dict]:
    rels = github_api(f"/repos/{repo}/releases?per_page={min(limit, 100)}")
    out = []
    for r in rels[:limit]:
        names = {a["name"] for a in r.get("assets", [])}
        tag = r["tag_name"]
        out.append({
            "tag": tag,
            "published": (r.get("published_at") or "")[:10],
            "prerelease": r.get("prerelease", False),
            "archive": f"{tag}.tar.gz" in names,
            "signed": f"{tag}.tar.gz.sigstore.json" in names,
        })
    return out


def download(url: str, dest: Path) -> Path:
    """Download without credentials (release assets are public)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "rez-in-a-box"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f, 1024 * 1024)
    except (urllib.error.URLError, OSError) as e:
        tmp.unlink(missing_ok=True)
        raise InstallError(f"download failed: {url}: {e}") from e
    tmp.replace(dest)
    return dest


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract(archive: Path, dest: Path) -> Path:
    """Extract a source tarball, return the directory containing install.py."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(dest, filter="data")  # refuses absolute paths, .., device files
    for candidate in [dest, *sorted(p for p in dest.iterdir() if p.is_dir())]:
        if (candidate / "install.py").is_file():
            return candidate
    raise InstallError(f"no install.py found in {archive.name}")


# -- sigstore ----------------------------------------------------------------------

def attestation_info(bundle: Path) -> dict[str, Any]:
    """Pull subject digest, workflow and commit out of the DSSE payload (informational)."""
    try:
        b = json.loads(bundle.read_text())
        stmt = json.loads(base64.b64decode(b["dsseEnvelope"]["payload"]))
        pred = stmt.get("predicate", {})
        wf = pred.get("buildDefinition", {}).get("externalParameters", {}).get("workflow", {})
        deps = pred.get("buildDefinition", {}).get("resolvedDependencies", [])
        return {
            "subject_sha256": stmt["subject"][0]["digest"]["sha256"],
            "workflow": wf.get("path"),
            "workflow_ref": wf.get("ref"),
            "commit": next((d.get("digest", {}).get("gitCommit") for d in deps
                            if d.get("digest", {}).get("gitCommit")), None),
            "run": pred.get("runDetails", {}).get("metadata", {}).get("invocationId"),
        }
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return {}


def sigstore_verify(archive: Path, bundle: Path, repo: str, tag: str) -> dict[str, Any]:
    """Verify the provenance attestation with sigstore-python (run through uvx)."""
    info = attestation_info(bundle)
    workflow = os.environ.get("REZBOX_REZ_RELEASE_WORKFLOW") or info.get("workflow")
    if not workflow:
        raise InstallError(f"cannot determine signing workflow from {bundle.name}")
    identity = f"https://github.com/{repo}/{workflow}@refs/tags/{tag}"
    cmd = [uv_exe(), "tool", "run", "--quiet", "--from", SIGSTORE_REQ, "sigstore",
           "verify", "github", "--bundle", str(bundle), "--cert-identity", identity,
           "--repository", repo, "--ref", f"refs/tags/{tag}", str(archive)]
    log(f"verifying sigstore attestation (identity {identity})")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()[-2000:]
        raise InstallError(f"sigstore verification FAILED for {archive.name}:\n{detail}")
    info["identity"] = identity
    return info


# -- Python via uv ---------------------------------------------------------------

def uv_exe() -> str:
    exe = shutil.which("uv")
    if not exe:
        raise InstallError("uv not found on PATH (use the rezbox shims, they install it)")
    return exe


def ensure_python(layout: Layout, version: str) -> tuple[Path, str]:
    """Install (if needed) and locate a uv-managed interpreter inside the box."""
    env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(layout.pythons))
    layout.pythons.mkdir(parents=True, exist_ok=True)
    log(f"ensuring Python {version} (uv-managed, in {layout.pythons})")
    subprocess.run([uv_exe(), "python", "install", "--quiet", "--no-bin", "--no-registry",
                    "--no-config", version], env=env, check=True)
    res = subprocess.run([uv_exe(), "python", "find", "--managed-python", "--no-config",
                          "--no-project", version],
                         env=env, check=True, capture_output=True, text=True)
    exe = Path(res.stdout.strip())
    if layout.pythons.resolve() not in exe.resolve().parents:
        raise InstallError(f"uv picked an interpreter outside the box: {exe}")
    full = subprocess.run([str(exe), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
                          check=True, capture_output=True, text=True).stdout.strip()
    return exe, full


def declared_pythons(source: Path) -> list[str]:
    """Python versions a rez source tree declares support for (setup.py classifiers)."""
    try:
        text = (source / "setup.py").read_text(encoding="utf-8")
    except OSError:
        return []
    return re.findall(r"Programming Language :: Python :: (3\.\d+)", text)


# -- install -----------------------------------------------------------------------

@dataclass
class Source:
    kind: str                      # release | git | path
    repo: str = DEFAULT_REPO
    ref: str | None = None         # tag / git ref
    path: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    extract_dir: Path | None = None  # temporary source tree, removed after install


def resolve_release(layout: Layout, repo: str, tag: str, verify: bool) -> tuple[Path, Source]:
    rel = github_api(f"/repos/{repo}/releases/latest" if tag == "latest"
                     else f"/repos/{repo}/releases/tags/{tag}")
    tag = rel["tag_name"]
    assets = {a["name"]: a for a in rel.get("assets", [])}
    src = Source("release", repo=repo, ref=tag)
    dl = layout.downloads / repo.replace("/", "_") / tag
    archive_asset = assets.get(f"{tag}.tar.gz")

    if archive_asset:
        archive = dl / archive_asset["name"]
        if not archive.exists():
            log(f"downloading {archive_asset['browser_download_url']}")
            download(archive_asset["browser_download_url"], archive)
        digest = sha256_file(archive)
        expected = (archive_asset.get("digest") or "").removeprefix("sha256:")
        if expected and expected != digest:
            archive.unlink()
            raise InstallError(f"sha256 mismatch for {archive.name}: got {digest}, GitHub says {expected}")
        src.meta.update(url=archive_asset["browser_download_url"], sha256=digest,
                        sha256_matches_github=bool(expected))

        bundle_asset = assets.get(f"{tag}.tar.gz.sigstore.json")
        if not verify:
            log("WARNING: signature verification skipped (--no-verify)")
            src.meta["verified"] = False
        elif bundle_asset:
            bundle = dl / bundle_asset["name"]
            if not bundle.exists():
                download(bundle_asset["browser_download_url"], bundle)
            try:
                info = sigstore_verify(archive, bundle, repo, tag)
                if info.get("subject_sha256") and info["subject_sha256"] != digest:
                    raise InstallError("attestation subject digest does not match the archive")
            except InstallError:
                # never keep artifacts that failed verification around for reuse
                archive.unlink(missing_ok=True)
                bundle.unlink(missing_ok=True)
                raise
            src.meta.update(verified=True, attestation=info)
            log(f"signature OK (commit {info.get('commit') or '?'})")
        else:
            log(f"WARNING: release {tag} has no sigstore bundle, cannot verify")
            src.meta["verified"] = None
    else:
        # releases before 3.4.0 have no assets: use GitHub's generated tarball
        log(f"release {tag} has no source archive asset; using GitHub's tag tarball (unsigned)")
        archive = dl / f"{tag}-github-tarball.tar.gz"
        if not archive.exists():
            download(f"https://codeload.github.com/{repo}/tar.gz/refs/tags/{tag}", archive)
        src.meta.update(url=f"https://codeload.github.com/{repo}/tar.gz/refs/tags/{tag}",
                        sha256=sha256_file(archive), verified=None)
    src.meta["published"] = rel.get("published_at")
    src.meta["prerelease"] = rel.get("prerelease", False)
    src.extract_dir = layout.sources / repo.replace("/", "_") / tag
    return extract(archive, src.extract_dir), src


def resolve_git(layout: Layout, repo: str, ref: str) -> tuple[Path, Source]:
    commit = github_api(f"/repos/{repo}/commits/{ref}")["sha"]
    archive = layout.downloads / repo.replace("/", "_") / "git" / f"{commit}.tar.gz"
    url = f"https://codeload.github.com/{repo}/tar.gz/{commit}"
    if not archive.exists():
        log(f"downloading {repo}@{ref} ({commit[:12]})")
        download(url, archive)
    src = Source("git", repo=repo, ref=ref,
                 meta={"commit": commit, "url": url, "sha256": sha256_file(archive), "verified": None})
    src.extract_dir = layout.sources / repo.replace("/", "_") / "git" / commit
    return extract(archive, src.extract_dir), src


def default_label(src: Source, python: str) -> str:
    if src.kind == "release":
        base = src.ref
    elif src.kind == "git":
        base = f"git-{re.sub(r'[^A-Za-z0-9._-]+', '-', src.ref or '')}-{src.meta['commit'][:8]}"
    else:
        base = f"path-{src.path.name}"
    if src.repo != DEFAULT_REPO and src.kind != "path":
        base = f"{src.repo.split('/')[0]}-{base}"
    return f"{base}-py{python}"


def run_install_py(source_dir: Path, python_exe: Path, dest: Path, editable: bool) -> None:
    if " " in str(source_dir.resolve()) or " " in str(dest.resolve()):
        raise InstallError("rez's install.py does not support paths with spaces; "
                           "set REZBOX_HOME to a path without spaces")
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX")}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    cmd = [str(python_exe), "-E", "-s", "install.py"]
    if editable:
        cmd.append("--editable")
    cmd.append(str(dest))
    log(f"running install.py -> {dest}")
    res = subprocess.run(cmd, cwd=source_dir, env=env)
    if res.returncode != 0:
        raise InstallError(f"install.py failed with exit code {res.returncode}")


def install(layout: Layout, src_kind: str, *, ref: str | None = None, path: Path | None = None,
            repo: str = DEFAULT_REPO, python: str = DEFAULT_PYTHON, label: str | None = None,
            verify: bool = True, editable: bool = False, force: bool = False) -> dict[str, Any]:
    if python not in SUPPORTED_PYTHONS:
        raise InstallError(f"Python {python} not supported by rezbox (choose from {', '.join(SUPPORTED_PYTHONS)})")
    for p in (layout.home, path):
        if p is not None and " " in str(Path(p).resolve()):
            raise InstallError(f"rez's install.py does not support paths with spaces: {p}"
                               + ("; set REZBOX_HOME to a path without spaces" if p == layout.home else ""))

    if src_kind == "release":
        source_dir, src = resolve_release(layout, repo, ref or "latest", verify)
    elif src_kind == "git":
        source_dir, src = resolve_git(layout, repo, ref or "main")
    elif src_kind == "path":
        source_dir = Path(path).resolve()
        if not (source_dir / "install.py").is_file():
            raise InstallError(f"{source_dir} is not a rez checkout (no install.py)")
        src = Source("path", repo="", path=source_dir, meta={"verified": None, "editable": editable})
    else:
        raise InstallError(f"unknown source {src_kind}")
    if editable and src_kind != "path":
        raise InstallError("--editable only makes sense with --path")

    declared = declared_pythons(source_dir)
    if declared and python not in declared:
        log(f"WARNING: this rez source declares Python {', '.join(declared)}; {python} is not listed")

    label = label or default_label(src, python)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
        raise InstallError(f"invalid label {label!r} (use letters, digits, . _ -)")
    dest = layout.install_dir(label)
    if dest.exists():
        if not force:
            raise InstallError(f"install '{label}' already exists (use --force to reinstall)")
        shutil.rmtree(dest)

    python_exe, python_full = ensure_python(layout, python)
    t0 = time.time()
    try:
        run_install_py(source_dir, python_exe, dest, editable)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        if src.extract_dir is not None:  # archives stay cached in downloads/
            shutil.rmtree(src.extract_dir, ignore_errors=True)

    marker = bin_dir(dest) / ".rez_production_install"
    if not marker.is_file():
        raise InstallError(f"install.py finished but {marker} is missing")
    meta = {
        "label": label,
        "rez_version": marker.read_text().strip(),
        "python": python_full,
        "python_executable": str(python_exe),
        "platform": platform_tag(),
        "bin_dir": str(bin_dir(dest)),
        "source": {"kind": src.kind, "repo": src.repo, "ref": src.ref,
                   "path": str(src.path) if src.path else None, **src.meta},
        "declared_pythons": declared,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "install_seconds": round(time.time() - t0, 1),
    }
    (dest / "rezbox.json").write_text(json.dumps(meta, indent=2))
    return meta


def installed(layout: Layout) -> list[dict[str, Any]]:
    out = []
    if layout.installs.is_dir():
        for d in sorted(layout.installs.iterdir()):
            f = d / "rezbox.json"
            if f.is_file():
                try:
                    out.append(json.loads(f.read_text()))
                except ValueError:
                    pass
    return out


def remove(layout: Layout, label: str) -> None:
    dest = layout.install_dir(label)
    if not (dest / "rezbox.json").is_file():
        raise InstallError(f"no such install: {label}")
    shutil.rmtree(dest)
