import argparse
import os
import re
import subprocess
import time

import whisper
from zhconv import convert  # 简繁体转换

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm"}
AUDIO_EXTS = {".aac", ".m4a", ".mp3", ".wav", ".flac", ".ogg"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS


def seconds_to_hmsm(seconds):
    """
    输入一个秒数，输出为 H:M:S,M 时间格式
    @params:
        seconds   - Required  : 秒 (float)
    """
    hours = str(int(seconds // 3600))
    minutes = str(int((seconds % 3600) // 60))
    seconds = seconds % 60
    milliseconds = str(int((seconds - int(seconds)) * 1000))  # 毫秒留三位
    seconds = str(int(seconds))
    # 补0
    if len(hours) < 2:
        hours = "0" + hours
    if len(minutes) < 2:
        minutes = "0" + minutes
    if len(seconds) < 2:
        seconds = "0" + seconds
    if len(milliseconds) < 3:
        milliseconds = "0" * (3 - len(milliseconds)) + milliseconds
    return f"{hours}:{minutes}:{seconds},{milliseconds}"


def is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def is_audio(path):
    return os.path.splitext(path)[1].lower() in AUDIO_EXTS


def find_media_files(path="."):
    """
    path 可以是单个音视频文件，也可以是目录。
    目录会递归查找其中所有支持的音视频文件。
    """
    path = os.path.abspath(path)
    if os.path.isfile(path):
        return [path] if os.path.splitext(path)[1].lower() in MEDIA_EXTS else []
    if not os.path.isdir(path):
        return []

    files = []
    for dirpath, _, filenames in os.walk(path):
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext in MEDIA_EXTS:
                files.append(os.path.join(dirpath, filename))
    return sorted(files)


def _parse_int_list(text):
    """Parse a comma/whitespace-separated list of 1-based indices into 0-based ints.

    Rejects anything non-numeric to avoid executing arbitrary expressions
    (the previous implementation used eval() on user input).
    """
    out = []
    for tok in re.split(r"[\s,]+", text.strip()):
        if not tok:
            continue
        if not tok.lstrip("-").isdigit():
            raise ValueError(f"not an integer: {tok!r}")
        out.append(int(tok) - 1)
    return out


def select_files_interactively():
    files = find_media_files(".")
    if not files:
        raise FileNotFoundError("当前目录下没有找到支持的音视频文件")

    for i, f in enumerate(files):
        print(f"[{i}]: {f}")
    raw = input("select media files by input num(split with ','): ")
    try:
        selected_idx = _parse_int_list(raw)
    except ValueError as e:
        raise ValueError(f"invalid input {raw!r}: {e}") from None
    selected = [files[i] for i in selected_idx]
    print("selected media files:", selected)
    return selected


def select_model_interactively():
    models = []
    for model in whisper.available_models():
        if ".en" in model:
            continue
        print(f"[{len(models)}]: {model}")
        models.append(model)
    raw = input("select a model by input a num(default 'base'): ")
    if not raw.strip():
        return "base"
    try:
        idx = _parse_int_list(raw)[0]
        return models[idx]
    except ValueError, IndexError:
        return "base"


def extract_audio(video_path, audio_path):
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-vn",
            "-ar",
            str(whisper.audio.SAMPLE_RATE),
            audio_path,
        ],
        check=True,
    )


def write_srt(result, srt_path):
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, r in enumerate(result["segments"], start=1):
            f.write(str(i) + "\n")
            f.write(
                seconds_to_hmsm(float(r["start"]))
                + " --> "
                + seconds_to_hmsm(float(r["end"]))
                + "\n"
            )
            f.write(convert(r["text"], "zh-cn") + "\n\n")


def transcribe_media(media_path, model):
    base_path, ext = os.path.splitext(media_path)
    srt_path = base_path + ".srt"
    temp_audio_path = base_path + ".whisper.m4a"
    audio_for_whisper = media_path

    print(f"\nInput: {media_path}")
    print(f"Output srt: {srt_path}")

    try:
        if is_video(media_path):
            # 视频需要先抽取音频；音频文件则直接交给 Whisper。
            print(f"Audio temp: {temp_audio_path}")
            extract_audio(media_path, temp_audio_path)
            audio_for_whisper = temp_audio_path
        elif not is_audio(media_path):
            print(f"Skip unsupported file: {media_path}")
            return

        start = time.time()
        result = model.transcribe(audio_for_whisper, verbose=False, language="zh")
        print("Time cost: ", time.time() - start)

        write_srt(result, srt_path)
    finally:
        if os.path.exists(temp_audio_path):
            os.remove(temp_audio_path)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate .srt captions for video/audio files."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=(
            "音视频文件或目录；目录会递归处理 mp4/mov/mkv/avi/flv/webm "
            "以及 aac/m4a/mp3/wav/flac/ogg。不传则交互选择。"
        ),
    )
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help="Whisper 模型名，例如 tiny/base/small/medium/large。默认交互选择或 base。",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.paths:
        media_paths = []
        for path in args.paths:
            media_paths.extend(find_media_files(path))
        if not media_paths:
            raise FileNotFoundError("传入的路径里没有找到支持的音视频文件")
        model_name = args.model or "base"
    else:
        media_paths = select_files_interactively()
        model_name = args.model or select_model_interactively()

    print("selected model:", model_name)
    model = whisper.load_model(model_name, download_root="whisper_models/")

    for media_path in media_paths:
        transcribe_media(media_path, model)


if __name__ == "__main__":
    main()
