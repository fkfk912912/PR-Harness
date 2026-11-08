"""Run PR Harness and a GitHub-only Quick Tunnel for one repository."""

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pr_harness.core.config import Settings
from pr_harness.tooling.github import verify_signature

STATE_DIR = ROOT / "validation" / "github-webhook"
STATE = STATE_DIR / "status.json"
STOP = STATE_DIR / "stop.request"
WEBHOOK_PATH = "/webhooks/github"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def gh_api(path, method="GET", body=None):
    command = ["gh", "api", "--hostname", "github.com", "-X", method, path]
    if body is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(
        command, input=json.dumps(body) if body is not None else None,
        encoding="utf-8", capture_output=True, timeout=30, creationflags=NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"GitHub {method} {path} failed. {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def resolve_github_token(settings):
    return settings.github_token or subprocess.check_output(
        ["gh", "auth", "token", "--hostname", "github.com"], encoding="utf-8",
        timeout=10, creationflags=NO_WINDOW,
    ).strip()


def make_ingress(port, repository, secret, size_limit):
    class WebhookIngress(BaseHTTPRequestHandler):
        def reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(404, b'{"error":"webhook endpoint only"}')

        def do_POST(self):
            if self.path != WEBHOOK_PATH:
                self.do_GET()
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= size_limit:
                    self.reply(413, b'{"error":"invalid body size"}')
                    return
                self.connection.settimeout(8)
                body = self.rfile.read(length)
                signature = self.headers.get("X-Hub-Signature-256", "")
                if not verify_signature(secret, body, signature):
                    self.reply(401, b'{"error":"invalid webhook signature"}')
                    return
                event = self.headers.get("X-GitHub-Event", "")
                payload = json.loads(body)
                received_repo = payload.get("repository", {}).get("full_name", "")
                if received_repo.lower() != repository.lower():
                    self.reply(403, b'{"error":"repository is not configured for this tunnel"}')
                    return
                headers = {
                    "Content-Type": "application/json", "X-GitHub-Event": event,
                    "X-Hub-Signature-256": signature,
                    "X-GitHub-Delivery": self.headers.get("X-GitHub-Delivery", ""),
                }
            except (ValueError, TypeError, AttributeError, OSError):
                self.reply(400, b'{"error":"invalid webhook payload"}')
                return
            upstream = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
            try:
                upstream.request("POST", WEBHOOK_PATH, body, headers)
                response = upstream.getresponse()
                status, output = response.status, response.read()
            except (OSError, http.client.HTTPException):
                self.reply(502, b'{"error":"PR Harness is unavailable"}')
                return
            finally:
                upstream.close()
            self.reply(status, output)

    return ThreadingHTTPServer(("127.0.0.1", 0), WebhookIngress)


def write_status(state):
    pending = STATE.with_suffix(f".{os.getpid()}.tmp")
    pending.write_text(json.dumps(state, indent=2), encoding="utf-8")
    pending.replace(STATE)


def process_running(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def session_running(state):
    return bool(state.get("running")) and all(
        process_running(state.get(key)) for key in ("launcher_pid", "app_pid", "tunnel_pid")
    )


def wait_until(predicate, child, timeout, name):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if STOP.exists():
            raise KeyboardInterrupt
        if child.poll() is not None:
            raise RuntimeError(f"{name} exited. Check its log in {STATE_DIR}.")
        value = predicate()
        if value:
            return value
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {name}. Check its log in {STATE_DIR}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", help="GitHub owner/repository")
    parser.add_argument("--cloudflared", type=Path,
                        default=Path(shutil.which("cloudflared") or STATE_DIR / "cloudflared.exe"))
    parser.add_argument("--stop", action="store_true", help="Stop this project's running webhook session")
    parser.add_argument("--status", action="store_true", help="Show the live state of this project's session")
    args = parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    previous = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if args.status:
        print(json.dumps({**previous, "recorded_running": bool(previous.get("running")),
                          "running": session_running(previous)}, ensure_ascii=False, indent=2))
        return
    if args.stop:
        if not process_running(previous.get("launcher_pid")):
            if previous:
                write_status({**previous, "running": False, "phase": "stopped"})
            print("No managed webhook launcher is running.")
            return
        STOP.touch()
        deadline = time.monotonic() + 20
        while process_running(previous.get("launcher_pid")) and time.monotonic() < deadline:
            time.sleep(.25)
        print("Stop requested. See validation/github-webhook/status.json.")
        return
    repository = args.repository or ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        parser.error("--repository owner/repository is required")
    if not args.cloudflared.is_file():
        parser.error("cloudflared executable not found. Supply --cloudflared PATH.")

    settings = Settings.from_env()
    if settings.host != "127.0.0.1":
        raise RuntimeError("The webhook launcher requires PR_HARNESS_HOST=127.0.0.1.")
    if not settings.resolved_llm():
        raise RuntimeError("Configure an PR Harness model in .env before running webhook reviews.")
    try:
        with socket.create_connection(("127.0.0.1", settings.port), timeout=2):
            if previous.get("repository") == repository and session_running(previous):
                print(f"PR Harness is already running: {previous['app_url']} | {repository}")
                return
            raise RuntimeError(f"Port {settings.port} is already occupied. Stop the existing service first.")
    except OSError:
        pass

    hook_path = f"repos/{repository}/hooks"
    remote_hooks = gh_api(hook_path)
    hook_id = next((item["id"] for item in remote_hooks
                    if previous.get("repository") == repository
                    and item["id"] == previous.get("hook_id")
                    and item["config"].get("url") == previous.get("webhook_url")), None)
    token = resolve_github_token(settings)
    if not token:
        raise RuntimeError("Set PR_HARNESS_GITHUB_TOKEN or sign in with GitHub CLI.")

    secret = settings.github_webhook_secret or secrets.token_urlsafe(32)
    app_env = dict(os.environ, PR_HARNESS_GITHUB_TOKEN=token,
                   PR_HARNESS_GITHUB_WEBHOOK_SECRET=secret, PR_HARNESS_AUTO_POST_REVIEW="true",
                   PYTHONUNBUFFERED="1")
    tunnel_env = {key: value for key, value in os.environ.items()
                  if not key.upper().startswith(("PR_HARNESS_", "GITHUB_TOKEN", "GH_TOKEN"))}
    STOP.unlink(missing_ok=True)
    state = {"repository": repository, "app_url": f"http://127.0.0.1:{settings.port}",
             "running": False, "phase": "starting", "launcher_pid": os.getpid(),
             "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if hook_id is not None:
        state.update(hook_id=hook_id, webhook_url=previous.get("webhook_url", ""))
    write_status(state)
    children = []
    ingress = None
    try:
        with (STATE_DIR / "pr_harness.log").open("w", encoding="utf-8") as log:
            app = subprocess.Popen([sys.executable, "-B", "-m", "pr_harness"], cwd=ROOT,
                                   env=app_env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=NO_WINDOW)
        children.append(app)
        state["app_pid"] = app.pid
        write_status(state)

        def health():
            try:
                with urllib.request.urlopen(state["app_url"] + "/health", timeout=2) as response:
                    return json.load(response).get("status") == "ok"
            except (OSError, ValueError):
                return False

        wait_until(health, app, 15, "PR Harness /health")
        ingress = make_ingress(settings.port, repository, secret, settings.max_diff_bytes + 256 * 1024)
        threading.Thread(target=ingress.serve_forever, daemon=True).start()
        tunnel_log = STATE_DIR / "cloudflared.log"
        with tunnel_log.open("w", encoding="utf-8") as log:
            tunnel = subprocess.Popen(
                [str(args.cloudflared.resolve()), "tunnel", "--no-autoupdate", "--protocol", "http2",
                 "--url", f"http://127.0.0.1:{ingress.server_port}"], cwd=ROOT, env=tunnel_env,
                stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
            )
        children.append(tunnel)
        state["tunnel_pid"] = tunnel.pid
        write_status(state)

        def public_url():
            content = tunnel_log.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
            return match.group() if match and "Registered tunnel connection" in content else None

        url = wait_until(public_url, tunnel, 70, "Cloudflare Quick Tunnel")
        webhook_url = url + WEBHOOK_PATH

        def ingress_reachable():
            try:
                urllib.request.urlopen(url + "/health", timeout=4).close()
            except urllib.error.HTTPError as error:
                with error:
                    return error.code == 404 and error.read() == b'{"error":"webhook endpoint only"}'
            except OSError:
                return False
            return False

        wait_until(ingress_reachable, tunnel, 35, "public webhook ingress")
        config = {"active": True, "events": ["pull_request"], "config": {
            "url": webhook_url, "content_type": "json", "secret": secret, "insecure_ssl": "0",
        }}
        if hook_id is None:
            hook = gh_api(hook_path, "POST", {"name": "web", **config})
        else:
            hook = gh_api(f"{hook_path}/{hook_id}", "PATCH", config)
        hook_id = hook["id"]
        state.update(hook_id=hook_id, webhook_url=webhook_url, running=True, phase="running")
        write_status(state)
        print(f"PR Harness {state['app_url']} | {repository} webhook {hook_id}", flush=True)
        print("GitHub credentials loaded; automatic comments enabled. "
              f"Repair test command configured: {bool(settings.repair_test_command)}", flush=True)
        while not STOP.exists() and all(child.poll() is None for child in children):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        state.update(phase="failed", error=str(error)[:1000])
        raise
    finally:
        if state.get("running"):
            try:
                gh_api(f"{hook_path}/{hook_id}", "PATCH", {"active": False})
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                print(f"Could not deactivate webhook {hook_id}. {error}", file=sys.stderr)
        for child in reversed(children):
            if child.poll() is None:
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                            capture_output=True, timeout=10, check=True,
                            creationflags=NO_WINDOW,
                        )
                    else:
                        child.terminate()
                except (OSError, subprocess.SubprocessError):
                    if child.poll() is None:
                        child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if ingress is not None:
            ingress.shutdown()
            ingress.server_close()
        state["running"] = False
        if state.get("phase") != "failed":
            state["phase"] = "stopped"
        state["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_status(state)


if __name__ == "__main__":
    main()
