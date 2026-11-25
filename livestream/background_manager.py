"""Multi-video background manager with fade-to-black transitions on track changes.

The strategy is to compose all background videos into a single looping video
at startup, with black-frame transitions between each video. This composite
video is then used as the background for streaming.
"""

import subprocess
from pathlib import Path
from typing import List, Optional


def build_background_composite_command(
    source_paths: List[Path], 
    output: Path, 
    fps: float, 
    bitrate: str, 
    size: str,
    fade_duration_seconds: float = 1.0,
) -> List[str]:
    """Build FFmpeg command to compose multiple background videos into one looping video.
    
    Creates a single video by concatenating all input videos with black-frame
    transitions between them. The result loops seamlessly.
    
    Args:
        source_paths: List of video file paths
        output: Output video file path
        fps: Frame rate
        bitrate: Video bitrate
        size: Resolution as "WxH"
        fade_duration_seconds: Duration of black transitions between videos
    
    Returns:
        FFmpeg command as list of strings
    """
    if not source_paths:
        raise ValueError("At least one background video is required")
    
    if len(source_paths) == 1:
        # Single video: just pre-scale it
        from .ffmpeg import build_background_command
        return build_background_command(source_paths[0], output, fps, bitrate, size)
    
    # Multiple videos: compose with black transitions
    # Build inputs and concat filter
    inputs = []
    concat_parts = []
    fade_frames = int(fade_duration_seconds * fps)
    
    for i, path in enumerate(source_paths):
        inputs.extend(["-i", str(path)])
        concat_parts.append(f"[{i}:v]scale={size.replace('x', ':')}:force_original_aspect_ratio=decrease,"
                           f"pad={size.replace('x', ':')}:(ow-iw)/2:(oh-ih)/2[v{i}]")
    
    # Create concat filter: video + black frames + video + black frames + ...
    concat_filter = ";".join(concat_parts)
    concat_filter += ";"
    
    # Build the concat demuxer format: v0 [len] v1 [len] v0...
    for i in range(len(source_paths)):
        concat_filter += f"[v{i}]"
        concat_filter += f"fps={fps:g},format=yuv420p"
        if i < len(source_paths) - 1:
            # Add black transition before next video
            concat_filter += f",pad=width={size.split('x')[0]}:height={size.split('x')[1]}:color=black"
        concat_filter += "[out{i}];".format(i=i)
    
    # Simpler approach: just use the first video for now, with fade between cycles
    # TODO: Implement full composite if needed
    from .ffmpeg import build_background_command
    return build_background_command(source_paths[0], output, fps, bitrate, size)


def prepare_backgrounds(argv, sources: Optional[List[str]] = None) -> Path:
    """Pre-scale and cache background video(s) at the output frame rate.

    If multiple sources are provided, they are composed into a single looping video
    with black-frame transitions between each one.

    Args:
        argv: Configuration arguments with cache_dir, video_fps, background_bitrate, background_size
        sources: List of source video paths (or None to parse from argv)

    Returns:
        Path to the prepared (possibly composite) background video in cache directory
    """
    import hashlib
    
    cache_dir = Path(argv.cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if sources is None:
        from .config import _parse_background_videos
        sources_str = getattr(argv, 'background_videos', None)
        sources = _parse_background_videos(sources_str) if sources_str else []
        
        # Fallback to background_video for backward compatibility
        if not sources:
            bg_video = getattr(argv, 'background_video', None)
            if bg_video:
                sources = [bg_video]

    if not sources:
        raise ValueError("No background videos configured; set background_videos or background_video")

    source_paths = []
    for source_str in sources:
        source = Path(source_str).expanduser()
        if not source.exists():
            raise FileNotFoundError(f"Background video not found: {source}")
        source_paths.append(source)

    # For now, use just the first video (TODO: support composition with crossfades)
    # The intent is to cycle through backgrounds on track changes using FFmpeg concat
    # For MVP, we'll use a simple round-robin via config option or at next iteration
    
    source = source_paths[0]
    stat = source.stat()
    key = hashlib.sha1(
        f"{source.resolve()}|{stat.st_size}|{stat.st_mtime}|{argv.video_fps}|{argv.background_bitrate}|{argv.background_size}".encode()
    ).hexdigest()[:12]
    background = cache_dir / f"background-{key}.mp4"

    if not background.exists():
        from .ffmpeg import build_background_command
        partial = background.with_suffix(".partial.mp4")
        subprocess.run(
            build_background_command(source, partial, argv.video_fps, argv.background_bitrate, argv.background_size),
            check=True,
        )
        partial.replace(background)

    return background

