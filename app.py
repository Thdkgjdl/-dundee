"""Local safety roster review and supervised official H5 confirmation tool."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time as monotonic_time
import urllib.request
import webbrowser
from datetime import date, datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from browser_bridge import MOBILE_USER_AGENT, BrowserBridge, BridgeConfig
from roster_parser import parse_safety_board_text

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TZ = ZoneInfo("Asia/Shanghai")
PORT = int(os.environ.get("CSU_SAFETY_PORT", "8767"))
CDP_PORT = int(os.environ.get("CSU_SAFETY_CDP_PORT", "9252"))
ACTIVE_CDP_PORT = CDP_PORT
TASK_NAME = "CSU Safety Review Readonly Check"
SCHOOL_HOME = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/pages/home/index"
SAFETY_BOARD = "https://zhxg.csu.edu.cn/znzhxgpt_h5/#/ywViews/qxjlfx/dqr/board"
SAFETY_SOURCE = "official-h5-safety-board"
DEMO = [{"id": "demo-001", "name": "演示人员甲", "status": "待确认"},
        {"id": "demo-002", "name": "演示人员乙", "status": "待确认"}]
LOCK = threading.RLock()
TRIALS = {}
BATCHES = {}
BATCH_RUNNING = threading.Event()


def now():
    return datetime.now(TZ)


def loopback_urlopen(url, timeout=None):
    """Open a loopback URL without ever routing through a configured proxy.

    A system or environment HTTP proxy would otherwise intercept the local
    CDP endpoint and break the browser connection on proxied machines.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout)


def read_json(path, fallback, strict=False):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if strict:
            raise ValueError(f"本地数据文件无效：{path.name}")
        return fallback


def write_json_atomic(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def submission_claim(business_date, kind):
    """Take a cross-process, same-day claim before any official submit attempt."""
    path = DATA / f"submission-claim-{business_date}.lock"
    try:
        with path.open("x", encoding="ascii") as stream:
            stream.write(kind)
    except FileExistsError as exc:
        raise ValueError("今天已有另一条提交流程在执行或结果不明；今天已尝试或正在尝试，请人工核对，不会重试") from exc
    return path


def release_submission_claim(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def trial_allows_batch(business_date):
    marker = DATA / f"trial-attempt-{business_date}.done"
    if not marker.exists():
        return
    result = read_json(DATA / "trial-result.json", None, strict=True)
    if (not isinstance(result, dict) or result.get("status") != "success" or
            str(result.get("at", ""))[:10] != business_date):
        raise ValueError("今天已有试验但结果不是明确成功，批次已暂停；请人工核对官方记录")


def batch_status():
    result = read_json(DATA / "batch-result.json", {"status": "idle", "submitted_count": 0,
                       "total_count": 0, "detail": "尚未提交批次"}, strict=True)
    if not isinstance(result, dict):
        raise ValueError("本地批次状态无效")
    if result.get("status") == "running" and not BATCH_RUNNING.is_set():
        result = dict(result, status="unknown", detail="上次批次中断；请人工核对学校记录，不会自动重试")
    return result


def settings():
    value = read_json(DATA / "settings.json", {}, strict=True)
    scheduled = str(value.get("time", "09:00"))
    try:
        time.fromisoformat(scheduled)
    except ValueError as exc:
        raise ValueError("每日检查时间无效") from exc
    if len(scheduled) != 5:
        raise ValueError("每日检查时间无效")
    return {"time": scheduled, "timezone": "Asia/Shanghai", "mode": "read-only"}


def update_task_time(value):
    if os.name != "nt":
        return
    installer = ROOT / "install-task.ps1"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", str(installer), "-At", value],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise RuntimeError("Windows 计划任务更新失败")


def find_free_cdp_port():
    for candidate in range(CDP_PORT, min(CDP_PORT + 100, 65536)):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    raise RuntimeError("没有可用的本机浏览器连接端口")


def browser_profile(port):
    suffix = "" if port == 9252 else f"-{port}"
    return DATA / f"edge-school-direct-mobile{suffix}"


def edge_candidates():
    """Return every plausible Microsoft Edge executable path.

    ``PROGRAMFILES(X86)`` is redirected on machines where the program files
    tree was moved (this one points at D: while Edge lives on C:), so the
    well-known default locations and the per-user install are always probed in
    addition to the environment-derived ones.
    """
    roots = []
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value))
    roots.extend([Path("C:/Program Files (x86)"), Path("C:/Program Files")])
    candidates = []
    for root in roots:
        candidate = root / "Microsoft/Edge/Application/msedge.exe"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def edge_executable():
    """Resolve the installed Edge executable, preferring PATH lookups."""
    found = shutil.which("msedge")
    if found:
        return found
    return next((str(path) for path in edge_candidates() if path.exists()), None)


def open_school_h5():
    global ACTIVE_CDP_PORT
    if os.name != "nt":
        raise RuntimeError("校园 H5 Edge 登录仅支持 Windows")
    edge = edge_executable()
    if not edge:
        raise RuntimeError("未找到 Microsoft Edge；请确认已安装 Edge，或把 msedge.exe 加入 PATH")
    existing = school_bridge().read_status()
    if existing.status.value in {"connected", "login_required", "loading"}:
        return existing.as_dict()
    if existing.status.value in {"mobile_required", "loading_failed", "not_school_page"}:
        # The browser is already running: navigate the existing window instead
        # of starting a second instance with the same profile.
        return school_bridge().open().as_dict()
    port = find_free_cdp_port()
    ACTIVE_CDP_PORT = port
    profile = browser_profile(port)
    profile.mkdir(parents=True, exist_ok=True)
    # Launch on a blank page with a process-level mobile user agent: the
    # school SPA evaluates its mobile gate on first boot, so it must never
    # load as a desktop client.  The flag keeps the mobile identity even if
    # the panel process restarts and drops its CDP emulation session.
    subprocess.Popen([edge, f"--user-data-dir={profile}", "--new-window", "--no-first-run",
                      "--no-default-browser-check", "--no-proxy-server",
                      f"--user-agent={MOBILE_USER_AGENT}", "--window-size=412,915",
                      f"--remote-allow-origins=http://127.0.0.1:{PORT}",
                      f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                      "about:blank"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    deadline = monotonic_time.monotonic() + 30
    while monotonic_time.monotonic() < deadline:
        try:
            with loopback_urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as response:
                version = json.load(response)
            if version.get("Browser") and version.get("webSocketDebuggerUrl"):
                with loopback_urlopen(f"http://127.0.0.1:{port}/json/list", timeout=0.5) as response:
                    targets = json.load(response)
                if any(target.get("type") == "page" for target in targets):
                    opened = school_bridge().open().as_dict()
                    if opened.get("status") in {"connected", "login_required", "mobile_required", "not_school_page"}:
                        return opened
                    # The SPA may still be redirecting right after navigation;
                    # wait for a settled status instead of reporting the
                    # transient loading state as a failure.
                    settle = monotonic_time.monotonic() + 10
                    while monotonic_time.monotonic() < settle:
                        threading.Event().wait(0.5)
                        current = school_bridge().read_status().as_dict()
                        if current.get("status") in {"connected", "login_required",
                                                     "mobile_required", "not_school_page"}:
                            return current
                    return opened
        except (OSError, ValueError):
            pass
        threading.Event().wait(0.25)
    raise RuntimeError("Edge 已启动，但未能连接页面；请在独立 Edge 地址栏打开学校 H5")


def school_bridge():
    return BrowserBridge(BridgeConfig(
        endpoint=f"http://127.0.0.1:{ACTIVE_CDP_PORT}",
        origin=f"http://127.0.0.1:{PORT}", mobile=True,
        profile_dir=str(browser_profile(ACTIVE_CDP_PORT))))


def inspect_school_h5():
    page = school_bridge().read_status().as_dict()
    is_board = (page.get("business_route") == "/ywViews/qxjlfx/dqr/board" and
                page.get("title") == "班级打卡看板" and "今日晚寝打卡" in page.get("text", ""))
    match = re.search(r"(?:^|\n)未打卡[（(](\d+)[）)](?:\n|$)", page.get("text", "")) if is_board else None
    page["pending_count"] = int(match.group(1)) if match else None
    page["list_detected"] = match is not None
    page.update(roster_ready=False, roster=[], revision="", verification="尚未读取完整学校名单", verified_at="")
    business_date = now().date().isoformat() if is_board else ""
    page["business_date"] = business_date
    page["source"] = SAFETY_SOURCE if is_board else ""
    if page.get("status") == "connected" and match and business_date:
        parsed = parse_safety_board_text(page.get("text", ""), business_date,
                                         expected_count=page["pending_count"], observed_date=business_date,
                                         selected_filter=page.get("board_filter", ""))
        if parsed.ok:
            items = [{"id": row.id, "name": row.name, "status": row.status} for row in parsed.records]
            page.update(roster_ready=True, roster=items, revision=school_revision(items, business_date))
            page.update(school_verification(items, business_date))
        else:
            page["verification"] = parsed.error
    return page


def school_revision(items, business_date):
    return digest({"date": business_date, "records": sorted(items, key=lambda item: item["id"]), "source": SAFETY_SOURCE})


def read_all_roster():
    """Read the complete class board (every status) for live verification.

    The panel shows this view after a batch so the supervisor can see every
    targeted student marked 已代确认 on the official board before treating
    the batch as done.  Only counts and the live roster are returned; nothing
    from this read is persisted to disk.
    """
    from official_adapter import _read_board_from_ui
    from playwright.sync_api import sync_playwright

    today = now().date().isoformat()
    config = BridgeConfig(endpoint=f"http://127.0.0.1:{ACTIVE_CDP_PORT}",
                          origin=f"http://127.0.0.1:{PORT}", mobile=True,
                          school_url=SAFETY_BOARD, board_filter="all")
    BrowserBridge(config).open()
    runtime = sync_playwright().start()
    try:
        browser = runtime.chromium.connect_over_cdp(config.endpoint, timeout=config.navigation_timeout_ms)
        candidates = [p for context in browser.contexts for p in context.pages
                      if urlparse(p.url).hostname == "zhxg.csu.edu.cn"]
        if len(candidates) != 1:
            raise RuntimeError("无法唯一定位学校页面，请刷新学校页面后重试")
        snapshot = _read_board_from_ui(candidates[0], today, "all")
    finally:
        try:
            runtime.stop()
        except Exception:
            pass
    counts = {status: sum(1 for row in snapshot.records if row["status"] == status)
              for status in ("未打卡", "已打卡", "已代确认")}
    return {"date": today, "source": SAFETY_SOURCE, "selected_filter": "all",
            "records": [dict(row) for row in snapshot.records],
            "pending_count": snapshot.pending_count, "counts": counts}


def school_verification(items, business_date):
    if business_date != now().date().isoformat():
        return {"verification": "历史名单，仅供查看", "verified_at": ""}
    snapshot = read_json(DATA / "school-snapshot.json", None, strict=True)
    if snapshot is None:
        label = "学校名单尚未整体核验"
    elif not isinstance(snapshot, dict):
        raise ValueError("学校核验快照格式无效")
    elif snapshot.get("date") != business_date or snapshot.get("source") != SAFETY_SOURCE:
        label = "学校核验已过期，请重新核验"
    elif snapshot.get("count") != len(items) or not hmac.compare_digest(str(snapshot.get("digest", "")), school_revision(items, business_date)):
        label = "学校名单或状态变化，已暂停"
    else:
        return {"verification": "今日学校名单已核验，名单一致", "verified_at": snapshot.get("at", "")}
    return {"verification": label, "verified_at": ""}


def verify_school_roster(revision, acknowledged):
    if acknowledged is not True:
        raise ValueError("请先逐人核实完整学校名单")
    with LOCK:
        page = open_school_roster(now().date().isoformat())
        if (page.get("status") != "connected" or not page.get("roster_ready") or
                page.get("business_date") != now().date().isoformat() or page.get("source") != SAFETY_SOURCE):
            raise ValueError("必须读取今天的完整官方名单后再核验")
        if not hmac.compare_digest(str(revision), page["revision"]):
            raise ValueError("学校名单或状态已变化，请刷新后重新核验")
        snapshot = {"date": page["business_date"], "at": now().isoformat(timespec="seconds"),
                    "digest": page["revision"], "count": len(page["roster"]), "source": SAFETY_SOURCE}
        (DATA / "school-snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        append_audit("school_roster_verified", len(page["roster"]), "今日学校完整名单核验已保存；未提交")
        return {"ok": True, "count": len(page["roster"]), "at": snapshot["at"]}


def prepare_school_trial(body):
    """Prepare a reviewable single-record trial; never submit here."""
    method = body.get("method")
    explanation = str(body.get("explanation", "")).strip()
    if method not in {"电话", "当面", "室友转达"} or not explanation or len(explanation) > 100:
        raise ValueError("请选择真实核实方式，并填写 1–100 字情况说明")
    with LOCK:
        page = open_school_roster(now().date().isoformat())
        if not page.get("roster_ready") or page.get("source") != SAFETY_SOURCE:
            raise ValueError("必须先读取完整安全待确认名单")
        if not hmac.compare_digest(str(body.get("revision", "")), page["revision"]):
            raise ValueError("名单已变化，请重新整体核验")
        snapshot = read_json(DATA / "school-snapshot.json", None, strict=True)
        if not isinstance(snapshot, dict) or snapshot.get("date") != page["business_date"] or snapshot.get("source") != SAFETY_SOURCE or snapshot.get("count") != len(page["roster"]) or not hmac.compare_digest(str(snapshot.get("digest", "")), page["revision"]):
            raise ValueError("请先整体核实并保存今天的完整名单")
        target = next((row for row in page["roster"] if row["id"] == str(body.get("record_id", ""))), None)
        if target is None:
            raise ValueError("试验记录必须在今天已核验的未打卡名单内")
        token = secrets.token_urlsafe(24)
        TRIALS.clear()
        TRIALS[token] = {"record": target, "method": method, "explanation": explanation,
                         "roster": page["roster"], "revision": page["revision"],
                         "date": page["business_date"], "expires": monotonic_time.monotonic() + 300}
        return {"token": token, "record": target, "method": method, "explanation": explanation,
                "date": page["business_date"], "notice": "仅提交这一条；结果不明或部分成功即停止，不会重试。"}


def submit_school_trial(body):
    """Called only after the user reviews and explicitly approves a trial."""
    if body.get("supervised_approved") is not True:
        raise ValueError("请明确确认这是获准的试验记录，并监督一次真实提交")
    with LOCK:
        trial = TRIALS.pop(str(body.get("token", "")), None)
        if not trial or trial["expires"] < monotonic_time.monotonic() or trial["date"] != now().date().isoformat():
            raise ValueError("试验预览已失效，请重新准备")
        page = open_school_roster(trial["date"])
        if not page.get("roster_ready") or not hmac.compare_digest(page.get("revision", ""), trial["revision"]):
            raise ValueError("名单或状态发生变化，试验已暂停")
        snapshot = read_json(DATA / "school-snapshot.json", None, strict=True)
        if (not isinstance(snapshot, dict) or snapshot.get("source") != SAFETY_SOURCE or
                snapshot.get("date") != trial["date"] or snapshot.get("count") != len(page["roster"]) or
                not hmac.compare_digest(str(snapshot.get("digest", "")), trial["revision"])):
            raise ValueError("今日核验快照失效，试验已暂停")
        if (DATA / f"batch-attempt-{trial['date']}.done").exists():
            raise ValueError("今天已有批次提交尝试，请人工核对官方结果，不会再次提交试验")
        claim = submission_claim(trial["date"], "trial")
        marker = DATA / f"trial-attempt-{trial['date']}.done"
        try:
            with marker.open("x", encoding="ascii") as stream:
                stream.write(now().isoformat(timespec="seconds"))
        except FileExistsError as exc:
            release_submission_claim(claim)
            raise ValueError("今天已经尝试过试验；请先人工核对结果，不会再次提交") from exc
        from official_adapter import TrialConfig, execute_trial
        from playwright.sync_api import sync_playwright
        runtime = None
        successful = False
        try:
            runtime = sync_playwright().start()
            browser = runtime.chromium.connect_over_cdp(school_bridge().config.endpoint, timeout=15000)
            candidates = [p for context in browser.contexts for p in context.pages
                          if urlparse(p.url).hostname == "zhxg.csu.edu.cn" and
                          urlparse(p.url).fragment.partition("?")[0] == "/ywViews/qxjlfx/dqr/board"]
            if len(candidates) != 1:
                raise ValueError("无法唯一定位官方班级看板，试验已停止")
            result = execute_trial(TrialConfig(page=candidates[0], supervised_approved=True, board_filter="pending"),
                                   trial["record"], trial["method"], trial["explanation"],
                                   trial["roster"], trial["date"])
            minimal = {"at": now().isoformat(timespec="seconds"), "status": result.status,
                       "submitted": result.submitted, "pending_before": result.pending_before,
                       "pending_after": result.pending_after, "detail": result.detail}
            (DATA / "trial-result.json").write_text(json.dumps(minimal, ensure_ascii=False), encoding="utf-8")
            append_audit("trial_" + result.status, int(result.submitted), result.detail)
            successful = result.status == "success"
            return minimal
        except Exception:
            unknown = {"at": now().isoformat(timespec="seconds"), "status": "unknown",
                       "submitted": 0, "pending_before": None, "pending_after": None,
                       "detail": "试验已停止；请人工核对官方结果，不会自动重试"}
            try:
                write_json_atomic(DATA / "trial-result.json", unknown)
            except OSError:
                pass
            append_audit("trial_stopped", 0, "试验已停止；请人工核对官方结果，不会自动重试")
            raise RuntimeError("试验已停止，请人工核对官方结果；不会自动重试") from None
        finally:
            if runtime is not None:
                try:
                    runtime.stop()
                except Exception:
                    pass
            if successful:
                release_submission_claim(claim)


def prepare_school_batch(body):
    """Bind a complete set of factual instructions to today's verified roster."""
    supplied = body.get("records")
    if not isinstance(supplied, list) or not supplied:
        raise ValueError("请为完整待确认名单逐人填写核实方式和情况说明")
    plans = []
    seen = set()
    for row in supplied:
        if not isinstance(row, dict):
            raise ValueError("批次记录格式无效")
        ident = str(row.get("id", "")).strip()
        method = row.get("method")
        explanation = row.get("explanation", "")
        explanation = explanation.strip() if isinstance(explanation, str) else ""
        if not ident or ident in seen or method not in {"电话", "当面", "室友转达"} or not explanation or len(explanation) > 100:
            raise ValueError("每人必须有唯一学号、真实核实方式和 1–100 字情况说明")
        seen.add(ident)
        plans.append({"id": ident, "method": method, "explanation": explanation})
    with LOCK:
        page = open_school_roster(now().date().isoformat())
        if (page.get("status") != "connected" or not page.get("roster_ready") or page.get("source") != SAFETY_SOURCE or
                page.get("business_date") != now().date().isoformat()):
            raise ValueError("必须先读取今天的完整官方安全代确认名单")
        if not hmac.compare_digest(str(body.get("revision", "")), page["revision"]):
            raise ValueError("名单或状态已变化，请重新整体核验")
        snapshot = read_json(DATA / "school-snapshot.json", None, strict=True)
        if (not isinstance(snapshot, dict) or snapshot.get("date") != page["business_date"] or
                snapshot.get("source") != SAFETY_SOURCE or snapshot.get("count") != len(page["roster"]) or
                not hmac.compare_digest(str(snapshot.get("digest", "")), page["revision"])):
            raise ValueError("请先整体核实并保存今天的完整名单")
        if len(plans) != len(page["roster"]) or seen != {row["id"] for row in page["roster"]}:
            raise ValueError("提交计划必须与今天已核验的完整待确认名单一致")
        by_id = {row["id"]: row for row in page["roster"]}
        ordered = [next(plan for plan in plans if plan["id"] == row["id"]) for row in page["roster"]]
        token = secrets.token_urlsafe(24)
        BATCHES.clear()
        BATCHES[token] = {"records": ordered, "roster": page["roster"], "revision": page["revision"],
                          "date": page["business_date"], "expires": monotonic_time.monotonic() + 300}
        preview = [{"id": plan["id"], "name": by_id[plan["id"]]["name"],
                    "method": plan["method"], "explanation": plan["explanation"]} for plan in ordered]
        return {"token": token, "date": page["business_date"], "count": len(preview), "records": preview,
                "notice": "点击提交后将依次操作官方页面；每人成功才继续，状态不明立即停止且不会自动重试。"}


def submit_school_batch(body):
    """Make one supervised batch attempt, with a persistent replay marker."""
    if body.get("supervised_approved") is not True:
        raise ValueError("请确认已复核整个批次并监督这一次提交")
    with LOCK:
        batch = BATCHES.pop(str(body.get("token", "")), None)
        if not batch or batch["expires"] < monotonic_time.monotonic() or batch["date"] != now().date().isoformat():
            raise ValueError("批次预览已失效，请重新准备")
        page = open_school_roster(batch["date"])
        if (page.get("status") != "connected" or not page.get("roster_ready") or
                page.get("source") != SAFETY_SOURCE or page.get("business_date") != batch["date"] or
                not hmac.compare_digest(str(page.get("revision", "")), batch["revision"])):
            raise ValueError("名单或状态发生变化，批次已暂停")
        snapshot = read_json(DATA / "school-snapshot.json", None, strict=True)
        if (not isinstance(snapshot, dict) or snapshot.get("source") != SAFETY_SOURCE or
                snapshot.get("date") != batch["date"] or snapshot.get("count") != len(page["roster"]) or
                not hmac.compare_digest(str(snapshot.get("digest", "")), batch["revision"])):
            raise ValueError("今日核验快照失效，批次已暂停")
        trial_allows_batch(batch["date"])
        claim = submission_claim(batch["date"], "batch")
        marker = DATA / f"batch-attempt-{batch['date']}.done"
        try:
            with marker.open("x", encoding="ascii") as stream:
                stream.write(now().isoformat(timespec="seconds"))
        except FileExistsError as exc:
            release_submission_claim(claim)
            raise ValueError("今天已尝试过批量提交；请人工核对官方结果，不会再次提交") from exc

        result_path = DATA / "batch-result.json"
        progress = {"at": now().isoformat(timespec="seconds"), "status": "running",
                    "submitted_count": 0, "total_count": len(batch["records"]), "detail": "正在官方页面逐条提交"}
        write_json_atomic(result_path, progress)

        def on_progress(completed):
            progress["submitted_count"] = completed
            write_json_atomic(result_path, progress)

        runtime = None
        BATCH_RUNNING.set()
        try:
            from official_adapter import TrialConfig, execute_batch
            from playwright.sync_api import sync_playwright
            runtime = sync_playwright().start()
            browser = runtime.chromium.connect_over_cdp(school_bridge().config.endpoint, timeout=15000)
            candidates = [p for context in browser.contexts for p in context.pages
                          if urlparse(p.url).hostname == "zhxg.csu.edu.cn" and
                          urlparse(p.url).fragment.partition("?")[0] == "/ywViews/qxjlfx/dqr/board"]
            if len(candidates) != 1:
                raise ValueError("无法唯一定位官方班级看板")
            result = execute_batch(TrialConfig(page=candidates[0], supervised_approved=True, board_filter="pending"),
                                   batch["records"], batch["roster"], batch["date"], on_progress=on_progress)
            # Final live verification: a completed batch is only reported as
            # such when a fresh full-board read shows every submitted student
            # marked 已代确认.  The count alone is persisted, never names.
            confirmed = None
            try:
                all_roster = read_all_roster()
                batch_ids = {row["id"] for row in batch["records"]}
                confirmed = sum(1 for row in all_roster["records"]
                                if row["id"] in batch_ids and row["status"] == "已代确认")
            except Exception:
                confirmed = None
            status, detail = result.status, result.detail
            if confirmed is not None:
                if result.status == "success" and confirmed != result.submitted_count:
                    status = "unknown"
                    detail += "；最终名单核验不一致，已停止"
                elif result.status == "success":
                    detail += f"；最终名单核验：{confirmed} 人已全部代确认"
            minimal = {"at": now().isoformat(timespec="seconds"), "status": status,
                       "submitted_count": result.submitted_count, "total_count": result.total_count,
                       "confirmed_count": confirmed, "detail": detail}
            write_json_atomic(result_path, minimal)
            append_audit("batch_" + status, result.submitted_count, detail)
            if status == "success":
                release_submission_claim(claim)
            return minimal
        except Exception:
            progress.update(at=now().isoformat(timespec="seconds"), status="unknown",
                            detail="批次已停止；请人工核对官方结果，不会自动重试")
            write_json_atomic(result_path, progress)
            append_audit("batch_unknown", progress["submitted_count"], progress["detail"])
            raise RuntimeError(progress["detail"]) from None
        finally:
            BATCH_RUNNING.clear()
            if runtime is not None:
                try:
                    runtime.stop()
                except Exception:
                    pass


def open_school_roster(selected_date):
    try:
        requested = date.fromisoformat(selected_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("请选择有效日期") from exc
    if requested.isoformat() != selected_date or requested != now().date():
        raise ValueError("安全代确认看板仅适配今天，请选择今天")
    config = BridgeConfig(endpoint=f"http://127.0.0.1:{ACTIVE_CDP_PORT}",
                         origin=f"http://127.0.0.1:{PORT}", mobile=True,
                         school_url=SAFETY_BOARD)
    BrowserBridge(config).open()
    deadline = monotonic_time.monotonic() + 12
    page = inspect_school_h5()
    while monotonic_time.monotonic() < deadline and (page.get("status") == "loading_failed" or
            (page.get("business_date") != selected_date and page.get("status") == "connected")):
        threading.Event().wait(0.5)
        page = inspect_school_h5()
    # A SPA can leave the previous date rendered after a navigation failure.
    # Never report that stale page as the requested roster.
    if page.get("status") == "connected" and page.get("business_date") != selected_date:
        page = dict(page)
        page["status"] = "loading_failed"
        page["detail"] = "官方名单日期未切换到所选日期，已停止读取"
        page["list_detected"] = False
        page["pending_count"] = None
        page["text"] = ""
        page.update(roster_ready=False, roster=[], revision="", verification="日期切换失败，已暂停", verified_at="")
    return page


def roster():
    # Legacy local demonstration; actual H5 records use inspect_school_h5.
    path = DATA / "roster.json"
    if not path.exists():
        raise ValueError("本地名单文件缺失")
    return read_json(path, DEMO, strict=True)


def safe_roster(items):
    if not isinstance(items, list):
        raise ValueError("名单格式无效")
    clean, seen = [], set()
    for row in items:
        if not isinstance(row, dict) or not all(k in row for k in ("id", "name", "status")):
            raise ValueError("名单记录格式无效")
        ident = str(row["id"])
        if ident in seen:
            raise ValueError("名单中存在重复记录")
        seen.add(ident)
        clean.append({"id": ident, "name": str(row["name"]), "status": str(row["status"])})
    return clean


def digest(items):
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new((DATA / "local.key").read_bytes(), canonical.encode(), hashlib.sha256).hexdigest()


def ensure_data():
    DATA.mkdir(exist_ok=True)
    if not (DATA / "local.key").exists():
        (DATA / "local.key").write_bytes(secrets.token_bytes(32))
    if not (DATA / "roster.json").exists():
        (DATA / "roster.json").write_text(json.dumps(DEMO, ensure_ascii=False, indent=2), encoding="utf-8")
    if not (DATA / "settings.json").exists():
        (DATA / "settings.json").write_text('{"time":"09:00"}', encoding="utf-8")


def append_audit(event, count=0, summary=""):
    record = {"at": now().isoformat(timespec="seconds"), "event": event,
              "count": int(count), "summary": summary}
    with (DATA / "audit.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def current_state():
    with LOCK:
        items = safe_roster(roster())
        snap = read_json(DATA / "snapshot.json", None, strict=True)
        today = now().date().isoformat()
        if snap is None:
            verification = "尚未核验"
        elif snap.get("date") != today:
            verification = "已过期，请重新核验"
        elif not hmac.compare_digest(snap.get("digest", ""), digest(items)):
            verification = "名单或状态变化，已暂停"
        else:
            verification = "今日已核验，名单一致（只读预演）"
        audits = []
        try:
            audits = [json.loads(line) for line in (DATA / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-8:]]
        except (OSError, ValueError):
            pass
        return {"date": today, "roster": items, "count": len(items), "verification": verification,
                "revision": digest(items), "settings": settings(), "audit": list(reversed(audits)),
                "submission_enabled": False, "snapshot": snap}


def scheduled_readonly_check():
    """Run at most once per local date, and only in the configured minute."""
    instant = now()
    plan = settings()["time"]
    if instant.strftime("%H:%M") != plan:
        append_audit("missed_schedule", 0, "计划触发时间已错过；未执行")
        return "missed"
    marker = DATA / f"scheduled-{instant.date().isoformat()}.done"
    try:
        with marker.open("x", encoding="ascii") as f:
            f.write(instant.isoformat(timespec="seconds"))
    except FileExistsError:
        return "already-run"
    try:
        page = open_school_roster(instant.date().isoformat())
        snap = read_json(DATA / "school-snapshot.json", None, strict=True)
        matches = bool(page.get("status") == "connected" and page.get("roster_ready") and
                       isinstance(snap, dict) and snap.get("date") == instant.date().isoformat() and
                       snap.get("source") == SAFETY_SOURCE and page.get("source") == SAFETY_SOURCE and
                       snap.get("count") == len(page.get("roster", [])) and
                       page.get("business_date") == snap["date"] and
                       hmac.compare_digest(str(snap.get("digest", "")), page["revision"]))
        append_audit("readonly_match" if matches else "readonly_paused", len(page.get("roster", [])),
                     "定时只读检查：学校名单一致；未提交" if matches else "定时只读检查：学校名单需重新核验或连接失效；未提交")
        return "match" if matches else "paused"
    except Exception:
        append_audit("readonly_error", 0, "定时只读检查失败；未提交")
        return "error"


HTML = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>安全待确认辅助工具</title><style>
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#202624;background:#f4f6f5}*{box-sizing:border-box}body{margin:0}header{background:#183f36;color:white;padding:20px max(20px,calc((100% - 1040px)/2));display:flex;justify-content:space-between;align-items:center}h1{font-size:20px;margin:0}h2{font-size:18px}main{max-width:1040px;margin:24px auto;padding:0 16px}.band{padding:18px 0;border-bottom:1px solid #d8dfdb}.status{display:flex;align-items:center;gap:12px}.dot{width:10px;height:10px;border-radius:50%;background:#bf6e20}.good{background:#297c56}button,input,select,textarea{font:inherit;padding:9px 12px;border:1px solid #aebbb4;border-radius:4px;background:#fff;color:inherit}button{cursor:pointer}button.primary{background:#176c50;border-color:#176c50;color:white}button:disabled{opacity:.5;cursor:not-allowed}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;margin-top:12px}th,td{text-align:left;padding:11px;border-bottom:1px solid #e0e5e2}th{font-size:13px;color:#5d6862}#schoolTable{min-width:760px}#schoolTable textarea{width:100%;min-width:210px;resize:vertical}#schoolTable select{width:110px}.muted{color:#68736d;font-size:14px}.alert{color:#8a4c12}.notice{background:#fff4e4;border-left:3px solid #c27a28;padding:12px 14px;margin:16px 0}.lock{color:#8b473d;font-weight:600}.time{width:130px}.audit{display:grid;grid-template-columns:1fr 120px 2fr;gap:10px;padding:8px 0;border-bottom:1px solid #e5e9e7;font-size:14px}@media(max-width:600px){header{padding:16px}main{margin:12px auto}.audit{grid-template-columns:1fr 70px}.audit span:last-child{grid-column:1/-1}}
.st{font-weight:600}.st-pending{color:#8a4c12}.st-confirmed{color:#176c50}.st-done{color:#5d6862}
.gate{position:fixed;inset:0;background:#f4f6f5;display:flex;align-items:center;justify-content:center;padding:20px;z-index:10;transition:opacity .45s ease}.gate.done{opacity:0;pointer-events:none}.gate-card{background:#fff;border:1px solid #d8dfdb;border-radius:10px;box-shadow:0 12px 40px rgba(24,63,54,.14);max-width:430px;width:100%;padding:38px 34px;text-align:center}.gate-badge{width:58px;height:58px;border-radius:50%;background:#183f36;display:flex;align-items:center;justify-content:center;font-size:26px;margin:0 auto 18px}.gate-badge .lock{color:#fff}.gate-title{font-size:21px;font-weight:600;margin:0 0 4px}.gate-sub{color:#5d6862;font-size:14px;margin:0 0 20px}.gate-button{width:100%;padding:12px;font-size:16px;margin:4px 0 14px}.gate-status{font-size:14px;color:#8a4c12;margin:0 0 8px;min-height:21px}.gate-hint{font-size:13px;margin:0 0 12px}.gate-skip{background:none;border:none;color:#68736d;font-size:13px;text-decoration:underline;cursor:pointer;padding:4px 8px}.gate-waiting .gate-status::before{content:"";display:inline-block;width:12px;height:12px;border:2px solid #c9d4ce;border-top-color:#176c50;border-radius:50%;margin-right:8px;vertical-align:-2px;animation:gatespin 1s linear infinite}@keyframes gatespin{to{transform:rotate(360deg)}}
</style></head><body><div id="loginGate" class="gate gate-waiting" role="dialog" aria-modal="true" aria-labelledby="gateTitle"><div class="gate-card"><div class="gate-badge"><span class="lock">🔒</span></div><p class="gate-title" id="gateTitle">安全待确认辅助工具</p><p class="gate-sub">中南大学 · 班级安全代确认</p><p class="muted">请使用学校统一身份认证登录。登录在学校官方页面中完成，工具不读取、不保存你的账号、密码、验证码或 Cookie。</p><button id="gateLogin" class="primary gate-button" type="button">打开校园 H5 登录</button><p id="gateStatus" class="gate-status" aria-live="polite">正在检查学校登录状态…</p><p id="gateHint" class="muted gate-hint" hidden>已打开独立 Edge 窗口，请在其中按学校流程正常登录；登录成功后工具会自动进入并读取今日待确认名单。</p><button id="gateSkip" class="gate-skip" type="button">不连接学校，进入本地演示</button></div></div><header><h1>安全待确认辅助工具</h1><span id="date"></span></header><main>
<section class="band"><div class="status"><span id="dot" class="dot"></span><strong id="state">正在读取学校页面</strong></div><p class="muted" id="mode">真实学校页面 · 人工核验后提交 · Asia/Shanghai</p><div class="row"><label for="runAt">每日检查时间</label><input id="runAt" class="time" type="time"><button id="saveTime">保存</button><button id="refresh">刷新名单</button><button id="openSchool">打开校园 H5 登录</button><button id="readSchool">刷新学校页面</button><label for="rosterDate">名单日期</label><input id="rosterDate" type="date" disabled><button id="openRoster" class="primary">打开安全待确认名单</button></div><p class="muted">在独立 Edge 中正常登录。学校页面通过直连和移动设备视图访问，登录信息由浏览器保留。</p><p id="schoolStatus" class="muted">学校 H5 尚未读取</p><p id="schoolVerification" class="muted"></p><div class="table-scroll"><table id="schoolTable" hidden><thead><tr><th>人员</th><th>学号</th><th>状态</th><th>核实方式</th><th>情况说明</th></tr></thead><tbody id="schoolRows"></tbody></table></div><p class="row"><label><input id="schoolAck" type="checkbox" disabled> 我已逐人核实以上完整名单中的每个人安全，且确实无法本人打卡</label><button id="verifySchool" disabled>保存今日学校名单核验</button></p><details><summary>查看学校页面可见文字</summary><pre id="schoolText" style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto"></pre></details></section>
<section class="band"><h2>一键提交安全代确认</h2><div class="row"><label>统一核实方式 <select id="batchMethod"><option>电话</option><option>当面</option><option>室友转达</option></select></label><label>统一情况说明 <input id="batchNote" maxlength="100" value="已确认"></label><button id="applyBatch" disabled>应用到全部人员</button></div><p class="muted">默认统一填写“电话 / 已确认”，每行也可修改。检查完整名单及填写内容后生成预览。</p><div class="row"><button id="oneClickSubmit" class="primary" disabled>一键代确认并立即提交</button></div><div class="notice"><span class="lock">点击后将立即提交，无二次确认</span>：先自动保存今日名单核验快照（需已在上方勾选逐人核实），再按上方统一字段（默认“电话 / 已确认”）应用到每一人，生成批次预览并立刻向学校官方页面逐条提交。每个日期最多尝试一次；一旦开始，当天无法再次提交；结果不明或部分成功须人工核对官方记录。若有人核实方式不是电话或情况说明不同，请改用下方分步流程逐人修改后提交。</div><button id="prepareBatch" disabled>生成完整批次预览</button><pre id="batchPreview" style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:300px;overflow:auto"></pre><p><label><input id="batchAck" type="checkbox" disabled> 我已检查完整批次，并监督这一次真实提交</label></p><button id="submitBatch" class="primary" disabled>一键提交全部</button><p id="batchResult" class="muted" aria-live="polite"></p></section>
<section class="band"><h2>全班打卡状态（实时核验）</h2><p class="muted">读取官方看板「全部」视图，实时显示每位学生的当前状态。批次提交完成后会自动刷新；只有名单全部显示「已代确认」才算本批完成。</p><div class="row"><button id="readAllRoster" disabled>读取全班状态</button><span id="allRosterSummary" class="muted"></span></div><div class="table-scroll"><table id="allRosterTable" hidden><thead><tr><th>人员</th><th>学号</th><th>当前状态</th></tr></thead><tbody id="allRosterRows"></tbody></table></div><p id="allRosterResult" class="muted" aria-live="polite"></p></section>
<section class="band"><h2>监督单条试验</h2><p class="muted">先保存今天完整名单核验，再选择一个获准的试验记录。填写事实后生成预览；只有你明确确认并点击提交，才执行一次真实安全代确认。每日自动提交仍未启用。</p><div class="row"><label>试验人员 <select id="trialRecord" disabled></select></label><label>真实核实方式 <select id="trialMethod"><option value="">请选择</option><option>电话</option><option>当面</option><option>室友转达</option></select></label></div><p><label>情况说明（必填，最多 100 字）<br><textarea id="trialNote" maxlength="100" rows="3" style="width:100%;font:inherit"></textarea></label></p><button id="prepareTrial" disabled>生成单条试验预览</button><pre id="trialPreview" style="white-space:pre-wrap"></pre><p><label><input id="trialAck" type="checkbox" disabled> 此条为获准的试验记录，核实方式和说明真实，我会监督这一次提交</label></p><button id="submitTrial" disabled>提交这一个试验记录</button><p id="trialResult" class="muted"></p></section>
<details><summary style="padding:16px 0">查看本机流程演示（与学校名单无关）</summary><section class="band"><h2>演示待确认名单 <small id="count" class="muted"></small></h2><p id="demoState" class="muted"></p><p class="muted">请核实名单内每一条记录。此处仅测试快照流程。学校实际记录请查看上方连接区域。</p><table><thead><tr><th>人员</th><th>状态</th></tr></thead><tbody id="rows"></tbody></table><p class="row"><label><input id="ack" type="checkbox"> 我已整体核实以上完整名单</label><button id="verify" class="primary" disabled>保存今日核验</button></p></section>
<section class="band"><h2>执行状态</h2><div class="notice"><span class="lock">提交已锁定</span>：真实 H5 页面结构和提交结果尚未验证。上方连接区域只读取学校页面，此演示区域不会操作学校记录。</div><p id="execution" class="muted"></p></section>
<section class="band"><h2>最近记录</h2><div id="audit"></div></section></details></main>
<script src="/panel.js"></script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalSafetyReview/1.0"

    def log_message(self, fmt, *args):
        pass

    def respond(self, status, payload, content_type="application/json; charset=utf-8"):
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= 512000:
            raise ValueError("请求体大小无效")
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("请求体必须是对象")
        return body

    def local_request(self):
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        origin = self.headers.get("Origin")
        return host in {"127.0.0.1", "localhost"} and (not origin or origin in {
            f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"})

    def do_GET(self):
        if not self.local_request():
            return self.respond(403, {"error": "仅允许本机面板访问"})
        if self.path == "/":
            self.respond(200, HTML, "text/html; charset=utf-8")
        elif self.path == "/panel.js":
            self.respond(200, (ROOT / "panel.js").read_text(encoding="utf-8"),
                         "text/javascript; charset=utf-8")
        elif self.path == "/api/batch-status":
            try:
                self.respond(200, batch_status())
            except (OSError, ValueError):
                self.respond(503, {"error": "批次状态无法读取，请人工核对学校记录"})
        elif self.path == "/api/state":
            try:
                self.respond(200, current_state())
            except Exception:
                self.respond(500, {"error": "读取名单失败，请检查本地演示数据格式"})
        elif self.path == "/api/school-page":
            if BATCH_RUNNING.is_set():
                return self.respond(409, {"error": "批次正在执行，请等待结果"})
            host = self.headers.get("Host", "").split(":", 1)[0].lower()
            origin = self.headers.get("Origin")
            if host not in {"127.0.0.1", "localhost"} or (origin and origin not in {
                    f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}):
                return self.respond(403, {"error": "仅允许本机面板读取学校页面"})
            try:
                self.respond(200, inspect_school_h5())
            except ValueError:
                self.respond(503, {"error": "本地学校核验快照无效，已暂停；请检查快照文件"})
            except RuntimeError as exc:
                self.respond(503, {"error": str(exc)})
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self):
        try:
            host = self.headers.get("Host", "").split(":", 1)[0].lower()
            origin = self.headers.get("Origin")
            if host not in {"127.0.0.1", "localhost"} or (origin and origin not in {
                    f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}):
                return self.respond(403, {"error": "仅允许本机面板发起请求"})
            body = self.read_body()
            with LOCK:
                if self.path == "/api/verify":
                    if body.get("acknowledged") is not True:
                        return self.respond(400, {"error": "请先整体核实完整名单"})
                    items = safe_roster(roster())
                    if not hmac.compare_digest(str(body.get("revision", "")), digest(items)):
                        return self.respond(409, {"error": "名单或状态已变化，请刷新后重新整体核验"})
                    snap = {"date": now().date().isoformat(), "at": now().isoformat(timespec="seconds"),
                            "digest": digest(items), "count": len(items)}
                    (DATA / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
                    append_audit("roster_verified", len(items), "今日整体核验已保存")
                    return self.respond(200, {"ok": True})
                if self.path == "/api/verify-school":
                    try:
                        result = verify_school_roster(body.get("revision", ""), body.get("acknowledged"))
                    except ValueError as exc:
                        return self.respond(409, {"error": str(exc)})
                    return self.respond(200, result)
                if self.path in {"/api/prepare-trial", "/api/trial-submit"}:
                    try:
                        result = prepare_school_trial(body) if self.path == "/api/prepare-trial" else submit_school_trial(body)
                    except ValueError as exc:
                        return self.respond(409, {"error": str(exc)})
                    except RuntimeError as exc:
                        return self.respond(503, {"error": str(exc)})
                    return self.respond(200, result)
                if self.path in {"/api/prepare-batch", "/api/batch-submit"}:
                    try:
                        result = prepare_school_batch(body) if self.path == "/api/prepare-batch" else submit_school_batch(body)
                    except ValueError as exc:
                        return self.respond(409, {"error": str(exc)})
                    except RuntimeError as exc:
                        return self.respond(503, {"error": str(exc)})
                    return self.respond(200, result)
                if self.path == "/api/open-school":
                    try:
                        open_school_h5()
                    except RuntimeError as exc:
                        return self.respond(503, {"error": str(exc)})
                    return self.respond(200, inspect_school_h5())
                if self.path == "/api/open-roster":
                    try:
                        return self.respond(200, open_school_roster(body.get("date")))
                    except RuntimeError as exc:
                        return self.respond(503, {"error": str(exc)})
                if self.path == "/api/all-roster":
                    if BATCH_RUNNING.is_set():
                        return self.respond(409, {"error": "批次正在执行，请等待结果"})
                    try:
                        return self.respond(200, read_all_roster())
                    except RuntimeError as exc:
                        return self.respond(409, {"error": str(exc)})
                if self.path == "/api/settings":
                    value = str(body.get("time", ""))
                    try:
                        time.fromisoformat(value)
                    except ValueError:
                        return self.respond(400, {"error": "时间格式应为 HH:MM"})
                    if len(value) != 5:
                        return self.respond(400, {"error": "时间格式应为 HH:MM"})
                    try:
                        update_task_time(value)
                    except (OSError, subprocess.SubprocessError, RuntimeError):
                        return self.respond(503, {"error": "未能更新 Windows 计划任务，时间未保存"})
                    (DATA / "settings.json").write_text(json.dumps({"time": value}), encoding="utf-8")
                    append_audit("schedule_changed", 0, "每日检查时间已更新")
                    return self.respond(200, {"ok": True})
                if self.path == "/api/submit":
                    return self.respond(403, {"error": "提交功能未启用：H5 页面与结果校验尚未验证"})
            self.respond(404, {"error": "not found"})
        except (ValueError, OSError):
            self.respond(400, {"error": "请求或本地数据无效"})


def scheduler_loop():
    while True:
        instant = now()
        if instant.strftime("%H:%M") == settings()["time"]:
            scheduled_readonly_check()
        threading.Event().wait(20)


if __name__ == "__main__":
    ensure_data()
    if "--scheduled-once" in sys.argv:
        scheduled_readonly_check()
        raise SystemExit(0)
    # Playwright resolves the loopback CDP endpoint through the process
    # environment; keep loopback out of any configured proxy so the browser
    # connection survives on proxied machines.
    existing = os.environ.get("no_proxy", "") or os.environ.get("NO_PROXY", "")
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = (existing + ",127.0.0.1,localhost").strip(",")
    threading.Thread(target=scheduler_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"本机面板：http://127.0.0.1:{PORT}/")
    try:
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
