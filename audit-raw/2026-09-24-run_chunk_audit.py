import json, subprocess, sys, io, os

SRC = r"C:/Users/Administrator/Desktop/stock-monitor/scripts/price_alert.py"
WORKERS = ["node-a:agnes-2.5-flash", "node-b:kilo-auto/free"]

# 真实文件行号（1-indexed, inclusive）切块
CHUNKS = [
    ("1-source-tracker+TDX", 39, 306),
    ("2-parse+single-quote", 307, 419),
    ("3-tencent+sina-batch", 420, 566),
    ("4-sqt+eastmoney-his", 567, 694),
    ("5-eastmoney+get_quotes_batch", 695, 847),
    ("6-kline", 848, 988),
    ("7-check_single/multi/main", 989, 1244),
]

with io.open(SRC, "r", encoding="utf-8") as f:
    lines = f.readlines()


def render(a, b):
    out = []
    for i in range(a - 1, min(b, len(lines))):
        out.append(f"{i+1:>4}  {lines[i].rstrip()}")
    return "\n".join(out)


INSTR = (
    "你在审计一个已过两轮修复、刚把 _tdx_lock 改成 RLock 修掉死锁的 Python 数据层文件的**某一段**（price_alert.py）。"
    "只报这段里**真实存在**的高/中危缺陷：崩溃(IndexError/KeyError/TypeError/AttributeError/未捕获异常)、"
    "死锁/并发竞态、除零、资源泄漏(连接/句柄/线程)、无界增长、明显的单位/涨跌/解析逻辑错误。\n"
    "不要报风格/命名/注释问题；不要复述防御已到位的代码；行号用我给的**真实行号**。\n"
    "每条一行：[高|中] 函数名:行号 —— 问题 | 证据代码 | 修复建议。最多5条，按严重度。确无则只写「无」。\n\n"
)

env = dict(os.environ, LLM_TIMEOUT="200", COLLAB_MAX_WORKERS=str(len(WORKERS)))

total = len(CHUNKS)
for idx, (name, a, b) in enumerate(CHUNKS, 1):
    code = render(a, b)
    prompt = f"{INSTR}===== price_alert.py 第 {a}-{b} 行 =====\n{code}\n===== 段结束 ====="
    payload = {"cmd": "ask_many", "workers": WORKERS, "prompt": prompt}
    proc = subprocess.run(
        ["uv", "run", "mao", "call", "ask_many"],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8",
        cwd=r"C:/Users/Administrator/Desktop/MAO", timeout=680, env=env,
    )
    print(f"\n\n############## CHUNK {idx}/{total}: {name} (lines {a}-{b}) ##############")
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        times = []
        for w in data.get("workers", []):
            body = w["answer"] if w["ok"] else f"<{w['status']}>"
            print(f"----- {w['worker']} [{w['status']}] {w.get('elapsed_ms')}ms -----")
            print(body.strip())
            times.append(f"{w['worker'].split(':')[-1]}={w.get('elapsed_ms')}ms")
        print(f">>> SPEED | {name} | " + " | ".join(times))
    except Exception as e:
        print(f"[parse fail: {e}] raw:", proc.stdout[-1500:], proc.stderr[-500:])
    sys.stdout.flush()
print("\nALL_CHUNKS_DONE")
