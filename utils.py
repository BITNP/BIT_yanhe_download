import os
import re
import time
from collections import Counter
from hashlib import md5

import requests

# 在延河课堂网站的main.js中4937号的O[N(149, 270, 240, 274)]["k"]()函数的返回值
magic = "1138b69dfef641d9d7ba49137d2d4875"
headers = {
    "Origin": "https://www.yanhekt.cn",
    "Referer": "https://www.yanhekt.cn/",
    "xdomain-client": "web_user",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36 Edg/107.0.1418.26",
    "Xdomain-Client": "web_user",
    "Xclient-Signature": md5((magic + "_v1_undefined").encode()).hexdigest(),
    "Xclient-Version": "v1",
    "Xclient-Timestamp": str(int(time.time())),
    "Authorization": "",
}


def auth_prompt(code=True):
    return [
        "请先在浏览器登录延河课堂",
        "并在延河课堂的地址栏输入 javascript:alert(JSON.parse(localStorage.auth).token)",
        '注意粘贴时浏览器会自动去掉"javascript:"，需要手动补上',
        "或者按F12打开控制台粘贴这段代码",
        "然后将弹出的内容粘贴到" + ("这里：" if code else '"身份认证码"栏'),
    ]


def encryptURL(url: str) -> str:
    url_list = url.split("/")
    # "c3d47d7b3aa8caf2983b313cb6cd142f"
    url_list.insert(-1, md5((magic + "_100").encode()).hexdigest())
    return "/".join(url_list)


def getSignature():
    timestamp = str(int(time.time()))
    signature = md5((magic + "_v1_" + timestamp).encode()).hexdigest()
    return timestamp, signature


def getToken() -> str:
    req = requests.get(
        "https://cbiz.yanhekt.cn/v1/auth/video/token?id=0", headers=headers
    )
    # Example response: `{"code":0,"message":"","data":{"token":"12345678901234ab","expired_at":1742300867,"now":1742300267}}`
    data = req.json()["data"]
    if not data:
        read_auth()
        req = requests.get(
            "https://cbiz.yanhekt.cn/v1/auth/video/token?id=0", headers=headers
        )
        data = req.json()["data"]
        if not data:
            raise Exception("获取Token失败")
    return data["token"]


def add_signature_for_url(url: str, token: str, timestamp: str, signature: str) -> str:
    url = (
        url
        + "?Xvideo_Token="
        + token
        + "&Xclient_Timestamp="
        + timestamp
        + "&Xclient_Signature="
        + signature
        + "&Xclient_Version=v1&Platform=yhkt_user"
    )
    return url


def read_auth():
    if not os.path.exists("auth.txt"):
        return ""
    with open("auth.txt") as f:
        auth = f.read().strip()
        headers["Authorization"] = "Bearer " + auth
    return auth


def write_auth(auth):
    headers["Authorization"] = "Bearer " + auth
    with open("auth.txt", "w") as f:
        f.write(auth)


def remove_auth():
    headers["Authorization"] = ""
    if os.path.exists("auth.txt"):
        os.remove("auth.txt")


def test_auth(courseID):
    """
    Test if the auth in headers is valid.
    Return True if the auth is valid, otherwise False.
    """
    res = requests.get(
        f"https://cbiz.yanhekt.cn/v2/course/session/list?course_id={courseID}",
        headers=headers,
    )
    return bool(res.json()["data"])


def disambiguate_session_titles(sessions):
    """Add the start time only when multiple sessions have the same title."""
    title_counts = Counter(session.get("title", "") for session in sessions)
    used_titles = {
        session.get("title", "")
        for session in sessions
        if title_counts[session.get("title", "")] == 1
    }

    for session in sessions:
        title = session.get("title", "")
        if not title or title_counts[title] == 1:
            continue

        started_at = str(session.get("started_at") or "")
        match = re.search(r"[ T](\d{2}):(\d{2})", started_at)
        suffix = f"{match.group(1)}-{match.group(2)}" if match else ""
        if not suffix:
            suffix = f"session-{session.get('id', 'unknown')}"

        candidate = f"{title} ({suffix})"
        if candidate in used_titles:
            candidate = f"{candidate} [{session.get('id', 'unknown')}]"
        session["title"] = candidate
        used_titles.add(candidate)


def get_course_info(courseID):
    courseID = courseID.strip()

    course = requests.get(
        f"https://cbiz.yanhekt.cn/v1/course?id={courseID}&with_professor_badges=true",
        headers=headers,
    )
    res = requests.get(
        f"https://cbiz.yanhekt.cn/v2/course/session/list?course_id={courseID}",
        headers=headers,
    )

    if course.json()["code"] != "0" and course.json()["code"] != 0:
        # print(course.json()["code"])
        # print(course.json()["message"])
        raise Exception(
            f"courseID: {courseID}, {course.json()['message']}。请检查您的课程ID，注意它应该是5位数字，从课程信息界面的链接yanhekt.cn/course/***获取，而不是课程播放界面的链接yanhekt.cn/session/***"
        )
    # print(course.json()["data"]["name_zh"])
    videoList = res.json()["data"]
    disambiguate_session_titles(videoList)
    name = course.json()["data"]["name_zh"].strip()
    if not videoList:
        raise Exception(f"该课程({name})没有视频信息，请检查课程ID是否正确")

    return (
        videoList,
        name,
        (
            course.json()["data"]["professors"][0]["name"].strip()
            if course.json()["data"]["professors"]
            else "未知教师"
        ),
    )


def get_audio_url(video_id):
    res = requests.get(
        f"https://cbiz.yanhekt.cn/v1/video?id={video_id}",
        headers=headers,
    )
    return res.json()["data"].get("audio", "")


def download_audio(url, path, name):
    token = getToken()
    url = add_signature_for_url(url, token, *getSignature())
    _headers = headers.copy()
    _headers["Host"] = "cvideo.yanhekt.cn"
    res = requests.get(url, headers=_headers)
    while res.status_code != 200:
        time.sleep(0.1)
        res = requests.get(url, headers=_headers)
    with open(f"{path}/{name}.aac", "wb") as f:
        f.write(res.content)


def print_help(f: callable):
    def wrap():
        try:
            f()
        except Exception as e:
            print(e)
            print(
                "If the problem is still not solved, you can report an issue in https://github.com/AuYang261/BIT_yanhe_download/issues."
            )
            print(
                "Or contact with the author xu_jyang@163.com. Thanks for your report!"
            )
            print(
                "如果问题仍未解决，您可以在https://github.com/AuYang261/BIT_yanhe_download/issues 中报告问题。"
            )
            print("或者联系作者xu_jyang@163.com。感谢您的报告！")

    return wrap
