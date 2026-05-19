import argparse
import csv
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_KEYWORDS = ["作业"]
DEFAULT_SCREENSHOT_OFFSETS = [0.0]
DEFAULT_MERGE_GAP = 60.0
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


@dataclass
class HomeworkEvent:
    video_path: Path | None
    srt_path: Path
    hits: list[HomeworkHit]
    screenshots: list[Path]

    @property
    def start(self) -> float:
        return self.hits[0].segment.start

    @property
    def end(self) -> float:
        return max(hit.segment.end for hit in self.hits)

    @property
    def keywords(self) -> list[str]:
        seen = set()
        keywords = []
        for hit in self.hits:
            for keyword in hit.keywords:
                if keyword not in seen:
                    seen.add(keyword)
                    keywords.append(keyword)
        return keywords

    @property
    def text(self) -> str:
        return " ".join(hit.segment.text for hit in self.hits)


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
        help="Comma-separated seconds relative to each matched event start, e.g. -10,0,10.",
    )
    parser.add_argument(
        "--merge-gap",
        type=float,
        default=DEFAULT_MERGE_GAP,
        help="Merge hits in the same video when the next hit starts within this many seconds. Defaults to 60. Use 0 to disable.",
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
                    )
                )
    return hits


def merge_hits(hits: list[HomeworkHit], merge_gap: float) -> list[HomeworkEvent]:
    events = []
    current_event = None
    sorted_hits = sorted(
        hits,
        key=lambda hit: (str(hit.srt_path), hit.segment.start, hit.segment.index),
    )

    for hit in sorted_hits:
        if (
            current_event is None
            or current_event.srt_path != hit.srt_path
            or hit.segment.start - current_event.end > merge_gap
        ):
            current_event = HomeworkEvent(
                video_path=hit.video_path,
                srt_path=hit.srt_path,
                hits=[hit],
                screenshots=[],
            )
            events.append(current_event)
        else:
            current_event.hits.append(hit)

    return events


def add_screenshots(
    events: list[HomeworkEvent], output_dir: Path, offsets: list[float]
) -> None:
    screenshot_dir = output_dir / "screenshots"
    for event_index, event in enumerate(events, 1):
        if not event.video_path:
            continue
        video_stem = safe_stem(event.video_path)
        for offset in offsets:
            timestamp = max(0.0, event.start + offset)
            screenshot_name = (
                f"{video_stem}_event{event_index:04d}_{timestamp_for_filename(timestamp)}.jpg"
            )
            screenshot_path = screenshot_dir / screenshot_name
            if capture_screenshot(event.video_path, timestamp, screenshot_path):
                event.screenshots.append(screenshot_path)


def write_csv(events: list[HomeworkEvent], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "homework_hits.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "srt",
                "video",
                "start",
                "end",
                "hit_count",
                "subtitle_indexes",
                "keywords",
                "text",
                "screenshots",
            ]
        )
        for event in events:
            writer.writerow(
                [
                    str(event.srt_path),
                    str(event.video_path or ""),
                    format_timestamp(event.start),
                    format_timestamp(event.end),
                    len(event.hits),
                    "|".join(str(hit.segment.index) for hit in event.hits),
                    "|".join(event.keywords),
                    event.text,
                    "|".join(str(path) for path in event.screenshots),
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
    events = merge_hits(hits, args.merge_gap)
    if not args.no_screenshots:
        add_screenshots(events, output_dir, offsets)

    csv_path = write_csv(events, output_dir)
    print(f"Scanned {len(srt_files)} subtitle file(s).")
    print(f"Found {len(hits)} hit(s) in {len(events)} event(s) for keywords: {', '.join(keywords)}")
    print(f"Report: {csv_path}")


if __name__ == "__main__":
    main()
