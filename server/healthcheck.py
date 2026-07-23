#!/usr/bin/env python3
"""Agent Console 健康检查脚本。

检查两件事：
  1. 进程存活 —— 本机是否有 `uvicorn server.main:app` 进程在跑（仅当探测的是本机/localhost 时才查，
     探测远程地址时跳过，只做 HTTP 探活）。
  2. HTTP 探活 —— 请求一个已存在的鉴权接口（默认 /api/tasks，带 AUTH_TOKEN），200 视为正常。

用法：
  python3 server/healthcheck.py                       # 用 server/config.py 里的 HOST/PORT/AUTH_TOKEN
  python3 server/healthcheck.py --host 127.0.0.1 --port 8800 --token 123
  HOST=0.0.0.0 PORT=8800 AUTH_TOKEN=123 python3 server/healthcheck.py   # 环境变量同样生效（config.py 本就读它们）

退出码：0 = 正常；1 = HTTP 探活失败；2 = 进程未存活（仅本机检查时才可能返回）。
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from server import config  # noqa: E402


def check_process() -> bool:
    """本机是否有 uvicorn server.main:app 进程在跑。"""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "uvicorn server.main:app"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def check_http(host: str, port: int, token: str, path: str, timeout: float, https: bool) -> tuple:
    """探活一个已存在的鉴权接口，返回 (是否正常, 说明文字)。"""
    scheme = "https" if https else "http"
    url = f"{scheme}://{host}:{port}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        ctx = None
        if https:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status == 200:
                json.loads(resp.read())  # 确认返回体是合法 JSON，不只是状态码对
                return True, f"{url} -> {resp.status}"
            return False, f"{url} -> {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"{url} -> HTTP {e.code}"
    except Exception as e:
        return False, f"{url} -> {e}"


def main():
    parser = argparse.ArgumentParser(description="Agent Console 健康检查")
    parser.add_argument("--host", default="127.0.0.1", help="探测的地址（默认 127.0.0.1，注意 config.HOST=0.0.0.0 是监听地址不是可探测地址）")
    parser.add_argument("--port", type=int, default=config.PORT, help=f"探测端口（默认取 config.PORT={config.PORT}）")
    parser.add_argument("--token", default=config.AUTH_TOKEN, help="AUTH_TOKEN（默认取 config.AUTH_TOKEN）")
    parser.add_argument("--path", default="/api/tasks", help="用于探活的鉴权接口路径（默认 /api/tasks）")
    parser.add_argument("--https", action="store_true", help="用 https 探测（自签证书场景，如 start_dual.py 的 8800 端口）")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP 请求超时秒数（默认 5）")
    parser.add_argument("--skip-process-check", action="store_true", help="跳过本机进程存活检查（探测远程实例时用）")
    args = parser.parse_args()

    ok = True
    is_local = args.host in ("127.0.0.1", "localhost", "0.0.0.0")

    if is_local and not args.skip_process_check:
        if check_process():
            print("[OK]   进程存活：找到 uvicorn server.main:app 进程")
        else:
            print("[FAIL] 进程存活：未找到 uvicorn server.main:app 进程")
            ok = False
            print("FAIL")
            sys.exit(2)

    http_ok, detail = check_http(args.host, args.port, args.token, args.path, args.timeout, args.https)
    if http_ok:
        print(f"[OK]   HTTP 探活：{detail}")
    else:
        print(f"[FAIL] HTTP 探活：{detail}")
        ok = False

    print("OK" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
