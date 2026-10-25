"""Raster image conversion with Pillow.

Formats: PNG, JPEG, WebP, AVIF, GIF, BMP, TIFF, ICO. AVIF needs the libavif codecs in
the installed Pillow build (bundled in Pillow's Windows wheels: dav1d to read, aom to
write); avif_support() checks that both actually work, and AVIF routes are offered
only for what does.

Animation (GIF, WebP, APNG, AVIF sources): converted to GIF, WebP, PNG (as APNG) or
AVIF, every frame is kept with its duration, the loop count and transparency. Other
targets get the first frame and a warning that the animation was not kept. Very
large animations are refused instead of being loaded into memory.
"""

import base64
import functools
import io
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError, features

from fileconverter.core.converter import JobContext
from fileconverter.core.errors import InvalidInputError, OutputError
from fileconverter.core.models import ChoiceOption, IntOption, Route
from fileconverter.core.registry import FormatRegistry

logger = logging.getLogger(__name__)

# Our format id -> Pillow's format name.
PILLOW_FORMATS = {
    "png": "PNG",
    "jpeg": "JPEG",
    "webp": "WEBP",
    "avif": "AVIF",
    "gif": "GIF",
    "bmp": "BMP",
    "tiff": "TIFF",
    "ico": "ICO",
}

# Pixel modes each target can store as-is. Anything else is converted first.
# See https://pillow.readthedocs.io/en/stable/handbook/concepts.html#modes
SUPPORTED_MODES = {
    "png": {"1", "L", "LA", "P", "RGB", "RGBA", "I;16"},
    "jpeg": {"L", "RGB", "CMYK"},
    "webp": {"RGB", "RGBA"},
    "avif": {"RGB", "RGBA"},
    # Pillow reduces RGB(A) to a 256-colour palette with on/off transparency when saving.
    "gif": {"P", "L", "RGB", "RGBA"},
    "bmp": {"1", "L", "P", "RGB"},
    "tiff": {"1", "L", "LA", "P", "RGB", "RGBA", "CMYK", "I;16"},
    "ico": {"RGB", "RGBA"},
}

# Animation: sources whose frames can be read, targets that can store them (PNG as APNG).
ANIMATED_SOURCE_FORMATS = {"GIF", "WEBP", "PNG", "AVIF"}  # Pillow format names
ANIMATED_TARGETS = {"gif", "webp", "png", "avif"}
DEFAULT_FRAME_MS = 100  # what browsers use for frames without a duration
# Limits for loading every frame into memory (RGBA: 4 bytes per pixel, ~256 MB).
MAX_FRAMES = 2000
MAX_ANIMATION_PIXELS = 64_000_000

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)  # 256 x 256 is the largest ICO size
MAX_ICON_SIZE = ICON_SIZES[-1]
ALPHA_MODES = {"RGBA", "LA", "PA", "RGBa", "La"}
# 16-bit grayscale as Pillow opens it from PNG/TIFF.
HIGH_BIT_GRAY_MODES = {"I", "I;16", "I;16B", "I;16L"}
GRAYSCALE_MODES = {"1", "L", "LA", "La", "F"} | HIGH_BIT_GRAY_MODES
WHITE = (255, 255, 255)

TIFF_BITS_PER_SAMPLE = 258  # TIFF tag id
# PNG layout: 8-byte signature, then IHDR = length(4) type(4) width(4) height(4) bit depth(1).
PNG_BIT_DEPTH_OFFSET = 24

# Pillow advises against JPEG quality above 95: files grow, quality barely improves.
JPEG_QUALITY = IntOption("quality", "Quality", default=90, minimum=1, maximum=95)
WEBP_QUALITY = IntOption("quality", "Quality", default=90, minimum=1, maximum=100)
# Pillow's AVIF default; AVIF looks good at lower numbers than JPEG.
AVIF_QUALITY = IntOption("quality", "Quality", default=75, minimum=1, maximum=100)
QUALITY_OPTIONS = {"jpeg": JPEG_QUALITY, "webp": WEBP_QUALITY, "avif": AVIF_QUALITY}

# Quality presets: explicit encoder quality values per codec. The numbers differ
# because the codecs' quality scales are not comparable (AVIF 65 ~ JPEG 82 visually).
PRESET_QUALITY = {
    "jpeg": {"high": 92, "balanced": 82, "small": 65},
    "webp": {"high": 90, "balanced": 80, "small": 60},
    "avif": {"high": 85, "balanced": 65, "small": 45},
}
PRESET_LABELS = (
    ("custom", "Custom (use Quality)"),
    ("high", "High quality"),
    ("balanced", "Balanced"),
    ("small", "Smaller file"),
)


def _preset_option(codec: str) -> ChoiceOption:
    values = PRESET_QUALITY[codec]
    return ChoiceOption(
        "preset",
        "Preset",
        PRESET_LABELS,
        default="custom",  # keeps the previous behaviour: the Quality value decides
        help=(
            f"{codec.upper()} quality: High {values['high']}, Balanced {values['balanced']}, "
            f"Smaller file {values['small']}. Choosing a preset sets Quality; changing "
            "Quality yourself switches back to Custom."
        ),
        implies=tuple((name, (("quality", quality),)) for name, quality in values.items()),
    )


PRESET_OPTIONS = {codec: _preset_option(codec) for codec in PRESET_QUALITY}

NOTE_NO_ALPHA = "Transparent areas will be filled with white."
NOTE_LOSSY = "Lossy format: some image detail is lost."
NOTE_FIRST_FRAME = (
    "Only the first frame or page of an animated or multi-page image is converted; "
    "the animation is not kept."
)
NOTE_ANIMATION = "Animations keep every frame, the frame timing, looping and transparency."
NOTE_GIF = "GIF stores at most 256 colours and only fully transparent or opaque pixels."
NOTE_ICO = (
    "Icons are square: the image is centred on a transparent square (never stretched), "
    "limited to 256 x 256, and saved in the standard sizes from 16 x 16 up."
)
NOTE_NO_METADATA = (
    "Metadata (EXIF, GPS location, camera info) is removed unless 'Preserve metadata' is on."
)
# Formats whose Pillow writer stores EXIF / XMP / ICC passed to save() (verified by tests).
EXIF_TARGETS = {"jpeg", "png", "webp", "tiff", "avif"}
XMP_TARGETS = {"jpeg", "webp", "avif"}  # Pillow's PNG and TIFF writers do not store XMP
ICC_TARGETS = {"jpeg", "png", "webp", "tiff", "avif"}
EXIF_ORIENTATION = 0x0112
EXIF_SUB_IFDS = (0x8769, 0x8825, 0xA005)  # Exif, GPS and Interoperability blocks
_TRANSPOSE_FOR_ORIENTATION = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}

# A 1 x 1 AVIF made with libavif, to test decoding even where encoding is unavailable.
_AVIF_SAMPLE = (
    "AAAAIGZ0eXBhdmlmAAAAAGF2aWZtaWYxbWlhZk1BMUIAAADrbWV0YQAAAAAAAAAhaGRscgAAAAAAAAAAcGljdAAAAAAA"
    "AAAAAAAAAAAAAAAOcGl0bQAAAAAAAQAAAB5pbG9jAAAAAEQAAAEAAQAAAAEAAAETAAAAJAAAAChpaW5mAAAAAAABAAAA"
    "GmluZmUCAAAAAAEAAGF2MDFDb2xvcgAAAABqaXBycAAAAEtpcGNvAAAAFGlzcGUAAAAAAAAAAQAAAAEAAAAQcGl4aQAA"
    "AAADCAgIAAAADGF2MUOBAAwAAAAAE2NvbHJuY2x4AAEADQAGgAAAABdpcG1hAAAAAAAAAAEAAQQBAoMEAAAALG1kYXQS"
    "AAoIGAAGiAhoNCAyFhlHh4Yhh5555oAAAJBAyRxhQmK+L0A="
)


@functools.cache  # the installed Pillow cannot change while the program runs
def avif_support() -> tuple[bool, bool]:
    """(can read, can write) AVIF with the installed Pillow, checked by really doing it."""
    if not features.check("avif"):
        return False, False
    try:
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, "AVIF")
        sample, can_write = buffer.getvalue(), True
    except Exception:  # e.g. a libavif built without an encoder
        logger.info("AVIF encoding is not available", exc_info=True)
        sample, can_write = base64.b64decode(_AVIF_SAMPLE), False
    try:
        with Image.open(io.BytesIO(sample)) as image:
            image.load()
        can_read = True
    except Exception:
        logger.info("AVIF decoding is not available", exc_info=True)
        can_read = False
    return can_read, can_write


def _avif_problem(can_read: bool, can_write: bool) -> str | None:
    if can_read and can_write:
        return None
    if not can_read and not can_write:
        return (
            "AVIF is not available: the installed Pillow has no working AVIF support "
            "(libavif). Update Pillow (pip install --upgrade pillow)."
        )
    if can_read:
        return "The installed Pillow cannot write AVIF (it has no AVIF encoder); it can read it."
    return "The installed Pillow cannot read AVIF (it has no AVIF decoder); it can write it."


class ImageConverter:
    def __init__(self, avif_read: bool = True, avif_write: bool = True) -> None:
        self.readable = [f for f in PILLOW_FORMATS if f != "avif" or avif_read]
        self.writable = [f for f in PILLOW_FORMATS if f != "avif" or avif_write]
        # format id -> why it is (partly) unavailable
        self.unavailable: dict[str, str] = {}
        if problem := _avif_problem(avif_read, avif_write):
            self.unavailable["avif"] = problem

    def routes(self) -> Iterator[Route]:
        for source in self.readable:
            for target in self.writable:
                if source != target:
                    yield Route(source, target, _options_for(target), _notes_for(source, target))

    def convert(
        self,
        source: Path,
        destination: Path,
        route: Route,
        options: Mapping[str, int],
        ctx: JobContext,
    ) -> None:
        if route.target in ANIMATED_TARGETS and _is_animated(source):
            self._convert_animation(source, destination, route.target, options, ctx)
            return

        image = load_first_frame(source, ctx)
        ctx.raise_if_cancelled()

        original_mode = image.mode
        # Take metadata out of image.info and clear the rest: some Pillow writers embed
        # info values (ICC profile, comments, ...) automatically. Only what is passed to
        # save() explicitly below ends up in the output. EXIF and XMP here no longer
        # contain an orientation: load_first_frame() already rotated the pixels.
        icc_profile = image.info.pop("icc_profile", None)
        exif = _clean_exif(image.info.pop("exif", None))
        xmp = image.info.pop("xmp", None)
        image.info = {}
        image = prepare_for_target(image, route.target, ctx)
        if _color_family(image.mode) != _color_family(original_mode):
            icc_profile = None  # a profile for one color space is wrong for another
        if not ctx.preserve_metadata:
            icc_profile = exif = xmp = None
        ctx.raise_if_cancelled()

        pillow_format = PILLOW_FORMATS[route.target]
        save_args = _save_args(route.target, options, icc_profile, exif, xmp)
        if route.target == "tiff" and exif is not None and _has_sub_ifds(exif):
            # libtiff (used for LZW) cannot write EXIF sub-blocks such as GPS; Pillow's own
            # TIFF writer can, but only without compression.
            save_args["compression"] = None
            ctx.warn("The TIFF file was saved uncompressed so that its EXIF/GPS data is kept.")
        if route.target == "ico":
            image, save_args["sizes"] = _fit_icon(image, ctx)
        if route.target == "gif":
            _warn_partial_transparency([image], ctx)
        try:
            image.save(destination, format=pillow_format, **save_args)
        except OSError as exc:
            raise OutputError(f"Could not write {pillow_format} file: {exc}") from exc

        _verify_output(destination, pillow_format)

    def _convert_animation(
        self,
        source: Path,
        destination: Path,
        target: str,
        options: Mapping[str, int],
        ctx: JobContext,
    ) -> None:
        animation = _read_animation(source, ctx)
        if ctx.preserve_metadata and animation.had_metadata:
            ctx.warn("Metadata is not kept when converting animations.")
        if animation.plays is None:
            ctx.warn(
                "The source does not say how often the animation repeats; it now loops forever."
            )
        plays = 0 if animation.plays is None else animation.plays
        if target == "avif" and plays != 0:
            ctx.warn(
                "The loop count was written to the AVIF file, but cannot be verified; some "
                "viewers loop AVIF animations forever."
            )
        if target == "gif":
            _warn_partial_transparency(animation.frames, ctx)
        ctx.raise_if_cancelled()

        save_args = _save_args(target, options, None, None, None)
        save_args.update(
            save_all=True,
            append_images=animation.frames[1:],
            duration=animation.durations,
            **_loop_args(target, plays),
        )
        if target == "gif":
            # Every frame is a complete picture: clear the canvas before drawing the next,
            # so transparent areas never show the previous frame through.
            save_args["disposal"] = 2
        if target == "webp" and _has_transparency(animation.frames):
            # libwebp's animation encoder may crop frames to their opaque part and then
            # write the file without an alpha channel, so transparency silently becomes
            # black. Encoding every frame as a key frame keeps it (larger file).
            save_args.update(kmin=0, kmax=1)
        pillow_format = PILLOW_FORMATS[target]
        try:
            animation.frames[0].save(destination, format=pillow_format, **save_args)
        except OSError as exc:
            raise OutputError(f"Could not write {pillow_format} animation: {exc}") from exc
        _verify_output(destination, pillow_format)


def register_images(registry: FormatRegistry) -> ImageConverter:
    """Add image routes for what this Pillow build can really do; explain the rest."""
    converter = ImageConverter(*avif_support())
    registry.register_converter(converter)
    for format_id, reason in converter.unavailable.items():
        registry.note_unavailable(format_id, reason)
    return converter


def _options_for(target: str) -> tuple[ChoiceOption | IntOption, ...]:
    # Lossless or palette formats (PNG, GIF, BMP, TIFF, ICO) get neither option.
    if target not in QUALITY_OPTIONS:
        return ()
    return (PRESET_OPTIONS[target], QUALITY_OPTIONS[target])


def _notes_for(source: str, target: str) -> tuple[str, ...]:
    notes = []
    if not _supports_alpha(target):
        notes.append(NOTE_NO_ALPHA)
    if target in ("jpeg", "webp", "avif"):
        notes.append(NOTE_LOSSY)
    if target == "gif":
        notes.append(NOTE_GIF)
    if target == "ico":
        notes.append(NOTE_ICO)
    # Sources that often hold several frames or pages (APNG is rare, so PNG is left out).
    if source in ("gif", "webp", "avif", "tiff"):
        animated = source != "tiff" and target in ANIMATED_TARGETS
        notes.append(NOTE_ANIMATION if animated else NOTE_FIRST_FRAME)
    notes.append(NOTE_NO_METADATA)
    return tuple(notes)


# --- animation ------------------------------------------------------------------


@dataclass
class _Animation:
    frames: list[Image.Image]  # complete RGBA pictures, upright
    durations: list[int]  # milliseconds per frame
    plays: int | None  # total number of plays: 0 = forever, None = unknown
    had_metadata: bool


def _is_animated(source: Path) -> bool:
    try:
        with Image.open(source) as opened:
            return opened.format in ANIMATED_SOURCE_FORMATS and getattr(opened, "n_frames", 1) > 1
    except Exception:
        return False  # the regular path reports unreadable files properly


def _read_animation(source: Path, ctx: JobContext) -> _Animation:
    try:
        with Image.open(source) as opened:
            count, (width, height) = opened.n_frames, opened.size
            if count > MAX_FRAMES or count * width * height > MAX_ANIMATION_PIXELS:
                raise InvalidInputError(
                    f"The animation is too large to convert safely ({count} frames of "
                    f"{width} x {height} pixels)."
                )
            plays = _total_plays(opened)
            orientation = opened.getexif().get(EXIF_ORIENTATION, 1)
            info = opened.info
            had_metadata = any(info.get(key) for key in ("exif", "xmp", "icc_profile"))
            frames, durations = [], []
            for index in range(count):
                ctx.raise_if_cancelled()
                opened.seek(index)
                opened.load()  # the frame duration is only known after loading the frame
                duration = opened.info.get("duration")
                durations.append(DEFAULT_FRAME_MS if duration is None else int(duration))
                frame = opened.convert("RGBA")  # a complete copy, also for delta frames
                # convert() copies info (loop, ICC, EXIF, ...); writers would silently use
                # it. Only what _convert_animation passes explicitly may reach the output.
                frame.info = {}
                if orientation in _TRANSPOSE_FOR_ORIENTATION:
                    frame = frame.transpose(_TRANSPOSE_FOR_ORIENTATION[orientation])
                frames.append(frame)
    except Image.DecompressionBombError as exc:
        raise InvalidInputError(f"'{source.name}' is too large to process safely.") from exc
    except (UnidentifiedImageError, OSError, EOFError, SyntaxError, ValueError) as exc:
        raise InvalidInputError(f"'{source.name}' is corrupted or unreadable: {exc}") from exc
    return _Animation(frames, durations, plays, had_metadata)


def _total_plays(opened: Image.Image) -> int | None:
    """How often the animation plays in total: 0 = forever, None = unknown.

    The formats disagree: a GIF "loop" counts repetitions after the first play (and no
    value means: play once), while WebP and APNG count total plays. Pillow does not
    report the AVIF loop count at all.
    """
    loop = opened.info.get("loop")
    if opened.format == "GIF":
        if loop is None:
            return 1
        return 0 if loop == 0 else loop + 1
    if opened.format == "AVIF":
        return None
    return 0 if loop is None else int(loop)


def _loop_args(target: str, plays: int) -> dict:
    """Save arguments for a total number of plays (0 = forever)."""
    if target == "gif":
        if plays == 1:
            return {}  # no loop extension: browsers play the GIF once
        return {"loop": 0 if plays == 0 else plays - 1}
    return {"loop": plays}


def _has_transparency(frames: list[Image.Image]) -> bool:
    return any(
        frame.mode in ALPHA_MODES and frame.getchannel("A").getextrema()[0] < 255
        for frame in frames
    )


def _warn_partial_transparency(frames: list[Image.Image], ctx: JobContext) -> None:
    for frame in frames:
        if frame.mode in ALPHA_MODES and sum(frame.getchannel("A").histogram()[1:255]):
            ctx.warn(
                "Semi-transparent pixels became fully transparent or opaque: GIF supports "
                "only on/off transparency."
            )
            return


# --- icons ----------------------------------------------------------------------


def _fit_icon(image: Image.Image, ctx: JobContext) -> tuple[Image.Image, list[tuple[int, int]]]:
    """Make a square image of at most 256 x 256 and choose the icon sizes to store."""
    width, height = image.size
    if width != height:
        side = max(width, height)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(image, ((side - width) // 2, (side - height) // 2))
        image = canvas
        ctx.warn(
            f"The {width} x {height} image was centred on a transparent {side} x {side} "
            "square, because icons are square; it was not stretched."
        )
    if image.width > MAX_ICON_SIZE:
        ctx.warn(
            f"The image was reduced from {image.width} x {image.width} to "
            f"{MAX_ICON_SIZE} x {MAX_ICON_SIZE}, the largest icon size."
        )
        image = image.resize((MAX_ICON_SIZE, MAX_ICON_SIZE), Image.Resampling.LANCZOS)
    side = image.width
    sizes = [(size, size) for size in ICON_SIZES if size < side] + [(side, side)]
    return image, sizes


def load_first_frame(source: Path, ctx: JobContext) -> Image.Image:
    try:
        with Image.open(source) as opened:
            frame_count = getattr(opened, "n_frames", 1)
            if frame_count > 1 and opened.format in ANIMATED_SOURCE_FORMATS:
                ctx.warn(
                    f"The animation was not kept: the target format cannot store it, so only "
                    f"the first of {frame_count} frames was converted."
                )
            elif frame_count > 1:
                ctx.warn(f"The image has {frame_count} pages; only the first was converted.")
            opened.seek(0)
            bit_depth = _source_bit_depth(opened, source)
            opened.load()
            # Apply the EXIF "Orientation" tag to the pixels, so that a photo taken
            # in portrait mode still looks upright in formats that ignore EXIF.
            # Returns a new in-memory image, so it stays valid after the file is closed.
            image = ImageOps.exif_transpose(opened)
    except Image.DecompressionBombError as exc:
        # Pillow refuses images with absurd pixel counts that could exhaust memory.
        raise InvalidInputError(f"'{source.name}' is too large to process safely.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        # Pillow reports corrupted/truncated data through several exception types.
        raise InvalidInputError(f"'{source.name}' is corrupted or unreadable: {exc}") from exc

    # Pillow keeps 16-bit grayscale, but silently reads 16-bit color (RGB, RGBA, LA)
    # as 8 bits per channel. The scaling is correct; only the extra precision is lost.
    if bit_depth > 8 and image.mode not in HIGH_BIT_GRAY_MODES:
        ctx.warn("16-bit color was reduced to 8 bits per channel when reading the file.")

    # Palette/RGB images can mark one color as transparent via info["transparency"].
    # Turning that into a real alpha channel lets the rest of the code check one thing.
    if "transparency" in image.info:
        image = image.convert("RGBA")
    return image


def _source_bit_depth(opened: Image.Image, source: Path) -> int:
    """Bits per channel stored in the file; 8 when the format cannot say otherwise."""
    if opened.format == "TIFF":
        bits = opened.tag_v2.get(TIFF_BITS_PER_SAMPLE, 8)
        return max(bits) if isinstance(bits, tuple) else bits
    if opened.format == "PNG":
        # Read the IHDR field directly: Pillow does not expose the PNG bit depth.
        with source.open("rb") as file:
            header = file.read(PNG_BIT_DEPTH_OFFSET + 1)
        if len(header) > PNG_BIT_DEPTH_OFFSET:
            return header[PNG_BIT_DEPTH_OFFSET]
    return 8


def prepare_for_target(image: Image.Image, target: str, ctx: JobContext) -> Image.Image:
    """Convert the pixel mode into one the target format can store."""
    if image.mode in ALPHA_MODES and not _supports_alpha(target):
        image, had_transparency = _flatten_on_white(image)
        if had_transparency:
            ctx.warn(
                "Transparent areas were filled with white: the target format has no transparency."
            )

    if image.mode in HIGH_BIT_GRAY_MODES and image.mode not in SUPPORTED_MODES[target]:
        image = _reduce_to_8bit_gray(image)
        ctx.warn("16-bit grayscale was reduced to 8-bit: the target format cannot store more.")

    if image.mode == "CMYK" and "CMYK" not in SUPPORTED_MODES[target]:
        # Pillow's CMYK -> RGB is a simple formula, not an ICC-based conversion.
        ctx.warn(
            "CMYK colors were converted to RGB without color management; "
            "colors may look different from the original."
        )

    if image.mode not in SUPPORTED_MODES[target]:
        image = image.convert(_fallback_mode(image, target))
    return image


def _reduce_to_8bit_gray(image: Image.Image) -> Image.Image:
    # Pillow's convert("L") clips 16-bit values (anything above 255 becomes white),
    # so scale the 0..65535 range down to 0..255 first.
    return image.convert("I").point(lambda value: value * (1 / 256)).convert("L")


def _flatten_on_white(image: Image.Image) -> tuple[Image.Image, bool]:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    min_alpha, _ = alpha.getextrema()
    background = Image.new("RGB", rgba.size, WHITE)
    background.paste(rgba, mask=alpha)
    return background, min_alpha < 255


def _fallback_mode(image: Image.Image, target: str) -> str:
    supported = SUPPORTED_MODES[target]
    if image.mode in ALPHA_MODES and "RGBA" in supported:
        return "RGBA"
    if image.mode in GRAYSCALE_MODES and "L" in supported:
        return "L"
    return "RGB"  # every target supports RGB


def _supports_alpha(target: str) -> bool:
    return "RGBA" in SUPPORTED_MODES[target]


def _color_family(mode: str) -> str:
    if mode == "CMYK":
        return "cmyk"
    if mode in GRAYSCALE_MODES:
        return "gray"
    return "rgb"


def _clean_exif(raw: bytes | None) -> bytes | None:
    """Re-serialize EXIF in the standard form every writer expects, without orientation.

    WebP stores EXIF without the "Exif\\0\\0" header that JPEG needs; re-serializing
    fixes that. The orientation tag is dropped because the pixels are already upright.
    """
    if not raw:
        return None
    exif = Image.Exif()
    try:
        exif.load(raw)
    except Exception:  # damaged EXIF must not stop the conversion
        logger.info("Ignoring unreadable EXIF data", exc_info=True)
        return None
    exif.pop(EXIF_ORIENTATION, None)
    return exif.tobytes() if len(exif) else None


def _has_sub_ifds(exif_bytes: bytes) -> bool:
    exif = Image.Exif()
    exif.load(exif_bytes)
    return any(tag in exif for tag in EXIF_SUB_IFDS)


def _save_args(
    target: str,
    options: Mapping[str, int],
    icc_profile: bytes | None,
    exif: bytes | None,
    xmp: bytes | str | None,
) -> dict:
    args: dict = {}
    if target in QUALITY_OPTIONS:
        args["quality"] = options["quality"]
    if target == "tiff":
        args["compression"] = "tiff_lzw"  # lossless; uncompressed TIFF is needlessly huge
    if icc_profile and target in ICC_TARGETS:
        args["icc_profile"] = icc_profile
    if exif and target in EXIF_TARGETS:
        args["exif"] = exif
    if xmp and target in XMP_TARGETS:
        args["xmp"] = xmp.encode("utf-8") if isinstance(xmp, str) else xmp
    return args


def _verify_output(path: Path, expected_format: str) -> None:
    """Re-open the written file to make sure it is a valid image of the right type."""
    try:
        with Image.open(path) as written:
            if written.format != expected_format:
                raise OutputError(f"Output is {written.format}, expected {expected_format}.")
            written.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise OutputError(f"The written file is not a valid image: {exc}") from exc
