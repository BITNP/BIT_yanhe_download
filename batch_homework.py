import argparse
import subprocess
import time
from pathlib import Path

import torch
import whisper
from zhconv import convert

import find_homework
from gen_caption import DEFAULT_CLI_MODEL, NOISE_PHRASES, seconds_to_hmsm


MEDIA_EXTENSIONS = (".mp4",)


def log(message: str) -> None:
    print(message, flush=True)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate captions for downloaded video/audio pairs, then search homework hits per lesson."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default="output",
        help="Directory to scan for downloaded .mp4 files. Defaults to output/.",
    )
    parser.add_argument(
        "--output",
        default="homework_results",
        help="Parent directory for per-lesson homework reports.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_CLI_MODEL,
        help=f"Whisper model name. Defaults to {DEFAULT_CLI_MODEL}.",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cuda", "cpu"),
        default="auto",
        help="Whisper device. Defaults to auto.",
    )
    parser.add_argument(
        "--overwrite-srt",
        action="store_true",
        help="Regenerate captions even when the .srt file already exists.",
    )
    parser.add_argument(
        "--whisper-progress",
        action="store_true",
        help="Show Whisper's internal tqdm progress while transcribing. Hidden by default.",
    )
    parser.add_argument(
        "-k",
        "--keyword",
        action="append",
        dest="keywords",
        help="Homework keyword. Can be used multiple times. Defaults to 作业.",
    )
    parser.add_argument(
        "--keywords-file",
        help="Text file with one keyword per line. Blank lines and lines starting with # are ignored.",
    )
    parser.add_argument(
        "--screenshot-offsets",
        default="0",
        help="Comma-separated seconds relative to each matched event start, e.g. -10,0,10.",
    )
    parser.add_argument(
        "--merge-gap",
        type=float,
        default=find_homework.DEFAULT_MERGE_GAP,
        help="Merge hits in the same video when the next hit starts within this many seconds. Defaults to 60.",
    )
    parser.add_argument(
        "--no-screenshots",
        action="store_true",
        help="Only write CSV reports; do not call ffmpeg for screenshots.",
    )
    parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Use case-sensitive matching for ASCII keywords.",
    )
    return parser.parse_args()


def select_device(device_arg: str) -> str:
    if device_arg == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if device_arg == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested, but torch.cuda.is_available() is false.")
    return device_arg


def find_video_files(root: Path) -> list[Path]:
    if root.is_file():
        if root.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError(f"Expected a .mp4 file or directory: {root}")
        return [root]
    return sorted(path for path in root.rglob("*.mp4") if path.is_file())


def media_for_video(video_path: Path) -> Path:
    audio_path = video_path.with_suffix(".aac")
    return audio_path if audio_path.exists() else video_path


def extract_audio(video_path: Path) -> Path:
    audio_path = video_path.with_suffix(".m4a")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-ar",
        str(whisper.audio.SAMPLE_RATE),
        "-y",
        str(audio_path),
    ]
    subprocess.run(command, check=True)
    return audio_path


def contains_noise(text: str) -> bool:
    """Return True if text contains any known noise phrase from whisper contamination."""
    for phrase in NOISE_PHRASES:
        if phrase in text:
            return True
    return False


def write_srt(segments: list[dict], srt_path: Path) -> None:
    with srt_path.open("w", encoding="utf-8") as f:
        srt_index = 0
        for segment in segments:
            text = convert(segment["text"], "zh-cn")
            if contains_noise(text):
                continue
            srt_index += 1
            f.write(f"{srt_index}\n")
            f.write(
                seconds_to_hmsm(float(segment["start"]))
                + " --> "
                + seconds_to_hmsm(float(segment["end"]))
                + "\n"
            )
            f.write(text + "\n\n")


def generate_caption(
    model,
    media_path: Path,
    srt_path: Path,
    device: str,
    whisper_progress: bool,
) -> None:
    audio_path = media_path
    remove_audio = False
    if media_path.suffix.lower() == ".mp4":
        audio_path = extract_audio(media_path)
        remove_audio = True

    try:
        start = time.time()
        result = model.transcribe(
            str(audio_path),
            verbose=False if whisper_progress else None,
            language="zh",
            fp16=(device == "cuda"),
        )
        write_srt(result["segments"], srt_path)
        log(f"[caption done] {srt_path} ({time.time() - start:.1f}s)")
    finally:
        if remove_audio and audio_path.exists():
            audio_path.unlink()


def write_homework_report(
    srt_path: Path,
    output_dir: Path,
    keywords: list[str],
    offsets: list[float],
    merge_gap: float,
    no_screenshots: bool,
    case_sensitive: bool,
) -> tuple[int, int, Path]:
    hits = find_homework.collect_hits([srt_path], keywords, case_sensitive)
    events = find_homework.merge_hits(hits, merge_gap)
    if not no_screenshots:
        find_homework.add_screenshots(events, output_dir, offsets)
    csv_path = find_homework.write_csv(events, output_dir)
    return len(hits), len(events), csv_path


def main():
    start_time = time.time()
    args = parse_args()
    root = Path(args.root)
    output_root = Path(args.output)
    videos = find_video_files(root)
    if not videos:
        log(f"No .mp4 files found under {root}")
        return

    device = select_device(args.device)
    keywords = find_homework.load_keywords(args)
    offsets = find_homework.parse_offsets(args.screenshot_offsets)

    log(f"Found {len(videos)} video file(s).")
    log(f"Using Whisper model: {args.model}")
    log(f"Using device: {device}")
    if device == "cuda":
        log(f"CUDA device: {torch.cuda.get_device_name(0)}")

    captions_to_generate = [
        video_path
        for video_path in videos
        if args.overwrite_srt or not video_path.with_suffix(".srt").exists()
    ]
    log(f"Captions to generate: {len(captions_to_generate)}")

    model = None
    if captions_to_generate:
        model = whisper.load_model(args.model, device=device, download_root="whisper_models/")

    generated_captions = 0
    skipped_captions = 0
    total_hits = 0
    total_events = 0

    for index, video_path in enumerate(videos, 1):
        media_path = media_for_video(video_path)
        srt_path = video_path.with_suffix(".srt")
        lesson_output = output_root / find_homework.safe_stem(video_path)

        log(f"[{index}/{len(videos)}] {video_path}")
        log(f"[media] {media_path}")
        if args.overwrite_srt or not srt_path.exists():
            if model is None:
                raise RuntimeError("Internal error: caption generation requires a loaded Whisper model.")
            generate_caption(
                model,
                media_path,
                srt_path,
                device,
                args.whisper_progress,
            )
            generated_captions += 1
        else:
            log(f"[caption skip] {srt_path}")
            skipped_captions += 1

        hit_count, event_count, csv_path = write_homework_report(
            srt_path=srt_path,
            output_dir=lesson_output,
            keywords=keywords,
            offsets=offsets,
            merge_gap=args.merge_gap,
            no_screenshots=args.no_screenshots,
            case_sensitive=args.case_sensitive,
        )
        total_hits += hit_count
        total_events += event_count
        log(f"[homework done] {hit_count} hit(s), {event_count} event(s): {csv_path}")

    log("[summary]")
    log(f"Videos processed: {len(videos)}")
    log(f"Captions generated: {generated_captions}")
    log(f"Captions skipped: {skipped_captions}")
    log(f"Homework hits: {total_hits}")
    log(f"Homework events: {total_events}")
    log(f"Output directory: {output_root}")
    log(f"Elapsed: {time.time() - start_time:.1f}s")


if __name__ == "__main__":
    main()
