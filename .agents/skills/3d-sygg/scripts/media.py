#!/usr/bin/env python3
"""FFmpeg-based trimming, normalization, stitching, mixing, and technical QA."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

try:
    from .providers import ValidationError, atomic_write_json, sha256_file, canonical_hash
except ImportError:  # Direct script execution.
    from providers import ValidationError, atomic_write_json, sha256_file, canonical_hash


TARGET_FPS = 30


def require_media_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValidationError("ffmpeg and ffprobe are required")
    return ffmpeg, ffprobe


def _run(command: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1600:]
        raise ValidationError(f"Media command failed ({result.returncode}): {detail}")
    return result


def image_pixel_hash(path: Path) -> str:
    """Hash decoded pixels, without judging whether two valid compositions look similar."""
    ffmpeg, _ = require_media_tools()
    result = _run([ffmpeg, "-v", "error", "-i", str(path), "-frames:v", "1",
                   "-pix_fmt", "rgba", "-f", "hash", "-hash", "sha256", "-"], timeout=90)
    digest = result.stdout.strip().removeprefix("SHA256=")
    if len(digest) != 64:
        raise ValidationError("Could not fingerprint decoded reference pixels")
    return digest


def probe_media(path: Path) -> dict[str, Any]:
    _, ffprobe = require_media_tools()
    if not path.is_file():
        raise ValidationError(f"Media file is missing: {path}")
    result = _run(
        [
            ffprobe, "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ],
        timeout=60,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"ffprobe returned invalid JSON for {path}") from exc
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        raise ValidationError(f"ffprobe result has no streams for {path}")
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    duration_value = (data.get("format") or {}).get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "path": str(path.resolve()),
        "duration": duration,
        "video_duration": float(video.get("duration") or duration) if video else 0,
        "audio_duration": float(audio.get("duration") or duration) if audio else 0,
        "width": int(video.get("width", 0)) if video else 0,
        "height": int(video.get("height", 0)) if video else 0,
        "video_codec": video.get("codec_name") if video else None,
        "video_frames": int(video["nb_frames"]) if video and str(video.get("nb_frames", "")).isdigit() else None,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "sample_rate": int(audio.get("sample_rate", 0)) if audio and audio.get("sample_rate") else None,
    }


def normalize_clip(
    source: Path,
    output: Path,
    keep_duration: float,
    *,
    target_width: int,
    target_height: int,
    trim_start: float = 0,
    align_frames: bool = False,
) -> Path:
    ffmpeg, _ = require_media_tools()
    if not math.isfinite(keep_duration) or keep_duration <= 0 or not math.isfinite(trim_start) or trim_start < 0:
        raise ValidationError("Clip duration and trim start must be finite and valid")
    if target_width <= 0 or target_height <= 0:
        raise ValidationError("Target video dimensions must be positive")
    signature = canonical_hash([sha256_file(source), round(keep_duration * TARGET_FPS), trim_start, target_width, target_height, TARGET_FPS] + (['frame-boundary-v1'] if align_frames else []))
    receipt = output.with_suffix('.normalize.json')
    if output.is_file() and receipt.is_file():
        cached = json.loads(receipt.read_text())
        if cached.get('signature') == signature and cached.get('sha256') == sha256_file(output):
            return output
    info = probe_media(source)
    if not info["has_video"]:
        raise ValidationError(f"Clip has no video stream: {source}")
    if info["video_duration"] + 1 / TARGET_FPS + 1e-6 < keep_duration + trim_start:
        raise ValidationError("Video result is shorter than the approved usable interval")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-i", str(source), "-ss", f"{trim_start:.9f}"]
    if not info["has_audio"]:
        raise ValidationError("Omni result has no ambient SFX audio track; do not replace it with artificial silence")
    audio_map = "0:a:0"
    video_filter = (
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
        f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={TARGET_FPS}"
    )
    if align_frames:
        # One boundary frame (33 ms) is an editing repair, not a new generation.
        # The source-length check above prevents padding substantive missing footage.
        video_filter += ",tpad=stop_mode=clone:stop=1"
    command += [
        "-t", f"{round(keep_duration * TARGET_FPS) / TARGET_FPS:.9f}", "-map", "0:v:0", "-map", audio_map,
        "-vf", video_filter, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", "-shortest", str(output),
    ]
    _run(command)
    atomic_write_json(receipt, {"signature": signature, "sha256": sha256_file(output)})
    return output


def stitch_clips(clips: list[Path], output: Path) -> Path:
    ffmpeg, _ = require_media_tools()
    if not clips:
        raise ValidationError("At least one normalized clip is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="3d-sygg-concat-") as temp_dir:
        manifest = Path(temp_dir) / "clips.txt"
        lines = []
        for clip in clips:
            escaped = str(clip.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _run(
            [
                ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(manifest),
                "-c", "copy", "-movflags", "+faststart", str(output),
            ]
        )
    return output


def mix_narration(video: Path, narration: Path, output: Path, *, start_time: float = 0) -> Path:
    ffmpeg, _ = require_media_tools()
    video_info = probe_media(video)
    narration_info = probe_media(narration)
    if not video_info["has_video"] or not video_info["has_audio"]:
        raise ValidationError("Stitched video must contain video and ambient audio streams")
    if not narration_info["has_audio"]:
        raise ValidationError("Narration file has no audio stream")
    if not math.isfinite(start_time) or start_time < 0:
        raise ValidationError("Narration start time must be nonnegative and finite")
    if start_time + narration_info["duration"] > video_info["duration"] + 0.10:
        raise ValidationError("Narration is longer than the video; do not silently cut speech")
    output.parent.mkdir(parents=True, exist_ok=True)
    graph = (
        "[0:a]aresample=48000,asetpts=PTS-STARTPTS[bg];"
        f"[1:a]loudnorm=I=-16:TP=-1.5:LRA=7,aresample=48000,asetpts=PTS-STARTPTS,adelay={round(start_time * 1000)}:all=1,apad,asetpts=N/SR/TB,atrim=duration={video_info['duration']:.6f},asplit=2[voice_sc][voice_mix];"
        "[bg][voice_sc]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=400[ducked];"
        "[ducked][voice_mix]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[aout]"
    )
    _run(
        [
            ffmpeg, "-y", "-i", str(video), "-i", str(narration),
            "-filter_complex", graph, "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(output),
        ]
    )
    return output


def extract_review_frames(video: Path, output_dir: Path, *, count: int = 6, timestamps: list[float] | None = None) -> list[Path]:
    ffmpeg, _ = require_media_tools()
    info = probe_media(video)
    if info["duration"] <= 0:
        raise ValidationError("Cannot extract frames from a zero-duration video")
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    times = timestamps if timestamps is not None else [info["duration"] * (i + 1) / (count + 1) for i in range(count)]
    for index, timestamp in enumerate(times):
        target = output_dir / f"review-{index + 1:02d}.jpg"
        _run(
            [
                ffmpeg, "-y", "-ss", f"{timestamp:.3f}", "-i", str(video),
                "-frames:v", "1", "-q:v", "2", str(target),
            ],
            timeout=90,
        )
        frames.append(target)
    return frames


def full_decode_check(video: Path) -> None:
    ffmpeg, _ = require_media_tools()
    _run([ffmpeg, "-v", "error", "-i", str(video), "-f", "null", "-"], timeout=900)


def write_qa_report(
    video: Path,
    report_path: Path,
    *,
    expected_duration: float,
    narration_expected: bool,
    expected_width: int,
    expected_height: int,
    expected_frames: int | None = None,
) -> dict[str, Any]:
    info = probe_media(video)
    decode_ok = True
    decode_error: str | None = None
    try:
        full_decode_check(video)
    except ValidationError as exc:
        decode_ok = False
        decode_error = str(exc)
    checks = {
        "decodes_completely": decode_ok,
        "resolution_matches_target": (
            info["width"] == expected_width and info["height"] == expected_height
        ),
        "duration_within_0_35s": abs(info["duration"] - expected_duration) <= 0.35,
        "audio_stream_present": bool(info["has_audio"]),
        "audio_covers_video": info["audio_duration"] + 0.10 >= info["video_duration"],
        "narration_stream_expected": narration_expected,
    }
    duration_check = "duration_within_0_35s"
    if expected_frames is not None:
        # Use metadata from the existing probe; do not decode the film a second time
        # just to count frames. Tolerate one boundary frame, never per-segment drift.
        actual_frames = info.get("video_frames")
        checks["video_duration_within_one_frame"] = abs(info["video_duration"] - expected_frames / TARGET_FPS) <= 1 / TARGET_FPS + 1e-6
        checks["video_frames_within_one"] = actual_frames is None or abs(actual_frames - expected_frames) <= 1
        duration_check = "video_duration_within_one_frame"
    technical_status = "pass" if all(
        checks[key] for key in (
            "decodes_completely",
            "resolution_matches_target",
            duration_check,
            "audio_stream_present",
            "audio_covers_video",
        )
    ) and checks.get("video_frames_within_one", True) else "failed"
    report = {
        "status": "awaiting_manual_review" if technical_status == "pass" else "failed",
        "technical_status": technical_status,
        "creative_review_status": "pending" if technical_status == "pass" else "blocked",
        "technical": info,
        "final_sha256": sha256_file(video),
        "checks": checks,
        "decode_error": decode_error,
        "manual_review_required": True,
        "manual_review_items": [
            "视频是否按已批准分镜顺序和每个关键构图推进",
            "商品外形、按键、材质和 Logo 是否忠实",
            "广告文字是否正确且没有乱码",
            "人物模式下是否保持同一位成年人物、同一服装与妆造",
            "人物模式下手部结构、握持和商品交互是否自然可信",
            "跨段匹配剪辑是否自然",
            "是否出现意外人物对白或人声",
            "旁白、环境音和冲击音听感是否专业",
        ],
    }
    atomic_write_json(report_path, report)
    return report
