import os
import sys
import time

import whisper
from zhconv import convert  # 简繁体转换


def seconds_to_hmsm(seconds):
    """
    输入一个秒数，输出为H:M:S:M时间格式
    @params:
        seconds   - Required  : 秒 (float)
    """
    hours = str(int(seconds // 3600))
    minutes = str(int((seconds % 3600) // 60))
    seconds = seconds % 60
    milliseconds = str(int(int((seconds - int(seconds)) * 1000)))  # 毫秒留三位
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


def main():
    # 视频文件路径
    video_paths = []
    media_extensions = (".mp4", ".aac")
    if len(sys.argv) >= 2:
        video_paths.append(sys.argv[1])
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk("."):
            for filename in filenames:
                if filename.endswith(media_extensions):
                    files.append(os.path.join(dirpath, filename).replace("\\", "/"))
        for i, f in enumerate(files):
            print(f"[{i}]: ", f)
        input_list = eval(
            "[" + input("select a media file by input a num(split with ','): ") + "]"
        )
        for i in input_list:
            video_paths.append(files[i])
        print("selected video files:", video_paths)
        models = []
        for model in whisper.available_models():
            if ".en" in model:
                continue
            print(f"[{len(models)}]: ", model)
            models.append(model)
        model_index = input("select a model by input a num(default 'base'): ")
        try:
            model_name = models[eval(model_index)]
        except Exception:
            model_name = "base"
        print("selected model:", model_name)

    for video_path in video_paths:
        base_path, ext = os.path.splitext(video_path)
        audio_path = video_path
        if ext.lower() == ".mp4":
            audio_path = base_path + ".m4a"
            cmd = f'ffmpeg -i "{video_path}" -vn -ar {whisper.audio.SAMPLE_RATE} "{audio_path}"'
            os.system(cmd)

        model = whisper.load_model(model_name, download_root="whisper_models/")

        start = time.time()
        result = model.transcribe(audio_path, verbose=False, language="zh")
        print("Time cost: ", time.time() - start)

        # 写入字幕文件
        with open(base_path + ".srt", "w", encoding="utf-8") as f:
            i = 1
            for r in result["segments"]:
                f.write(str(i) + "\n")
                f.write(
                    seconds_to_hmsm(float(r["start"]))
                    + " --> "
                    + seconds_to_hmsm(float(r["end"]))
                    + "\n"
                )
                i += 1
                f.write(
                    convert(r["text"], "zh-cn") + "\n"
                )  # 结果可能是繁体，转为简体zh-cn
                f.write("\n")

        # 删除音频文件
        if audio_path != video_path:
            os.remove(audio_path)


if __name__ == "__main__":
    main()
