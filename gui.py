import curses
import os
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
    height, width = get_cmd_window_size(stdscr)
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
    height, width = get_cmd_window_size(stdscr)

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


class _LoginCancelled(utils.AuthError):
    """User cancellation must not trigger the SSO failure fallback."""


def _read_login_field(stdscr, prompt, *, secret=False):
    stdscr.clear()
    draw_line(stdscr, prompt, 0)
    draw_line(stdscr, "留空或按 Ctrl-C 取消登录", 1)
    stdscr.refresh()
    try:
        if secret:
            curses.noecho()
        else:
            curses.echo()
        value = stdscr.getstr(2, align).decode("utf-8")
        if not value or "\x03" in value:
            raise _LoginCancelled("登录已取消。")
        return value
    finally:
        curses.noecho()


def _show_login_status(stdscr):
    stdscr.clear()
    draw_line(stdscr, "正在登录，请稍候…", 0)
    stdscr.refresh()


def login_tui(stdscr):
    """Read credentials and any SMS challenge inside the active curses session."""
    try:
        curses.noecho()
        import sso_login

        username = os.environ.get("STUDENT_ID", "").strip()
        if not username:
            username = _read_login_field(stdscr, "请输入学号：").strip()
        if not username:
            raise _LoginCancelled("登录已取消。")
        password = os.environ.get("PASSWORD") or _read_login_field(
            stdscr, "请输入密码（不回显）：", secret=True
        )

        def read_code():
            code = _read_login_field(stdscr, "请输入短信验证码（不回显）：", secret=True)
            _show_login_status(stdscr)
            return code.strip()

        _show_login_status(stdscr)
        return sso_login.login(
            username, password, sms_code=os.environ.get("SMS_CODE") or None,
            code_provider=read_code,
        )
    except (KeyboardInterrupt, EOFError):
        raise _LoginCancelled("登录已取消。") from None
    finally:
        curses.noecho()


def token_login_tui(stdscr):
    """Read a hidden token; validation and persistence belong to ensure_auth."""
    token = _read_login_field(stdscr, "请输入身份认证码 token（不回显）：", secret=True).strip()
    if not token:
        raise _LoginCancelled("登录已取消。")
    return token


def login_prompt_tui(stdscr):
    """Choose token (default) or optional SSO when cached auth is unavailable."""
    try:
        curses.noecho()
        selected = 0
        while True:
            stdscr.clear()
            draw_line(stdscr, "请选择登录方式", 0)
            draw_line(stdscr, "上下键或 1/2 选择，Enter 确认；q/Esc 取消", 1)
            for index, option in enumerate(("1. 手动输入 token（默认）", "2. 学号密码 SSO 登录")):
                draw_line(stdscr, ("> " if index == selected else "  ") + option, index + 3)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), 27):
                raise _LoginCancelled("登录已取消。")
            if key in (curses.KEY_DOWN, curses.KEY_UP):
                selected = 1 - selected
            elif key in (ord("1"), ord("2")):
                selected = key - ord("1")
            elif key in (curses.KEY_ENTER, 10, 13):
                break

        if selected == 0:
            return token_login_tui(stdscr)
        try:
            return login_tui(stdscr)
        except _LoginCancelled:
            raise
        except (utils.AuthError, ImportError) as error:
            stdscr.clear()
            message = (
                "SSO 依赖不可用，请运行 uv sync --extra sso。"
                if isinstance(error, ImportError) else "SSO 登录失败。"
            )
            draw_line(stdscr, message, 0)
            draw_line(stdscr, "按 Enter 改用手动 token；q/Esc 取消", 1)
            stdscr.refresh()
            while True:
                key = stdscr.getch()
                if key in (ord("q"), 27):
                    raise _LoginCancelled("登录已取消。")
                if key in (curses.KEY_ENTER, 10, 13):
                    return token_login_tui(stdscr)
    except (KeyboardInterrupt, EOFError):
        raise _LoginCancelled("登录已取消。") from None
    except (OSError, curses.error, UnicodeError):
        raise utils.AuthError("登录输入不可用。") from None
    finally:
        curses.noecho()


def config(stdscr):
    global \
        videoList, \
        courseName, \
        professor, \
        selected_videos, \
        selected_signal, \
        download_audio

    height, width = get_cmd_window_size(stdscr)

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

    try:
        # 课程编号可回显，认证信息由 curses 内的登录提示读取。
        try:
            courseID = stdscr.getstr().decode("utf-8").strip()
        finally:
            curses.noecho()
        if not courseID or "\x03" in courseID:
            raise utils.AuthError("登录已取消。")
        utils.ensure_auth(lambda: login_prompt_tui(stdscr))
    except (utils.AuthError, KeyboardInterrupt, EOFError, OSError, curses.error, UnicodeError) as error:
        curses.noecho()
        stdscr.clear()
        message = str(error) if isinstance(error, utils.AuthError) else "登录已取消或输入不可用。"
        draw_line(stdscr, message, 0)
        draw_line(stdscr, "按任意键退出", 1)
        stdscr.refresh()
        try:
            stdscr.getch()
        except (KeyboardInterrupt, EOFError, curses.error):
            pass
        sys.exit(1)
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
