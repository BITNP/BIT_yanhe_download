import base64
import os
import queue
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from subprocess import run

import requests
import urllib3

import utils


class ThreadPoolExecutorWithQueueSizeLimit(ThreadPoolExecutor):
    """
    实现多线程有界队列
    队列数为线程数的2倍
    """

    def __init__(self, max_workers=None, *args, **kwargs):
        super().__init__(max_workers, *args, **kwargs)
        self._work_queue = queue.Queue(max_workers * 2)


def make_sum():
    ts_num = 0
    while True:
        yield ts_num
        ts_num += 1


def dummy_func(downloaded, total, merge_status):
    return


class M3u8Download:
    """
    :param url: 完整的m3u8文件链接 如"https://www.bilibili.com/example/index.m3u8"
    :param name: 保存m3u8的文件名 如"index"
    :param max_workers: 多线程最大线程数
    :param num_retries: 重试次数
    :param base64_key: base64编码的字符串
    """

    def __init__(
        self,
        url,
        workDir,
        name,
        max_workers=32,
        num_retries=32,
        base64_key=None,
        progress_callback=dummy_func,
    ):
        self._url = url
        self._token = None
        self._workDir = workDir
        self._name = name
        self._max_workers = max_workers
        self._num_retries = num_retries
        self._progress_callback = progress_callback
        if not os.path.exists(os.path.join(os.getcwd(), self._workDir)):
            os.makedirs(os.path.join(os.getcwd(), self._workDir))
        self._file_path = os.path.join(
            os.getcwd(),
            self._workDir,
            utils.sanitize_filename(self._name),
        )
        if os.path.exists(self._file_path + ".mp4"):
            print(f"File '{self._file_path}.mp4' already exists, skip download")
            self._progress_callback(100, 100, 2)
            return
        self._front_url = None
        self._ts_url_list = []
        self._success_sum = 0
        self._failed_ts = []
        self._success_lock = threading.Lock()
        self._stop_signature_update = threading.Event()
        self._signature_lock = threading.Lock()
        self._last_progress_time = time.time()
        self._ts_sum = 0
        self._key = base64.b64decode(base64_key.encode()) if base64_key else None
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.82 Safari/537.36 Edg/93.0.961.52",
            "Origin": "https://www.yanhekt.cn",
            "referer": "https://www.yanhekt.cn/",
        }
        self.timestamp, self.signature = utils.getSignature()
        urllib3.disable_warnings()

        self._url = utils.encryptURL(self._url)

        self.get_m3u8_info(self._url, self._num_retries)

        def signal_handler(sig, frame):
            print("Caught KeyboardInterrupt. Shutting down...")
            os._exit(1)

        signal.signal(signal.SIGINT, signal_handler)
        print(f"Downloading: {self._name}", f"Save path: {self._file_path}", sep="\n")

        signature_thread = threading.Thread(
            target=self.updateSignatureLoop,
            daemon=True,
        )
        signature_thread.start()
        monitor_thread = threading.Thread(
            target=self.printStallStatusLoop,
            daemon=True,
        )
        monitor_thread.start()
        with ThreadPoolExecutorWithQueueSizeLimit(self._max_workers) as pool:
            futures = []
            for k, ts_url in enumerate(self._ts_url_list):
                futures.append(
                    pool.submit(
                        self.download_ts,
                        ts_url,
                        # The `.ts` extension is mandatory for FFmpeg 7.1.1+.
                        # https://git.ffmpeg.org/gitweb/ffmpeg.git/commit/b753bac08f6881b2d3dea8f1ab84c81550f35897
                        # https://git.ffmpeg.org/gitweb/ffmpeg.git/commit/6c4e56f07d1a703435854f2156c881885f7798da
                        os.path.join(self._file_path, f"{k}.ts"),
                        self._num_retries,
                    )
                )
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    self._failed_ts.append(str(e))
                    print(f"\n{e}")
        self._stop_signature_update.set()
        signature_thread.join(timeout=1)
        monitor_thread.join(timeout=1)
        if self._success_sum == self._ts_sum:
            self._progress_callback(self._success_sum, self._ts_sum, 1)
            self.output_mp4()
            self.delete_file()
            print(f"Download successfully --> {self._name}")
            self._progress_callback(self._success_sum, self._ts_sum, 2)
        else:
            print(
                f"\nDownload incomplete: {self._success_sum}/{self._ts_sum}. "
                "Please rerun; existing good .ts files will be skipped."
            )
            if self._failed_ts:
                print("Failed segments:")
                for error in self._failed_ts[:8]:
                    print(f"- {error}")
                if len(self._failed_ts) > 8:
                    print(f"- ... and {len(self._failed_ts) - 8} more")

    def _print_progress(self) -> None:
        bar = "*" * (100 * self._success_sum // self._ts_sum // 4)
        sys.stdout.write(f"\r[{bar:<25}]({self._success_sum}/{self._ts_sum})")
        sys.stdout.flush()

    def updateSignatureLoop(self):
        while (
            not self._stop_signature_update.is_set()
            and self._success_sum != self._ts_sum
        ):
            new_ts, new_sig = utils.getSignature()
            # Assign the (timestamp, signature) pair atomically under the lock
            # so that download_ts / get_m3u8_info never observe a mismatched
            # pair (e.g. new timestamp with the previous signature), which
            # would produce an invalid signed URL.
            with self._signature_lock:
                self.timestamp, self.signature = new_ts, new_sig
            time.sleep(10)

    def _current_signature(self):
        """Return (timestamp, signature) as an internally-consistent snapshot."""
        with self._signature_lock:
            return self.timestamp, self.signature

    def printStallStatusLoop(self):
        while not self._stop_signature_update.is_set():
            time.sleep(15)
            if time.time() - self._last_progress_time < 30:
                continue
            part_files = []
            if os.path.exists(self._file_path):
                part_files = [
                    f
                    for f in os.listdir(self._file_path)
                    if f.endswith(".part")
                    and not os.path.exists(os.path.join(self._file_path, f[:-5]))
                ]
            part_files.sort(key=self._part_sort_key)
            preview = ", ".join(part_files[:8])
            more = "" if len(part_files) <= 8 else f", ... +{len(part_files) - 8}"
            print(
                f"\nStill working: {self._success_sum}/{self._ts_sum}, "
                f"active/incomplete parts: {len(part_files)}"
                f"{f' ({preview}{more})' if part_files else ''}"
            )

    @staticmethod
    def _part_sort_key(filename):
        prefix = filename.split(".", 1)[0]
        if prefix.isdigit():
            return 0, int(prefix)
        return 1, filename

    def get_m3u8_info(self, m3u8_url: str, num_retries: int) -> None:
        """
        获取m3u8信息
        """

        if not self._token:
            self._token = utils.getToken()
        token = self._token
        ts, sig = self._current_signature()
        url = utils.add_signature_for_url(m3u8_url, token, ts, sig)
        try:
            with requests.get(
                url, timeout=(3, 30), verify=False, headers=self._headers
            ) as res:
                if res.status_code != 200:
                    raise Exception(f"Failed to get m3u8 info: {res.status_code}")
                self._front_url = res.url.split(res.request.path_url)[0]
                if "EXT-X-STREAM-INF" in res.text:  # 判定为顶级M3U8文件
                    for line in res.text.split("\n"):
                        if "#" in line:
                            continue
                        elif line.startswith("http"):
                            self._url = line
                        elif line.startswith("/"):
                            self._url = self._front_url + line
                        else:
                            self._url = self._url.rsplit("/", 1)[0] + "/" + line
                    self.get_m3u8_info(self._url, self._num_retries)
                else:
                    m3u8_text_str = res.text
                    self.get_ts_url(m3u8_text_str)
        except Exception as e:
            print(e)
            if num_retries > 0:
                self.get_m3u8_info(m3u8_url, num_retries - 1)
            else:
                raise RuntimeError(
                    f"Failed to get m3u8 info after retries: {m3u8_url}"
                ) from e

    def get_ts_url(self, m3u8_text_str: str) -> None:
        """
        获取每一个ts文件的链接
        """
        if not os.path.exists(self._file_path):
            os.mkdir(self._file_path)
        new_m3u8_str = ""
        ts = make_sum()
        for line in m3u8_text_str.split("\n"):
            if "#" in line:
                if "EXT-X-KEY" in line and "URI=" in line:
                    if os.path.exists(os.path.join(self._file_path, "key")):
                        continue
                    key = self.download_key(line, 5)
                    if key:
                        new_m3u8_str += f"{key}\n"
                        continue
                new_m3u8_str += f"{line}\n"
                if "EXT-X-ENDLIST" in line:
                    break
            else:
                if line.startswith("http"):
                    self._ts_url_list.append(line)
                elif line.startswith("/"):
                    self._ts_url_list.append(self._front_url + line)
                else:
                    self._ts_url_list.append(self._url.rsplit("/", 1)[0] + "/" + line)
                new_m3u8_str += os.path.join(self._file_path, f"{next(ts)}.ts") + "\n"
        self._ts_sum = next(ts)
        with open(self._file_path + ".m3u8", "wb") as f:
            f.write(new_m3u8_str.encode("utf-8"))

    def download_ts(self, ts_url_original: str, name: str, num_retries: int) -> None:
        """
        下载 .ts 文件
        """
        if os.path.exists(name):
            tmp_name = name + ".part"
            # A stale .part may be left from a previous interrupted run.  If
            # the final .ts already exists, the .part is not needed and would
            # only make the "Still working" monitor look confusing.
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
            # Resume/skip existing segment: still advance progress display.
            with self._success_lock:
                self._success_sum += 1
                self._last_progress_time = time.time()
                self._print_progress()
            self._progress_callback(self._success_sum, self._ts_sum, 0)
            return

        tmp_name = name + ".part"
        if os.path.exists(tmp_name):
            os.remove(tmp_name)

        last_error = None
        for attempt in range(num_retries + 1):
            try:
                if not self._token:
                    self._token = utils.getToken()
                token = self._token
                # Build the signed URL immediately before each retry.  A stale
                # signature is one of the common reasons Yanhe segment downloads
                # appear to "stop" after running for a while.  The (ts, sig)
                # snapshot below must come from the lock-protected helper so we
                # never mix a new timestamp with the previous signature.
                ts, sig = self._current_signature()
                ts_url = utils.add_signature_for_url(
                    ts_url_original.split("\n")[0],
                    token,
                    ts,
                    sig,
                )
                with requests.get(
                    ts_url,
                    stream=True,
                    timeout=(5, 60),
                    verify=False,
                    headers=self._headers,
                ) as res:
                    if res.status_code != 200:
                        if res.status_code in (401, 403):
                            self._token = utils.getToken()
                        raise Exception(f"HTTP {res.status_code}")
                    written = 0
                    expected = int(res.headers.get("Content-Length") or 0)
                    with open(tmp_name, "wb") as ts:
                        for chunk in res.iter_content(chunk_size=1024):
                            if chunk:
                                ts.write(chunk)
                                written += len(chunk)
                    if expected and written != expected:
                        raise Exception(
                            f"Incomplete segment ({written}/{expected} bytes)"
                        )
                    os.replace(tmp_name, name)
                break
            except Exception as e:
                last_error = e
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
                if attempt < num_retries:
                    time.sleep(min(5, 0.2 * (attempt + 1)))
        else:
            raise RuntimeError(f"Failed to download {name}: {last_error}")

        # Reporting failures must not roll back a completed segment or make
        # the success counter advance more than once.
        with self._success_lock:
            self._success_sum += 1
            self._last_progress_time = time.time()
            self._print_progress()
        self._progress_callback(self._success_sum, self._ts_sum, 0)

    def download_key(self, key_line, num_retries):
        """
        下载key文件
        """
        mid_part = re.search(r"URI=[\'|\"].*?[\'|\"]", key_line).group()
        may_key_url = mid_part[5:-1]
        if self._key:
            with open(os.path.join(self._file_path, "key"), "wb") as f:
                f.write(self._key)
            return f'{key_line.split(mid_part)[0]}URI="./{self._name}/key"'
        if may_key_url.startswith("http"):
            true_key_url = may_key_url
        elif may_key_url.startswith("/"):
            true_key_url = self._front_url + may_key_url
        else:
            true_key_url = self._url.rsplit("/", 1)[0] + "/" + may_key_url
        try:
            with (
                requests.get(
                    true_key_url, timeout=(5, 30), verify=False, headers=self._headers
                ) as res,
                open(os.path.join(self._file_path, "key"), "wb") as f,
            ):
                f.write(res.content)
            return f'{key_line.split(mid_part)[0]}URI="./{self._name}/key"{key_line.split(mid_part)[-1]}'
        except Exception as e:
            print(e)
            if os.path.exists(os.path.join(self._file_path, "key")):
                os.remove(os.path.join(self._file_path, "key"))
            print("加密视频,无法加载key,解密失败")
            if num_retries > 0:
                self.download_key(key_line, num_retries - 1)

    def output_mp4(self) -> None:
        """
        合并.ts文件，输出mp4格式视频，需要ffmpeg
        """
        output_file = f"{self._file_path}.mp4"
        tmp_output_file = f"{output_file}.part"
        if os.path.exists(tmp_output_file):
            os.remove(tmp_output_file)
        cmd = [
            "ffmpeg",
            "-y",
            *("-i", f"{self._file_path}.m3u8"),
            *("-acodec", "copy"),
            *("-vcodec", "copy"),
            *("-f", "mp4"),
            tmp_output_file,
        ]
        fallback_cmd = [
            "ffmpeg",
            "-y",
            # Some Yanhe segments occasionally contain a few corrupt TS packets.
            # Dropping those packets is preferable to failing the whole merge.
            *("-fflags", "+discardcorrupt"),
            *("-err_detect", "ignore_err"),
            *("-i", f"{self._file_path}.m3u8"),
            *("-acodec", "copy"),
            *("-vcodec", "copy"),
            *("-f", "mp4"),
            tmp_output_file,
        ]
        try:
            run(
                cmd,
                check=True,
            )
        except Exception as e:
            print(f"Normal ffmpeg merge failed, retry with corrupt-packet discard: {e}")
            if os.path.exists(tmp_output_file):
                os.remove(tmp_output_file)
            run(
                fallback_cmd,
                check=True,
            )
        try:
            os.replace(tmp_output_file, output_file)
        except Exception:
            if os.path.exists(tmp_output_file):
                os.remove(tmp_output_file)
            raise

    def delete_file(self):
        file = os.listdir(self._file_path)
        for item in file:
            os.remove(os.path.join(self._file_path, item))
        os.removedirs(self._file_path)
        os.remove(self._file_path + ".m3u8")
