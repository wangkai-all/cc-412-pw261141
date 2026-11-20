"""异常检测：滚动中位数 ± k·MAD（稳健 z 分数）。纯确定性，无外部模型。"""
from datetime import date, timedelta
from statistics import median

WINDOW = 14          # 历史窗口
K = 4.0              # 稳健 z 阈值
MIN_HISTORY = 10


def _parse_window(w: str):
    a, b = w.split("..")
    return a, b


def detect_anomalies(series: list[dict]) -> list[dict]:
    """series: [{day, rate}] 按天升序。返回异常事件列表（连续越界合并为一个事件）。"""
    flags = []
    for i in range(MIN_HISTORY, len(series)):
        lo = max(0, i - WINDOW)
        hist = [series[j]["rate"] for j in range(lo, i)]
        med = median(hist)
        mad = median([abs(x - med) for x in hist]) * 1.4826
        if mad <= 1e-9:
            continue
        z = (series[i]["rate"] - med) / mad
        if abs(z) > K:
            flags.append({"day": series[i]["day"], "delta": series[i]["rate"] - med, "z": z})
    if not flags:
        return []
    # 连续越界合并为一个事件，锚定在首个越界日
    events = []
    group = [flags[0]]
    for f in flags[1:]:
        prev = date.fromisoformat(group[-1]["day"])
        if date.fromisoformat(f["day"]) - prev <= timedelta(days=1):
            group.append(f)
        else:
            events.append(group)
            group = [f]
    events.append(group)

    out = []
    for g in events:
        anchor = date.fromisoformat(g[0]["day"])
        pre_start = anchor - timedelta(days=7)
        post_end = anchor + timedelta(days=6)
        last_day = date.fromisoformat(series[-1]["day"])
        if post_end > last_day:
            post_end = last_day
        delta = min(g, key=lambda f: f["delta"])["delta"]  # 取组内最大跌幅
        out.append({
            "day": g[0]["day"],
            "pre_window": f"{pre_start.isoformat()}..{(anchor - timedelta(days=1)).isoformat()}",
            "post_window": f"{anchor.isoformat()}..{post_end.isoformat()}",
            "delta": delta,
        })
    return out


def window_days(window: str) -> list[str]:
    a, b = _parse_window(window)
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    days = []
    d = da
    while d <= db:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days
