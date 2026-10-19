from pathlib import Path
from typing import Dict, List


def ffmpeg_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_ffmpeg_command(
    background_video: Path,
    tracks: List[Path],
    track_durations: List[float],
    overlay_files: Dict[str, Path],
    stream_url: str,
    output_format: str,
    title_font_path: str,
    info_font_path: str,
    track_fade_seconds: float,
) -> List[str]:
    title_font = ffmpeg_escape(title_font_path)
    info_font = ffmpeg_escape(info_font_path)
    center_title = ffmpeg_escape(str(overlay_files["center"]))
    artist = ffmpeg_escape(str(overlay_files["artist"]))
    track_title = ffmpeg_escape(str(overlay_files["title"]))
    album = ffmpeg_escape(str(overlay_files["album"]))
    label = ffmpeg_escape(str(overlay_files["label"]))
    year = ffmpeg_escape(str(overlay_files["year"]))
    bpm = ffmpeg_escape(str(overlay_files["bpm"]))

    if len(tracks) != len(track_durations):
        raise ValueError("Track list and duration list must be the same length")

    effective_fade = max(0.0, float(track_fade_seconds))
    if track_durations:
        shortest_track = min(track_durations)
        effective_fade = min(effective_fade, max(shortest_track / 2.0, 0.0))

    audio_inputs: List[str] = []
    audio_filters: List[str] = []
    for index, track in enumerate(tracks, start=1):
        audio_inputs.extend(["-i", str(track)])
        audio_filters.append(
            f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{index}]"
        )

    if len(tracks) == 1:
        if effective_fade > 0.0:
            audio_filters.append(
                f"[a1]afade=t=in:st=0:d={effective_fade:.3f},"
                f"afade=t=out:st={max(track_durations[0] - effective_fade, 0.0):.3f}:d={effective_fade:.3f}[audio]"
            )
        else:
            audio_filters.append("[a1]anull[audio]")
    else:
        if effective_fade > 0.0:
            audio_filters[0] = f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,afade=t=in:st=0:d={effective_fade:.3f}[a1]"
            audio_filters[-1] = (
                f"[{len(tracks)}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"afade=t=out:st={max(track_durations[-1] - effective_fade, 0.0):.3f}:d={effective_fade:.3f}[a{len(tracks)}]"
            )

        current_label = "a1"
        for index in range(2, len(tracks) + 1):
            next_label = f"a{index}"
            output_label = f"x{index}"
            audio_filters.append(
                f"[{current_label}][{next_label}]acrossfade=d={effective_fade:.3f}:c1=tri:c2=tri[{output_label}]"
            )
            current_label = output_label
        audio_filters.append(f"[{current_label}]anull[audio]")

    overlay = (
        "[0:v]scale=1920:1080,format=yuv420p,"
        "drawbox=x=0:y=0:w=300:h=ih:color=black@0.54:t=fill,"
        "drawbox=x=16:y=22:w=268:h=66:color=#111111@0.95:t=fill,"
        "drawbox=x=16:y=102:w=268:h=710:color=#111111@0.92:t=fill,"
        "drawbox=x=16:y=826:w=268:h=230:color=#111111@0.95:t=fill,"
        f"drawtext=fontfile='{info_font}':text='Now Playing':x=28:y=38:fontsize=34:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='Artist\\:':x=28:y=140:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{artist}':reload=1:x=28:y=172:fontsize=23:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='Title\\:':x=28:y=224:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{track_title}':reload=1:x=28:y=256:fontsize=23:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='Album\\:':x=28:y=308:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{album}':reload=1:x=28:y=340:fontsize=23:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='Label\\:':x=28:y=392:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{label}':reload=1:x=28:y=424:fontsize=23:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='Year\\:':x=28:y=476:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{year}':reload=1:x=28:y=508:fontsize=23:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':text='BPM\\:':x=28:y=560:fontsize=25:fontcolor=white,"
        f"drawtext=fontfile='{info_font}':textfile='{bpm}':reload=1:x=28:y=592:fontsize=23:fontcolor=white,"
        "drawbox=x=320:y=h/2-120:w=w-420:h=240:color=black@0.44:t=fill,"
        f"drawtext=fontfile='{title_font}':textfile='{center_title}':reload=1:"
        "x=(w-text_w)/2:y=(h-text_h)/2:fontsize=80:fontcolor=white:shadowcolor=black:shadowx=2:shadowy=2[video]"
    )
    filter_complex = ";".join([overlay, *audio_filters])
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-stream_loop",
        "-1",
        "-i",
        str(background_video),
        *audio_inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        "[video]",
        "-map",
        "[audio]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        "-f",
        output_format,
        stream_url,
    ]
