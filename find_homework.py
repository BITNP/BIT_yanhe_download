import argparse
import csv
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_KEYWORDS = ["作业"]
DEFAULT_SCREENSHOT_OFFSETS = [0.0]
MEDIA_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi")


@dataclass
class SubtitleSegment:
    index: int
    start: float
    end: float
    text: str


@dataclass
class HomeworkHit:
    video_path: Path | None
    srt_path: Path
    segment: SubtitleSegment
    keywords: list[str]
    screenshots: list[Path]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Search generated SRT captions for homework keywords and capture nearby screenshots."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default="output",
        help="Directory or .srt file to scan. Defaults to output/.",
    )
    parser.add_argument(
        "-k",
        "--keyword",
        action="append",
        dest="keywords",
        help="Keyword to search. Can be used multiple times. Defaults to 作业.",
    )
    parser.add_argument(
        "--keywords-file",
        help="Text file with one keyword per line. Blank lines and lines starting with # are ignored.",
    )
    parser.add_argument(
        "--output",
        default="homework_results",
        help="Directory for CSV report and screenshots.",
    )
    parser.add_argument(
        "--screenshot-offsets",
        default="0",
        help="Comma-separated seconds relative to each matched subtitle start, e.g. -10,0,10.",
    )
    parser.add_argument(
        "--no-screenshots",
        action="store_true",
        help="Only write the CSV report; do not call ffmpeg for screenshots.",
    )
    parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Use case-sensitive matching for ASCII keywords.",
    )
    return parser.parse_args()


def load_keywords(args) -> list[str]:
    keywords = list(args.keywords or DEFAULT_KEYWORDS)
    if args.keywords_file:
        keyword_path = Path(args.keywords_file)
        for line in keyword_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                keywords.append(line)

    seen = set()
    deduped = []
    for keyword in keywords:
        keyword = keyword.strip()
        if keyword and keyword not in seen:
            seen.add(keyword)
            deduped.append(keyword)
    return deduped


def parse_offsets(raw_offsets: str) -> list[float]:
    offsets = []
    for item in raw_offsets.split(","):
        item = item.strip()
        if item:
            offsets.append(float(item))
    return offsets or DEFAULT_SCREENSHOT_OFFSETS


def parse_srt_time(value: str) -> float:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    hours, minutes, seconds, milliseconds = [int(part) for part in match.groups()]
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    if milliseconds == 1000:
        secs += 1
        milliseconds = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def timestamp_for_filename(seconds: float) -> str:
    return format_timestamp(seconds).replace(":", "-").replace(".", "-")


def parse_srt(path: Path) -> list[SubtitleSegment]:
    content = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", content.strip())
    segments = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue

        if "-->" in lines[0]:
            index = len(segments) + 1
            time_line = lines[0]
            text_lines = lines[1:]
        else:
            try:
                index = int(lines[0])
            except ValueError:
                index = len(segments) + 1
            time_line = lines[1]
            text_lines = lines[2:]

        if "-->" not in time_line:
            continue
        start_raw, end_raw = [part.strip().split()[0] for part in time_line.split("-->", 1)]
        segments.append(
            SubtitleSegment(
                index=index,
                start=parse_srt_time(start_raw),
                end=parse_srt_time(end_raw),
                text=" ".join(text_lines),
            )
        )
    return segments


def find_srt_files(root: Path) -> list[Path]:
    if root.is_file():
        if root.suffix.lower() != ".srt":
            raise ValueError(f"Expected a .srt file or directory: {root}")
        return [root]
    return sorted(root.rglob("*.srt"))


def find_matching_video(srt_path: Path) -> Path | None:
    base_path = srt_path.with_suffix("")
    for extension in MEDIA_EXTENSIONS:
        video_path = base_path.with_suffix(extension)
        if video_path.exists():
            return video_path
    return None


def match_keywords(text: str, keywords: list[str], case_sensitive: bool) -> list[str]:
    if case_sensitive:
        return [keyword for keyword in keywords if keyword in text]
    lowered_text = text.lower()
    return [keyword for keyword in keywords if keyword.lower() in lowered_text]


def safe_stem(path: Path) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", path.stem).strip("_")


def capture_screenshot(video_path: Path, timestamp: float, output_path: Path) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        format_timestamp(timestamp),
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-y",
        str(output_path),
    ]
    result = subprocess.run(command)
    return result.returncode == 0 and output_path.exists()


def collect_hits(
    srt_files: list[Path],
    keywords: list[str],
    case_sensitive: bool,
) -> list[HomeworkHit]:
    hits = []
    for srt_path in srt_files:
        video_path = find_matching_video(srt_path)
        for segment in parse_srt(srt_path):
            matched = match_keywords(segment.text, keywords, case_sensitive)
            if matched:
                hits.append(
                    HomeworkHit(
                        video_path=video_path,
                        srt_path=srt_path,
                        segment=segment,
                        keywords=matched,
                        screenshots=[],
                    )
                )
    return hits


def add_screenshots(hits: list[HomeworkHit], output_dir: Path, offsets: list[float]) -> None:
    screenshot_dir = output_dir / "screenshots"
    for hit in hits:
        if not hit.video_path:
            continue
        video_stem = safe_stem(hit.video_path)
        for offset in offsets:
            timestamp = max(0.0, hit.segment.start + offset)
            screenshot_name = (
                f"{video_stem}_sub{hit.segment.index:04d}_{timestamp_for_filename(timestamp)}.jpg"
            )
            screenshot_path = screenshot_dir / screenshot_name
            if capture_screenshot(hit.video_path, timestamp, screenshot_path):
                hit.screenshots.append(screenshot_path)


def write_csv(hits: list[HomeworkHit], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "homework_hits.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "srt",
                "video",
                "subtitle_index",
                "start",
                "end",
                "keywords",
                "text",
                "screenshots",
            ]
        )
        for hit in hits:
            writer.writerow(
                [
                    str(hit.srt_path),
                    str(hit.video_path or ""),
                    hit.segment.index,
                    format_timestamp(hit.segment.start),
                    format_timestamp(hit.segment.end),
                    "|".join(hit.keywords),
                    hit.segment.text,
                    "|".join(str(path) for path in hit.screenshots),
                ]
            )
    return csv_path


def main():
    args = parse_args()
    root = Path(args.root)
    output_dir = Path(args.output)
    keywords = load_keywords(args)
    offsets = parse_offsets(args.screenshot_offsets)
    srt_files = find_srt_files(root)

    if not srt_files:
        print(f"No .srt files found under {root}")
        return

    hits = collect_hits(srt_files, keywords, args.case_sensitive)
    if not args.no_screenshots:
        add_screenshots(hits, output_dir, offsets)

    csv_path = write_csv(hits, output_dir)
    print(f"Scanned {len(srt_files)} subtitle file(s).")
    print(f"Found {len(hits)} hit(s) for keywords: {', '.join(keywords)}")
    print(f"Report: {csv_path}")


if __name__ == "__main__":
    main()
