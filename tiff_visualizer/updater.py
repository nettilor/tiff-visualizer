"""Checks GitHub for a newer TIFF Visualizer and offers to fetch it.

Deliberately not an installer: the download lands in ~/Downloads and opens —
the mounted disk image or the unzipped app, ready to drag into Applications.
Replacing the running app from inside itself is the whole complexity of a real
update framework, taken on without its safety.

Plain urllib against the public GitHub API, no dependency, and quiet by design:
the automatic check runs at most once a day, says nothing when up to date, and
nothing at all offline — this app works offline and an update nag must never
suggest otherwise. It is the only network request the app ever makes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from . import __version__
from . import settings as app_settings

REPO = "nettilor/tiff-visualizer"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"

# "Once a day" with slack: launching every morning should count as daily
# instead of forever falling a few minutes short of a strict 24 hours.
CHECK_INTERVAL = timedelta(hours=20)
TIMEOUT = 15

_AUTO_KEY = "updates/auto"
_LAST_CHECK_KEY = "updates/lastCheck"
_SKIPPED_KEY = "updates/skippedTag"


# ---------------------------------------------------------------- versions

def parse_version(text: str | None) -> tuple[int, ...] | None:
    """A dotted release version — "1.2", "1.3.1", tag-style "v1.3" — as ints.

    Trailing zeros are dropped so "1.2" and "1.2.0" compare equal (otherwise a
    version the user skipped would be re-offered under its other spelling), and
    plain tuple comparison then orders 1.10 after 1.9. Anything that is not
    digits and dots returns None, and every caller treats that as "no update":
    a malformed tag must never produce an update alert.
    """
    if not text:
        return None
    s = text.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    if not s:
        return None
    parts: list[int] = []
    for piece in s.split("."):
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def display_version(tag: str) -> str:
    """"v1.3" reads as a git tag; the alert says "1.3", the way the app does."""
    s = tag.strip()
    return s[1:] if s[:1] in ("v", "V") else s


# ---------------------------------------------------------------- releases

@dataclass(frozen=True)
class Release:
    """The slice of GitHub's `releases/latest` answer the checker needs.

    Drafts and prereleases never appear at that endpoint, so a parsed release
    is a published one.
    """

    tag: str
    url: str
    body: str
    assets: tuple[tuple[str, str], ...]  # (file name, download URL)

    @classmethod
    def from_json(cls, data: dict) -> "Release | None":
        tag = data.get("tag_name")
        if not isinstance(tag, str) or not tag:
            return None
        assets = tuple(
            (a["name"], a["browser_download_url"])
            for a in data.get("assets") or []
            if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")
        )
        return cls(
            tag=tag,
            url=data.get("html_url") or RELEASES_PAGE,
            body=data.get("body") or "",
            assets=assets,
        )

    def asset_for_platform(self, system: str | None = None) -> tuple[str, str] | None:
        """The download to hand this machine, or None to open the release page.

        Names are the only signal a GitHub asset carries, so anything naming a
        *different* platform is refused outright — offering a Windows zip to a
        Mac is worse than offering nothing.
        """
        system = (system or sys.platform).lower()
        if system.startswith("darwin"):
            mine, theirs = ("mac", "osx", "darwin"), ("win", "linux")
            suffixes = (".dmg", ".pkg", ".zip", ".tar.gz")
        elif system.startswith("win"):
            mine, theirs = ("win",), ("mac", "osx", "darwin", "linux")
            suffixes = (".exe", ".msi", ".zip")
        else:
            mine, theirs = ("linux",), ("mac", "osx", "darwin", "win")
            suffixes = (".appimage", ".tar.gz", ".zip")

        def rank(name: str) -> tuple[int, int] | None:
            low = name.lower()
            if any(t in low for t in theirs):
                return None
            for i, suffix in enumerate(suffixes):
                if low.endswith(suffix):
                    # The suffix order above decides first (a .dmg is a better
                    # answer than any .zip), and a name that says "macos" only
                    # breaks ties between equals.
                    return (i, 0 if any(m in low for m in mine) else 1)
            return None

        ranked = [(r, a) for a in self.assets if (r := rank(a[0])) is not None]
        return min(ranked)[1] if ranked else None


def notes_excerpt(body: str, max_lines: int = 5, max_characters: int = 400) -> str:
    """The first few meaningful lines of the release notes, so the alert says
    what the update *is* — trimmed hard, because a message box is not a place
    to read markdown."""
    lines = [ln.strip() for ln in body.replace("\r", "").split("\n") if ln.strip()]
    if not lines:
        return ""
    excerpt = "\n".join(lines[:max_lines])
    truncated = len(lines) > max_lines
    if len(excerpt) > max_characters:
        excerpt = excerpt[:max_characters]
        truncated = True
    return excerpt + "…" if truncated else excerpt


# ------------------------------------------------------------------ policy

def auto_check_enabled() -> bool:
    return app_settings.settings().value(_AUTO_KEY, True, type=bool)


def set_auto_check_enabled(on: bool):
    app_settings.settings().setValue(_AUTO_KEY, bool(on))


def _last_check() -> datetime | None:
    raw = app_settings.settings().value(_LAST_CHECK_KEY, "", type=str)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def is_due(last_check: datetime | None, now: datetime | None = None) -> bool:
    if last_check is None:
        return True
    return (now or datetime.now()) - last_check > CHECK_INTERVAL


def verdict(release: Release, current: str | None, skipped_tag: str | None,
            manual: bool) -> str:
    """What a fetched release means for this copy: "update", "up-to-date" or
    "skipped" (newer, but the user said Skip This Version — only the automatic
    check honours that; asking by hand is asking to see it again)."""
    here, offered = parse_version(current), parse_version(release.tag)
    if here is None or offered is None or offered <= here:
        return "up-to-date"
    if not manual and parse_version(skipped_tag) == offered:
        return "skipped"
    return "update"


def free_destination(name: str, directory: Path | None = None) -> Path:
    """A spot in ~/Downloads for the file, stepping aside from anything already
    there Finder-style ("TIFF Visualizer 2.dmg") — an existing file is the
    user's, whatever it is, and is not this code's to overwrite."""
    if directory is None:
        directory = Path.home() / "Downloads"
        if not directory.is_dir():
            directory = Path.home()
    stem, dot, ext = name.rpartition(".")
    if not dot:  # no extension
        stem, ext = name, ""
    candidate = directory / name
    n = 2
    while candidate.exists():
        candidate = directory / (f"{stem} {n}" + (f".{ext}" if ext else ""))
        n += 1
    return candidate


# ----------------------------------------------------------------- network

def fetch_latest(url: str = LATEST_RELEASE_API, timeout: int = TIMEOUT) -> Release:
    """The one network call. Raises on anything that isn't a parsed release."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"TIFF-Visualizer/{__version__}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    release = Release.from_json(data)
    if release is None:
        raise ValueError("GitHub's answer carried no release tag")
    return release


def download(url: str, destination: Path, on_progress=None,
             cancelled: threading.Event | None = None) -> None:
    """Fetch to a sibling temp file, then rename — a half-written download must
    never be left sitting in Downloads looking like the real thing."""
    request = urllib.request.Request(
        url, headers={"User-Agent": f"TIFF-Visualizer/{__version__}"}
    )
    fd, tmp_path = tempfile.mkstemp(dir=str(destination.parent), suffix=".part")
    tmp = Path(tmp_path)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response, \
                os.fdopen(fd, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            seen = 0
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError("cancelled")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                seen += len(chunk)
                if on_progress is not None:
                    on_progress(seen, total)
        os.replace(tmp, destination)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def reveal_or_open(path: Path):
    """Hand the download to the system: a .dmg mounts and shows its
    drag-to-Applications window, a .zip unpacks beside itself."""
    if sys.platform.startswith("darwin"):
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform.startswith("win"):
        os.startfile(str(path))  # noqa: S606 - Windows' own opener
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def open_in_browser(url: str):
    import webbrowser

    webbrowser.open(url)


# --------------------------------------------------------------- the check

_pool: ThreadPoolExecutor | None = None
_bridge: "_Bridge | None" = None
_in_flight = False


class _Bridge(QObject):
    """Carries worker results back to the main thread (queued signals)."""

    checked = Signal(object, object, bool)  # release | None, error | None, manual
    progress = Signal(int, int)  # bytes so far, total (0 when unknown)
    downloaded = Signal(object, object)  # path | None, error | None


def _get_bridge() -> _Bridge:
    global _bridge
    if _bridge is None:
        _bridge = _Bridge()
        _bridge.checked.connect(_on_checked)
        _bridge.downloaded.connect(_on_downloaded)
    return _bridge


def _submit(work):
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="updater")
    _pool.submit(work)


def check_on_launch():
    """Only if enabled, only once a day, and held back a few seconds so its
    alert can never beat the window it would sit in front of."""
    if not auto_check_enabled() or not is_due(_last_check()):
        return
    QTimer.singleShot(3000, lambda: _check(manual=False))


def check_now():
    """The menu item. Always checks, and unlike the launch check it answers out
    loud: "you're up to date" and "GitHub didn't answer" are both real answers
    to a question the user actually asked."""
    _check(manual=True)


def _check(manual: bool):
    global _in_flight
    if _in_flight:
        return
    _in_flight = True
    # The attempt is what is stamped, not the success — a Mac that is offline
    # every morning should stay quiet, not retry on every launch.
    app_settings.settings().setValue(_LAST_CHECK_KEY, datetime.now().isoformat())
    bridge = _get_bridge()

    def work():
        try:
            bridge.checked.emit(fetch_latest(), None, manual)
        except Exception as exc:  # noqa: BLE001 - every failure is "no answer"
            bridge.checked.emit(None, exc, manual)

    _submit(work)


def _on_checked(release: Release | None, error: Exception | None, manual: bool):
    global _in_flight
    _in_flight = False
    if release is None:
        if manual:
            _say_check_failed(error)
        return
    result = verdict(
        release,
        __version__,
        app_settings.settings().value(_SKIPPED_KEY, "", type=str),
        manual,
    )
    if result == "update":
        _offer(release, manual)
    elif result == "up-to-date" and manual:
        _say_up_to_date()


# ------------------------------------------------------- talking to the user

def _parent():
    app = QApplication.instance()
    if app is None:
        return None
    return app.activeWindow()


def _box(icon, title: str, text: str, informative: str = "") -> QMessageBox:
    box = QMessageBox(_parent())
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    return box


def _offer(release: Release, manual: bool):
    asset = release.asset_for_platform()
    info = f"You're using {__version__}."
    notes = notes_excerpt(release.body)
    if notes:
        info += f"\n\n{notes}"
    if asset is not None:
        info += ("\n\nThe download lands in your Downloads folder and opens, "
                 "ready to drag into Applications.")
    box = _box(
        QMessageBox.Information,
        "Update available",
        f"TIFF Visualizer {display_version(release.tag)} is available",
        info,
    )
    get = box.addButton(
        "Open Release Page" if asset is None else "Download && Open",
        QMessageBox.AcceptRole,
    )
    later = box.addButton("Not Now", QMessageBox.RejectRole)
    skip = None if manual else box.addButton("Skip This Version", QMessageBox.DestructiveRole)
    box.setDefaultButton(get)
    box.setEscapeButton(later)
    box.exec()
    clicked = box.clickedButton()
    if clicked is get:
        if asset is None:
            open_in_browser(release.url)
        else:
            _download(release, asset)
    elif clicked is skip:
        app_settings.settings().setValue(_SKIPPED_KEY, release.tag)


_progress: QProgressDialog | None = None
_cancelled: threading.Event | None = None
_pending_asset: tuple[str, str] | None = None


def _download(release: Release, asset: tuple[str, str]):
    global _progress, _cancelled, _pending_asset
    name, url = asset
    _pending_asset = asset
    _cancelled = threading.Event()
    destination = free_destination(name)
    bridge = _get_bridge()

    _progress = QProgressDialog(
        f"Downloading TIFF Visualizer {display_version(release.tag)}…",
        "Cancel", 0, 0, _parent(),
    )
    _progress.setWindowTitle("Downloading update")
    _progress.setWindowModality(Qt.WindowModal)
    _progress.setMinimumDuration(0)
    _progress.setAutoClose(False)
    _progress.setAutoReset(False)
    _progress.canceled.connect(_cancelled.set)
    try:
        bridge.progress.connect(_on_progress, Qt.UniqueConnection)
    except RuntimeError:  # already connected from an earlier download
        pass
    _progress.show()

    cancelled = _cancelled

    def work():
        try:
            download(
                url,
                destination,
                on_progress=lambda seen, total: bridge.progress.emit(seen, total),
                cancelled=cancelled,
            )
            bridge.downloaded.emit(destination, None)
        except InterruptedError:
            bridge.downloaded.emit(None, None)
        except Exception as exc:  # noqa: BLE001 - reported to the user below
            bridge.downloaded.emit(None, exc)

    _submit(work)


def _on_progress(seen: int, total: int):
    if _progress is None or not total:
        return
    _progress.setMaximum(total)
    _progress.setValue(seen)


def _on_downloaded(path: Path | None, error: Exception | None):
    global _progress
    if _progress is not None:
        _progress.close()
        _progress = None
    if path is not None:
        reveal_or_open(path)
    elif error is not None:
        _say_download_failed(error)


def _say_up_to_date():
    _box(
        QMessageBox.Information,
        "Up to date",
        "You're up to date",
        f"TIFF Visualizer {__version__} is the latest version.",
    ).exec()


def _say_check_failed(error: Exception | None):
    box = _box(
        QMessageBox.Warning,
        "Update check",
        "The update check didn't reach GitHub",
        (f"{error}\n\n" if error else "") + "You can look at the releases page yourself.",
    )
    ok = box.addButton(QMessageBox.Ok)
    page = box.addButton("Open Releases Page", QMessageBox.AcceptRole)
    box.setDefaultButton(ok)
    box.exec()
    if box.clickedButton() is page:
        open_in_browser(RELEASES_PAGE)


def _say_download_failed(error: Exception):
    box = _box(
        QMessageBox.Warning,
        "Download failed",
        "The download didn't finish",
        f"{error}\n\nYour browser can fetch the same file instead.",
    )
    browser = box.addButton("Download in Browser", QMessageBox.AcceptRole)
    cancel = box.addButton("Cancel", QMessageBox.RejectRole)
    box.setDefaultButton(browser)
    box.setEscapeButton(cancel)
    box.exec()
    if box.clickedButton() is browser and _pending_asset is not None:
        open_in_browser(_pending_asset[1])
