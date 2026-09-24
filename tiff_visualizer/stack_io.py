"""Loading of ImageJ/Fiji TIFF stacks into a normalized 5D (T, Z, C, Y, X) array.

The ImageJ hyperstack TIFF layout is the native format of this app: files
written by Fiji open here with their dimensions, LUTs and display ranges
intact, and anything we write back stays readable by Fiji.
"""

from __future__ import annotations

import os
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import tifffile

AXES_ORDER = "TZCYX"

# Every live stack, so a save can move all memory maps of the file it is
# about to overwrite into RAM first (see _detach_from).
_open_stacks: "weakref.WeakValueDictionary[int, TiffStack]" = weakref.WeakValueDictionary()

# Fiji's default channel colors for composites without embedded LUTs.
_DEFAULT_COLORS = [
    (1.0, 0.0, 0.0),  # red
    (0.0, 1.0, 0.0),  # green
    (0.0, 0.0, 1.0),  # blue
    (1.0, 1.0, 1.0),  # gray
    (0.0, 1.0, 1.0),  # cyan
    (1.0, 0.0, 1.0),  # magenta
    (1.0, 1.0, 0.0),  # yellow
]


def _ramp_lut(color: tuple[float, float, float]) -> np.ndarray:
    ramp = np.linspace(0.0, 255.0, 256)
    return (np.outer(ramp, color)).astype(np.uint8)  # (256, 3)


def _finite(plane: np.ndarray) -> np.ndarray:
    """The plane's values without NaN/inf, which Fiji leaves out of its
    statistics (a single NaN would otherwise make every range NaN)."""
    plane = np.asarray(plane)
    if np.issubdtype(plane.dtype, np.floating):
        return plane[np.isfinite(plane)]
    return plane


def _above(plane: np.ndarray, lo: float) -> float:
    """The smallest usable max over lo for a flat plane: one gray level for
    integers, a step scaled to the values for floats (0–1 data must not get
    a window reaching 1 above its minimum)."""
    if np.issubdtype(plane.dtype, np.floating):
        return lo + max(abs(lo) * 1e-3, 1e-6)
    return lo + 1


def auto_range(plane: np.ndarray) -> tuple[float, float]:
    """Fiji-style auto contrast: clip ~0.35% of pixels at each end."""
    plane = _finite(plane)
    if plane.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(plane, [0.35, 99.65])
    if hi <= lo:
        lo = float(plane.min())
        hi = float(max(plane.max(), _above(plane, lo)))
    return float(lo), float(hi)


def full_range(dtype: np.dtype, plane: np.ndarray | None = None) -> tuple[float, float]:
    """The 'Reset' display range: dtype limits for ints, data limits for floats."""
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        return float(info.min), float(info.max)
    if plane is not None:
        plane = _finite(plane)
        if plane.size:
            lo = float(plane.min())
            return lo, float(max(plane.max(), _above(plane, lo)))
    return 0.0, 1.0


@dataclass
class TiffStack:
    path: Path | None  # None for derived (unsaved) stacks such as projections
    name: str
    data: np.ndarray  # (T, Z, C, Y, X), possibly a memmap
    luts: np.ndarray  # (C, 256, 3) uint8
    ranges: np.ndarray  # (C, 2) float display min/max
    composite: bool  # whether to blend channels like Fiji's composite mode
    labels: list[str] | None  # per-page slice labels, ImageJ order (t, z, c)
    version: int = 0  # bumped on display-range edits; render caches key on it
    # ImageJ mode written while composite is off: the file's own "color" or
    # "grayscale", so a Fiji "color" hyperstack round-trips as one.
    plain_mode: str = "grayscale"

    def __post_init__(self):
        _open_stacks[id(self)] = self

    @property
    def n_frames(self) -> int:
        return self.data.shape[0]

    @property
    def n_slices(self) -> int:
        return self.data.shape[1]

    @property
    def n_channels(self) -> int:
        return self.data.shape[2]

    @property
    def shape_yx(self) -> tuple[int, int]:
        return self.data.shape[3], self.data.shape[4]

    @property
    def dtype(self) -> np.dtype:
        return self.data.dtype

    @property
    def nbytes(self) -> int:
        return self.data.nbytes

    @property
    def in_memory(self) -> bool:
        return not isinstance(self.data, np.memmap)

    def channel_color(self, c: int) -> tuple[int, int, int]:
        """Representative RGB color of a channel's LUT (its brightest entry)."""
        return tuple(int(v) for v in self.luts[c, 255])

    def label(self, t: int, z: int, c: int) -> str | None:
        if not self.labels:
            return None
        idx = (t * self.n_slices + z) * self.n_channels + c
        return self.labels[idx] if idx < len(self.labels) else None

    def plane(self, t: int, z: int, c: int) -> np.ndarray:
        return self.data[t, z, c]

    def values_at(
        self, t: int, z: int, y: int, x: int, mip: bool = False, method: str = "Max"
    ) -> np.ndarray:
        """Raw pixel values across channels at one position."""
        if mip:
            return project_block(np.asarray(self.data[t, :, :, y, x]), method)
        return np.asarray(self.data[t, z, :, y, x])

    def render(
        self,
        t: int,
        z: int,
        channels: Sequence[int],
        stride: int = 1,
        mip: bool = False,
        method: str = "Max",
    ) -> np.ndarray:
        """Render one displayed plane to RGB uint8 via display ranges + LUTs.

        stride > 1 renders every stride-th pixel — used when the pane shows
        the image small, so scrubbing many tiled stacks stays fast.
        mip renders a projection over z (method, one of PROJECTION_METHODS)
        instead of one slice.
        """
        h, w = self.shape_yx
        h = (h + stride - 1) // stride
        w = (w + stride - 1) // stride
        acc = np.zeros((h, w, 3), dtype=np.uint16)
        for ci in channels:
            lo, hi = self.ranges[ci]
            if mip:
                block = np.asarray(self.data[t, :, ci, ::stride, ::stride])
                img = project_block(block, method)
                if method == "Sum":
                    # Sums leave the slice's value range, so the display
                    # window grows with them: on screen a sum reads like the
                    # mean while the probe still reports the true totals.
                    lo, hi = lo * block.shape[0], hi * block.shape[0]
            else:
                img = self.data[t, z, ci][::stride, ::stride]
            if img.dtype == np.uint8 and lo == 0 and hi == 255:
                idx = img
            else:
                scaled = (img.astype(np.float32) - lo) * (255.0 / max(hi - lo, 1e-9))
                if np.issubdtype(img.dtype, np.floating):
                    # NaN has no uint8 value (the cast is platform-dependent):
                    # render it as the bottom of the range, like Fiji.
                    scaled = np.nan_to_num(scaled, copy=False, nan=0.0)
                idx = np.clip(scaled, 0, 255).astype(np.uint8)
            acc += self.luts[ci][idx]
        return np.clip(acc, 0, 255).astype(np.uint8)

    def save(self, path: str | Path):
        """Write as an ImageJ hyperstack TIFF that Fiji opens with LUTs/ranges intact."""
        path = Path(os.path.abspath(path))
        # Truncating a file under a live memory map destroys it: this stack
        # after an earlier Save As, or another copy of the same file.
        _detach_from(path)
        data = np.asarray(self.data)
        if data.dtype not in (np.uint8, np.uint16, np.float32):
            # The ImageJ format only supports these; float64/int32 etc. are demoted.
            data = data.astype(np.float32)
        metadata = {
            "axes": AXES_ORDER,
            # Channels shown one at a time appear in their LUT colors, which
            # is Fiji's "color" mode; a single channel keeps the file's mode.
            "mode": (
                "composite" if self.composite
                else "color" if self.n_channels > 1
                else self.plain_mode
            ),
            "Ranges": tuple(float(v) for v in self.ranges.ravel()),
            "LUTs": [np.ascontiguousarray(lut.T) for lut in self.luts],
        }
        if self.n_channels == 1:
            # Fiji keeps a single-channel display range in min=/max=.
            metadata["min"], metadata["max"] = (float(v) for v in self.ranges[0])
        if self.labels and len(self.labels) == self.n_frames * self.n_slices * self.n_channels:
            metadata["Labels"] = self.labels
        tifffile.imwrite(path, data, imagej=True, metadata=metadata)
        self.path = path
        self.name = path.name


def _detach_from(path: Path):
    """Copy into RAM every open stack whose memory map reads from path."""
    if not path.exists():
        return
    for stack in list(_open_stacks.values()):
        mapped = getattr(stack.data, "filename", None)
        if not isinstance(stack.data, np.memmap) or not mapped:
            continue
        try:
            same = os.path.samefile(mapped, path)
        except OSError:  # the mapped file is gone; nothing to protect
            continue
        if same:
            stack.data = np.array(stack.data)


def _normalize_axes(data: np.ndarray, axes: str) -> np.ndarray:
    """Reshape/transpose an arbitrary tifffile series into (T, Z, C, Y, X)."""
    axes = axes.upper()
    # RGB samples axis acts as channels when there is no separate C axis.
    if "S" in axes and "C" not in axes:
        axes = axes.replace("S", "C")
    # Fold unknown axes (e.g. 'I'/'Q' image sequences) into free slots.
    for i, ax in enumerate(axes):
        if ax not in AXES_ORDER:
            for target in "ZTC":
                if target not in axes:
                    axes = axes[:i] + target + axes[i + 1 :]
                    break
            else:
                raise ValueError(f"Cannot interpret TIFF axis {ax!r} (axes={axes})")
    for ax in AXES_ORDER:
        if ax not in axes:
            data = np.expand_dims(data, 0)
            axes = ax + axes
    return data.transpose([axes.index(ax) for ax in AXES_ORDER])


def needs_decode(path: str | Path) -> bool:
    """True if the file is compressed (cannot memmap; loading will be slow)."""
    with tifffile.TiffFile(path) as tf:
        return tf.pages[0].compression != 1  # 1 == COMPRESSION.NONE


def load_stack(path: str | Path) -> TiffStack:
    # Absolute, so a relative argv path still says where the file is — but
    # not symlink-resolved: a link keeps its own name in headers, labels and
    # export names. Code that asks "same file?" resolves or uses samefile.
    path = Path(os.path.abspath(path))
    with tifffile.TiffFile(path) as tf:
        series = tf.series[0]
        axes = series.axes
        ij = tf.imagej_metadata or {}
        page = tf.pages[0]
        palette = page.photometric == tifffile.PHOTOMETRIC.PALETTE
        colormap = page.colormap if palette else None
        try:
            # Uncompressed contiguous files (the normal Fiji case) are
            # memory-mapped so multi-GB stacks open instantly — read-only,
            # so read-only files open too and nothing can write through.
            data = tifffile.memmap(path, mode="r")
            data = data.reshape(series.shape)
        except (ValueError, OSError):
            data = series.asarray()
    rgb = "S" in axes.upper() and "C" not in axes.upper()
    data = _normalize_axes(data, axes)
    n_channels = data.shape[2]

    luts = _load_luts(ij, n_channels, colormap)
    ranges = _load_ranges(ij, data)
    if (
        n_channels == 1 and colormap is not None and ij.get("LUTs") is None
        and "Ranges" not in ij and "min" not in ij
    ):
        # A palette maps each pixel value to its own color: without a saved
        # display range, pin the window to the palette's domain so value v
        # shows entry v (the LUT samples that whole domain).
        ranges[0] = (0, colormap.shape[1] - 1)
    # RGB samples are red/green/blue channels, meant to be seen together.
    composite = (ij.get("mode") == "composite" or rgb) and n_channels > 1
    plain_mode = "color" if ij.get("mode") == "color" else "grayscale"
    labels = ij.get("Labels")
    return TiffStack(
        path, path.name, data, luts, ranges, composite, labels, plain_mode=plain_mode
    )


def _load_luts(ij: dict, n_channels: int, colormap: np.ndarray | None = None) -> np.ndarray:
    luts = np.empty((n_channels, 256, 3), dtype=np.uint8)
    embedded = ij.get("LUTs")
    if embedded is not None:
        embedded = np.asarray(embedded)
        if embedded.ndim == 2:  # tifffile hands back a lone LUT unwrapped
            embedded = embedded[None]
    for c in range(n_channels):
        if embedded is not None and c < len(embedded):
            luts[c] = np.asarray(embedded[c]).T  # (3, 256) -> (256, 3)
        elif n_channels == 1 and colormap is not None:
            luts[c] = _palette_lut(colormap)
        elif n_channels == 1:
            luts[c] = _ramp_lut((1.0, 1.0, 1.0))
        else:
            luts[c] = _ramp_lut(_DEFAULT_COLORS[c % len(_DEFAULT_COLORS)])
    return luts


def _palette_lut(colormap: np.ndarray) -> np.ndarray:
    """A TIFF ColorMap (3, 2**bits) of 16-bit entries — how 8-bit files carry
    a LUT — as a (256, 3) uint8 LUT."""
    cmap = np.asarray(colormap)
    if cmap.shape[1] != 256:  # e.g. a 16-bit palette: sample 256 entries
        cmap = cmap[:, np.linspace(0, cmap.shape[1] - 1, 256).astype(int)]
    if cmap.max() > 255:
        cmap = cmap >> 8
    return np.ascontiguousarray(cmap.T).astype(np.uint8)


def _load_ranges(ij: dict, data: np.ndarray) -> np.ndarray:
    n_channels = data.shape[2]
    embedded = ij.get("Ranges")
    if embedded is not None and len(embedded) >= 2 * n_channels:
        return np.asarray(embedded, dtype=np.float64).reshape(-1, 2)[:n_channels].copy()
    if n_channels == 1 and "min" in ij and "max" in ij:
        # Fiji's display range for a single-channel image.
        return np.array([[float(ij["min"]), float(ij["max"])]])
    if data.dtype == np.uint8:
        return np.tile([0.0, 255.0], (n_channels, 1))
    # No stored display range: auto-contrast each channel from a middle plane.
    ranges = np.empty((n_channels, 2))
    t_mid, z_mid = data.shape[0] // 2, data.shape[1] // 2
    for c in range(n_channels):
        ranges[c] = auto_range(np.asarray(data[t_mid, z_mid, c]))
    return ranges


# ---- projections -------------------------------------------------------

PROJECTION_METHODS = ["Max", "Min", "Mean", "Median", "Sum"]
_PROJ_PREFIX = {"Max": "MAX", "Min": "MIN", "Mean": "AVG", "Median": "MED", "Sum": "SUM"}
# Short names for the live z-projection toggle; "Max" keeps its familiar MIP.
PROJECTION_ABBREV = {"Max": "MIP", "Min": "MIN", "Mean": "AVG", "Median": "MED", "Sum": "SUM"}


def project_block(block: np.ndarray, method: str) -> np.ndarray:
    """Collapse block's leading axis by one of PROJECTION_METHODS.

    Max/Min/Median keep the input dtype (Fiji's convention), Mean and Sum
    promote to 32-bit float since their results leave it.
    """
    if method == "Max":
        return block.max(axis=0)
    if method == "Min":
        return block.min(axis=0)
    if method == "Mean":
        return block.mean(axis=0, dtype=np.float64).astype(np.float32)
    if method == "Median":
        med = np.median(block, axis=0)
        if np.issubdtype(block.dtype, np.integer):
            return np.round(med).astype(block.dtype)
        return med.astype(block.dtype)
    if method == "Sum":
        return block.sum(axis=0, dtype=np.float64).astype(np.float32)
    raise ValueError(f"Unknown projection method {method!r}")


def project(stack: TiffStack, axis: str, method: str, start: int, stop: int) -> TiffStack:
    """Project along 'Z' or 'T' over the inclusive [start, stop] range (0-based).

    Follows Fiji's Z Project conventions: Max/Min/Median keep the source
    dtype, Mean and Sum promote to 32-bit float, and the result is named
    MAX_/MIN_/AVG_/MED_/SUM_<name>.
    """
    ax = {"T": 0, "Z": 1}[axis]
    sub = np.asarray(stack.data[start : stop + 1] if ax == 0 else stack.data[:, start : stop + 1])
    arr = np.expand_dims(project_block(np.moveaxis(sub, ax, 0), method), ax)

    if arr.dtype == stack.dtype:
        ranges = stack.ranges.copy()
    else:  # dtype changed (Mean/Sum): recompute per-channel display ranges
        ranges = np.empty((stack.n_channels, 2))
        t_mid, z_mid = arr.shape[0] // 2, arr.shape[1] // 2
        for c in range(stack.n_channels):
            ranges[c] = auto_range(arr[t_mid, z_mid, c])

    name = f"{_PROJ_PREFIX[method]}_{stack.name}"
    return TiffStack(
        None, name, arr, stack.luts.copy(), ranges, stack.composite, None,
        plain_mode=stack.plain_mode,
    )
