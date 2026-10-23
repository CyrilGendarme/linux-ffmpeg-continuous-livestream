from pathlib import Path
from typing import List, Optional

from .overlay import CANVAS_H, CENTER_H, CENTER_W, CENTER_Y, PANEL_W, PANEL_X


# Raw PCM format exchanged between track decoders, the Python mixer and the encoder.
PCM_FORMAT = "f32le"
PCM_SAMPLE_RATE = 48000
PCM_CHANNELS = 2

# Packets FFmpeg may queue per piped input. The Python writers are unpaced and
# the encoder throttles itself with realtime filters, so these queues always fill
# (FFmpeg logs "Thread message queue blocking" once per input; that is expected).
# Larger queues let encoding ride out short stalls on the Python side
# (GC, prerendering, disk) at the cost of overlay latency and memory:
# 64 raw frames is ~2 s at 30 fps and ~155 MB across both overlay inputs;
# 256 PCM packets (1024 samples each) is ~5 s of audio.
OVERLAY_QUEUE_SIZE = 64
AUDIO_QUEUE_SIZE = 256


def build_decoder_command(track: Path, gain_db: float = 0.0) -> List[str]:
    """Decode a single track to raw PCM on stdout, applying a fixed gain."""
    gain = ["-af", f"volume={gain_db:.2f}dB"] if gain_db else []
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "1",
        "-i",
        str(track),
        "-map",
        "0:a:0",
        *gain,
        "-f",
        PCM_FORMAT,
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-ac",
        str(PCM_CHANNELS),
        "pipe:1",
    ]


LAYOUT_SIZE = "1920x1080"  # overlay coordinates below are designed for this canvas


def build_background_command(source: Path, output: Path, fps: float, bitrate: str) -> List[str]:
    """Pre-scale the background loop once: layout size, output fps, no audio, moderate bitrate."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-an",
        "-vf", f"scale={LAYOUT_SIZE.replace('x', ':')},fps={fps:g},format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-b:v", bitrate,
        "-movflags", "+faststart",
        str(output),
    ]


def _encoder_args(stream_url: str, output_format: str, settings) -> List[str]:
    """Output encoding shared by the radio encoder and the switcher's output encoder."""
    fps = f"{settings.video_fps:g}"
    bitrate = settings.video_bitrate
    # Buffer = 1s of bitrate (OBS's CBR default) keeps the rate steady second to second.
    bufsize = settings.video_bufsize or bitrate
    x264_params = []
    if output_format == "mpegts":
        # MPEG-TS has no global header: repeat SPS/PPS on keyframes so a
        # viewer joining mid-stream can decode. FLV/RTMP carries them already.
        x264_params.append("repeat-headers=1")
    if settings.rc_lookahead is not None:
        x264_params.append(f"rc-lookahead={settings.rc_lookahead}")
    # Constant bitrate: ingest servers expect a steady rate.
    x264_params.append("nal-hrd=cbr")

    is_network = stream_url.split(":", 1)[0].lower() in {"rtmp", "rtmps", "rtmpt", "rtmpe"}
    network = ["-rw_timeout", str(int(settings.network_timeout * 1_000_000))] if is_network else []

    return [
        "-c:v", "libx264",
        "-preset", settings.video_preset,
        "-b:v", bitrate,
        "-maxrate", bitrate,
        "-bufsize", bufsize,
        "-x264-params", ":".join(x264_params),
        "-threads", str(settings.encoder_threads),
        "-pix_fmt", "yuv420p",
        "-r", fps,
        "-force_key_frames", f"expr:gte(t,n_forced*{settings.keyframe_seconds:g})",
        "-c:a", "aac",
        "-b:a", settings.audio_bitrate,
        "-ar", "48000",
        "-ac", "2",
        *network,
    ]


def build_ffmpeg_command(
    background_video: Path,
    panel_fd: int,
    title_fd: int,
    stream_url: str,
    output_format: str,
    settings,
    realtime: bool = True,
    duration: Optional[float] = None,
) -> List[str]:
    """Long-running encoder.

    Inputs: looped background video, the now-playing panel as raw RGBA frames
    (pipe `panel_fd`), the centre title as a raw alpha mask (pipe `title_fd`),
    and raw PCM audio on stdin. Overlay frames come from livestream.overlay.

    `settings` provides video_bitrate, video_bufsize, video_fps, video_size,
    video_preset, encoder_threads, rc_lookahead, audio_bitrate,
    keyframe_seconds and network_timeout (see config.OPTIONS).
    """
    fps = f"{settings.video_fps:g}"
    scale_out = "" if settings.video_size == LAYOUT_SIZE else f",scale={settings.video_size.replace('x', ':')}"
    video = (
        # Renumber frames so the looped background has monotonic timestamps.
        "[0:v]setpts=N/FRAME_RATE/TB[bg];"
        f"color=c=white:s={CENTER_W}x{CENTER_H}:r={fps}[white];"
        "[white][2:v]alphamerge[title];"
        f"[bg][title]overlay=0:{CENTER_Y}:format=yuv420[withtitle];"
        f"[withtitle][1:v]overlay={PANEL_X}:0:format=yuv420"
        + scale_out
        # Throttle output to wall-clock speed so live destinations stay fed
        # steadily and the overlay animation stays in sync with what is heard.
        + (",realtime" if realtime else "")
        + "[vout]"
    )
    audio = "[3:a]arealtime[aout]" if realtime else "[3:a]anull[aout]"

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        # Machine-readable progress on stdout, read by the stall watchdog.
        "-progress",
        "pipe:1",
        "-stats_period",
        "1",
        "-stream_loop",
        "-1",
        "-i",
        str(background_video),
        "-thread_queue_size",
        str(OVERLAY_QUEUE_SIZE),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-video_size",
        f"{PANEL_W}x{CANVAS_H}",
        "-framerate",
        fps,
        "-i",
        f"pipe:{panel_fd}",
        "-thread_queue_size",
        str(OVERLAY_QUEUE_SIZE),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-video_size",
        f"{CENTER_W}x{CENTER_H}",
        "-framerate",
        fps,
        "-i",
        f"pipe:{title_fd}",
        "-thread_queue_size",
        str(AUDIO_QUEUE_SIZE),
        "-f",
        PCM_FORMAT,
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-ac",
        str(PCM_CHANNELS),
        "-i",
        "pipe:0",
        "-filter_complex",
        f"{video};{audio}",
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-shortest",
        *_encoder_args(stream_url, output_format, settings),
        *(["-t", f"{duration:g}"] if duration else []),
        "-f",
        output_format,
        stream_url,
    ]


# -- Source switcher (livestream.switcher) -----------------------------------
# The switcher's MediaMTX "live" path only accepts what its offline clip
# carries: H.264 video and AAC at this rate/layout.
SWITCH_AUDIO_RATE = 48000
SWITCH_AUDIO_CHANNELS = 2


def build_offline_clip_command(background: Path, output: Path, fps: float, seconds: float = 10.0) -> List[str]:
    """Clip MediaMTX loops on the live path while no source is relayed: the radio background, silent."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-stream_loop", "-1", "-i", str(background),
        "-f", "lavfi", "-i", f"anullsrc=r={SWITCH_AUDIO_RATE}:cl=stereo",
        "-t", f"{seconds:g}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", "-g", f"{2 * fps:g}",
        "-c:a", "aac", "-ar", str(SWITCH_AUDIO_RATE), "-ac", str(SWITCH_AUDIO_CHANNELS),
        "-movflags", "+faststart",
        str(output),
    ]


def build_relay_reader_command(source_url: str, timeout: float) -> List[str]:
    """First half of a relay: a source path as FLV on stdout, audio brought to the live path's format."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-rw_timeout", str(int(timeout * 1_000_000)),
        "-i", source_url,
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy",
        # OBS may send 44.1 kHz or mono; the live path would refuse that.
        "-c:a", "aac", "-b:a", "320k", "-ar", str(SWITCH_AUDIO_RATE), "-ac", str(SWITCH_AUDIO_CHANNELS),
        "-flush_packets", "1",
        "-flvflags", "no_duration_filesize",  # a pipe cannot be rewound to fix up the header
        "-f", "flv", "pipe:1",
    ]


def build_relay_publisher_command(target_url: str) -> List[str]:
    """Second half of a relay: FLV on stdin (from its first keyframe) published to the live path."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-f", "flv", "-i", "pipe:0",
        "-c", "copy",
        "-f", "rtsp", "-rtsp_transport", "tcp",
        target_url,
    ]


def build_live_reader_command(source_url: str, timeout: float) -> List[str]:
    """Remux the switcher's live path to FLV on stdout, for livestream.flv_retime."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        # Read over RTMP: MediaMTX keeps its timestamps continuous across a
        # publisher change, while ffmpeg's RTSP client makes them jump back.
        "-rw_timeout", str(int(timeout * 1_000_000)),
        "-i", source_url,
        "-map", "0:v:0", "-map", "0:a:0",
        "-c", "copy",
        "-flush_packets", "1",
        "-flvflags", "no_duration_filesize",  # a pipe cannot be rewound to fix up the header
        "-f", "flv", "pipe:1",
    ]


def build_output_command(stream_url: str, output_format: str, settings) -> List[str]:
    """Long-running encoder from the switcher's live path (re-timed FLV on stdin) to the final destination.

    Sources differ in size, frame rate and timestamps, and change mid-stream;
    everything is normalized here so the destination sees one steady stream.
    """
    width, height = settings.video_size.split("x")
    video = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        f"fps={settings.video_fps:g},format=yuv420p"
    )
    # Fill audio where a stalled source leaves a hole.
    audio = f"aresample={SWITCH_AUDIO_RATE}:async=1000,aformat=channel_layouts=stereo"
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        # Machine-readable progress on stdout, read by the stall watchdog.
        "-progress",
        "pipe:1",
        "-stats_period",
        "1",
        "-f",
        "flv",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        # Separate graphs: a source with another resolution rebuilds the video
        # graph, and must not restart (and rewind) the audio one.
        "-vf",
        video,
        "-af",
        audio,
        *_encoder_args(stream_url, output_format, settings),
        "-f",
        output_format,
        stream_url,
    ]
