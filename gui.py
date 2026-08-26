import curses
import sys

import m3u8dl
import utils

videoList = []
courseName = ""
professor = ""
selected_videos = []
selected_signal = []
download_audio = []

align = 0


class Row:
    def __init__(self, text, highlighted=False):
        self.text = text
        self.highlighted = highlighted


def draw_line(stdscr, text, row):
    # 在每个中文字符后插入一个空格，以解决中文字符宽度问题
    new_text = ""
    for c in text:
        new_text += c
        if ord(c) > 127:
            new_text += " "
    stdscr.addnstr(row, align, new_text, get_cmd_window_size(stdscr)[1])


def draw_menu(stdscr, options, checked, title, subtitle, current_row):
    stdscr.clear()
    height, _width = get_cmd_window_size(stdscr)
    draw_line(stdscr, title, 0)
    draw_line(stdscr, subtitle, 1)
    msg = []
    for idx, option in enumerate(options):
        checkmark = "[X]" if checked[idx] else "[ ]"
        msg.append(Row(f"{checkmark} {option}", idx == current_row))
    draw_multi_select(stdscr, msg, current_row)
    draw_line(stdscr, "按上下键移动，按空格键选择/取消选择", height - 2)
    draw_line(stdscr, "按回车键确认，按q键退出", height - 1)
    stdscr.refresh()


def draw_multi_select(stdscr, messages: list, center_row):
    # 获取屏幕的行数和列数
    height, _width = get_cmd_window_size(stdscr)

    # 计算消息的开始位置以使其居中
    total_messages = len(messages)
    visible_messages = min(height - 5, total_messages)  # 屏幕可以显示的最大消息数
    start_row = max(2, (height // 2) - (visible_messages // 2))

    # 确定要显示的消息的范围
    start_index = min(
        max(0, center_row - (visible_messages // 2)), total_messages - visible_messages
    )
    end_index = min(total_messages, start_index + visible_messages)

    for i in range(start_index, end_index):
        message = messages[i]
        draw_line(
            stdscr,
            message.text + (" <=" if message.highlighted else ""),
            start_row + (i - start_index),
        )


def multi_select(stdscr, options, title, subtitle="", checked=None):
    # curses.curs_set(0)  # 隐藏光标
    if checked is None:
        checked = [False] * len(options)
    else:
        checked = [bool(c) for c in checked]
    current_row = 0
    while True:
        draw_menu(stdscr, options, checked, title, subtitle, current_row)
        key = stdscr.getch()

        if key == curses.KEY_DOWN:
            current_row = (current_row + 1) % len(options)  # 向下循环移动
        elif key == curses.KEY_UP:
            current_row = (current_row - 1) % len(options)  # 向上循环移动
        elif key == ord("q"):
            sys.exit()  # 退出程序
        elif key == ord(" "):
            checked[current_row] = not checked[current_row]  # 切换当前行的勾选状态
        elif key == curses.KEY_ENTER or key in [10, 13]:
            break

    # 获取选择
    return [i for i, c in enumerate(checked) if c]


def config(stdscr):
    global \
        videoList, \
        courseName, \
        professor, \
        selected_videos, \
        selected_signal, \
        download_audio

    # 开启回显
    curses.echo()
    # 设置背景色
    curses.start_color()
    # 设置颜色对
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)
    # 设置窗口
    stdscr.clear()
    stdscr.refresh()

    # stdscr.border(0)
    # 提示用户输入
    url_base = "https://www.yanhekt.cn/course/"

    draw_line(stdscr, "请输入课程编号(回车退出):", 0)

    draw_line(stdscr, f"{url_base}", 1)

    # 等待用户输入字符串并显示它
    courseID = stdscr.getstr().decode("utf-8")
    if not courseID:
        sys.exit()

    if not utils.read_auth() or not utils.test_auth(courseID=courseID):
        stdscr.clear()
        for i, line in enumerate(utils.auth_prompt()):
            draw_line(stdscr, line, i)
        auth = stdscr.getstr().decode("utf-8")
        utils.write_auth(auth)
        if not utils.test_auth(courseID=courseID):
            stdscr.clear()
            draw_line(stdscr, "身份验证失败", 0)
            stdscr.getch()
            sys.exit()
    videoList, courseName, professor = utils.get_course_info(courseID=courseID)

    selected_videos = []

    while True:
        selected_videos = multi_select(
            stdscr,
            [v["title"] for v in videoList],
            f"课程名：{courseName}，请选择要下载的视频:",
        )
        if not selected_videos:
            stdscr.clear()
            draw_line(stdscr, "请至少选择一个视频，按回车继续", 0)
            stdscr.getch()
        else:
            break

    selected_signal = []

    while True:
        selected_signal = multi_select(
            stdscr, ["摄像头", "电脑屏幕"], "选择要下载的信号："
        )
        if not selected_signal:
            stdscr.clear()
            draw_line(stdscr, "请至少选择一个信号，按回车继续", 0)
            stdscr.getch()
        else:
            break

    download_audio = multi_select(
        stdscr,
        ["下载蓝牙音频"],
        "选择是否下载教室蓝牙话筒的音频（如果有的话）：",
        "若教师未使用教室蓝牙话筒则该音频无声音",
        checked=[True],
    )

    stdscr.clear()


def get_cmd_window_size(stdscr):
    return stdscr.getmaxyx()


@utils.print_help
def main():
    global align
    align = 25
    curses.wrapper(config)

    fail = []
    for i in selected_videos:
        c = videoList[i]
        name = courseName + "-" + professor + "-" + c["title"]
        print(name)
        try:
            if 1 in selected_signal:
                path = f"output/{courseName}-screen"
                m3u8dl.M3u8Download(c["videos"][0]["vga"], path, name)
            if 0 in selected_signal:
                path = f"output/{courseName}-video"
                m3u8dl.M3u8Download(c["videos"][0]["main"], path, name)
            if download_audio:
                audio_url = utils.get_audio_url(c["video_ids"][0])
                if audio_url:
                    print("Downloading audio...")
                    utils.download_audio(audio_url, path, name)
                    print("Download audio successfully.")
        except Exception as e:
            print(e)
            fail.append(name)
            input(f"下载{name}失败，按回车键开始下一个")
    if fail:
        print("以下视频下载失败：")
        for f in fail:
            print(f)
        input("按回车键退出")
    else:
        input("下载结束，按回车键退出")


if __name__ == "__main__":
    main()
