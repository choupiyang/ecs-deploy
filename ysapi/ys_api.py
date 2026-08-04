#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Young's Insight 订阅 API 服务 (纯 Python 标准库, 零第三方依赖)
监听 127.0.0.1:8080, 由 nginx 反代 /youngsinsight/api/ -> /
由 systemd (ys-api.service) 托管, 开机自启, 崩溃自动重启。

功能:
  POST /subscribe           订阅注册 -> 返回 sid + feed_url
  GET  /feed                增量拉取更新 (since 游标), 记录推送
  GET  /go                  追踪跳转 (记录阅读点击)
  POST /unsubscribe         退订
  GET  /unsubscribe         退订 (链接形式, 返回确认页)
  GET  /stats               统计 (公开聚合 / key 全量)
  GET  /health              健康检查
"""
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

# ---------------- 配置 ----------------
BASE_DIR = os.environ.get("YS_BASE_DIR", "/var/www/youngsinsight")
DATA_DIR = os.path.join(BASE_DIR, ".ysdata")
ARTICLES_FILE = os.path.join(BASE_DIR, "articles.json")
CARDS_FILE = os.path.join(BASE_DIR, "cards.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.jsonl")
SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")
STATS_KEY_FILE = os.path.join(DATA_DIR, "stats_key")
SITE_BASE = os.environ.get("YS_SITE_BASE", "https://mindresonance.online/youngsinsight")
ALLOWED_HOSTS = ("mindresonance.online",)
PORT = int(os.environ.get("YS_PORT", "8080"))
WATCH_INTERVAL = int(os.environ.get("YS_WATCH_INTERVAL", "300"))
SUB_RATE_LIMIT_PER_IP = 20   # 每 IP 每天最多注册次数
MAX_EVENTS_LINES = 20000     # events 文件行数上限, 超出压缩保留最近 10000

LOCK = threading.RLock()
os.makedirs(DATA_DIR, exist_ok=True)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def today():
    return time.strftime("%Y-%m-%d")


# ---------------- 数据存取 ----------------
def load_subscribers():
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def save_subscribers(subs):
    with LOCK:
        tmp = SUBSCRIBERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(subs, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SUBSCRIBERS_FILE)


def log_event(ev):
    ev.setdefault("ts", now_iso())
    with LOCK:
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        try:
            if os.path.getsize(EVENTS_FILE) > 4 * 1024 * 1024:
                lines = open(EVENTS_FILE, encoding="utf-8").readlines()
                if len(lines) > MAX_EVENTS_LINES:
                    keep = lines[-MAX_EVENTS_LINES:]
                    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
                        f.writelines(keep)
        except Exception:
            pass


def read_events():
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            out = []
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
            return out
    except Exception:
        return []


# ---------------- Feed 构建 ----------------
FEED_STATE_FILE = os.path.join(DATA_DIR, "feed_state.json")
STATE = {"version": 0, "mtimes": {}, "initialized": False}


def load_feed_state():
    try:
        with open(FEED_STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        STATE["version"] = int(d.get("version", 0))
        STATE["mtimes"] = d.get("mtimes", {})
        STATE["initialized"] = True
    except Exception:
        STATE["version"] = 0
        STATE["mtimes"] = {}
        STATE["initialized"] = False


def save_feed_state():
    with LOCK:
        with open(FEED_STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"version": STATE["version"], "mtimes": STATE["mtimes"]}, f)
        os.replace(FEED_STATE_FILE + ".tmp", FEED_STATE_FILE)


def refresh_version():
    """内容文件变化时版本号 +1。首次加载或重启后无变化则保持版本。"""
    cur = {}
    for f in (ARTICLES_FILE, CARDS_FILE):
        try:
            cur[f] = os.path.getmtime(f)
        except Exception:
            pass
    with LOCK:
        if not STATE["initialized"]:
            load_feed_state()
        changed = False
        for f in cur:
            if STATE["mtimes"].get(f, 0) != cur[f]:
                changed = True
                break
        if changed:
            STATE["version"] += 1
            STATE["mtimes"] = cur
            save_feed_state()
        elif not STATE["mtimes"]:
            STATE["mtimes"] = cur
            save_feed_state()
        STATE["initialized"] = True
    return STATE["version"]


def parse_cursor(cursor):
    """'v5:2026-08-04#99999' -> (5, '2026-08-04#99999'); 兼容旧格式 -> (0, cursor)"""
    if isinstance(cursor, str) and cursor.startswith("v") and ":" in cursor:
        try:
            v, k = cursor.split(":", 1)
            v = v[1:] if v.startswith("v") else v
            return int(v), k
        except Exception:
            pass
    return 0, (cursor or "")


def build_items():
    """合并文章+卡片, 按 (日期 desc, 位置 desc) 排序, 带版本+游标 key。
    key = 'v{V}:{YYYY-MM-DD}#{NNNNN}' (NNNNN = 99999-位置), 字典序即时间序。
    版本号保证: 同一位置被同日新内容顶替时也能识别为新。"""
    version = refresh_version()
    items = []
    try:
        with open(ARTICLES_FILE, "r", encoding="utf-8") as f:
            arts = json.load(f)
    except Exception:
        arts = []
    for i, a in enumerate(arts or []):
        if not isinstance(a, dict) or not a.get("id"):
            continue
        fname = a.get("file") or a.get("filename") or ""
        if not fname:
            continue
        items.append({
            "key": "v%d:%s#%05d" % (version, str(a.get("date", "0000-00-00")), 99999 - i),
            "v": version,
            "type": "article",
            "id": str(a.get("id")),
            "title": a.get("title", ""),
            "excerpt": a.get("excerpt", ""),
            "date": a.get("date", ""),
            "url": SITE_BASE + "/" + fname,
        })
    try:
        with open(CARDS_FILE, "r", encoding="utf-8") as f:
            cards = json.load(f)
    except Exception:
        cards = []
    for i, c in enumerate(cards or []):
        if not isinstance(c, dict) or not c.get("id"):
            continue
        page = c.get("page", "")
        if not page:
            continue
        items.append({
            "key": "v%d:%s#%05d" % (version, str(c.get("date", "0000-00-00")), 99999 - i),
            "v": version,
            "type": "card",
            "id": str(c.get("id")),
            "title": c.get("title", ""),
            "excerpt": c.get("subtitle", ""),
            "date": c.get("date", ""),
            "url": SITE_BASE + "/" + page,
        })
    items.sort(key=lambda x: x["key"], reverse=True)
    return items


def track_url(item, sid):
    q = urllib.parse.urlencode({
        "u": item["url"], "sid": sid or "",
        "t": item["type"], "id": item["id"],
    })
    return SITE_BASE + "/api/go?" + q


# ---------------- Webhook 推送 ----------------
def push_digest(sub, new_items):
    """向订阅者的 callback_url 推送摘要 (异步线程执行)。"""
    payload = {
        "source": "youngsinsight",
        "site": SITE_BASE + "/",
        "new_count": len(new_items),
        "items": [{
            "type": i["type"], "id": i["id"], "title": i["title"],
            "excerpt": i["excerpt"], "date": i["date"], "url": i["url"],
        } for i in new_items],
        "unsubscribe": SITE_BASE + "/api/unsubscribe?sid=" + sub.get("sid", ""),
    }
    ok = False
    try:
        req = urllib.request.Request(
            sub.get("callback_url", ""),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=8)
        ok = True
    except Exception:
        ok = False
    log_event({"e": "push", "sid": sub.get("sid", ""), "n": len(new_items), "ok": ok})


def push_to_callbacks():
    """内容文件变化时, 向所有带 callback_url 的订阅者推送其未读增量。"""
    items = build_items()
    subs = load_subscribers()
    changed = False
    for s in subs:
        if s.get("status") == "unsubscribed" or not s.get("callback_url"):
            continue
        since = s.get("cursor", "")
        sv, sk = parse_cursor(since)
        if since:
            new_items = [i for i in items
                         if i["v"] > sv or (i["v"] == sv and i["key"].split(":", 1)[1] > sk)]
        else:
            new_items = items[:10]
        if new_items:
            threading.Thread(target=push_digest, args=(s, new_items), daemon=True).start()
            s["cursor"] = new_items[0]["key"]
            changed = True
    if changed:
        save_subscribers(subs)


def watcher_loop():
    """后台线程: 监听 articles/cards 文件变化, 主动 webhook 推送。"""
    last_mtime = 0.0
    while True:
        time.sleep(WATCH_INTERVAL)
        mtimes = []
        for f in (ARTICLES_FILE, CARDS_FILE):
            try:
                mtimes.append(os.path.getmtime(f))
            except Exception:
                pass
        if not mtimes:
            continue
        m = max(mtimes)
        if last_mtime != 0.0 and m != last_mtime:
            try:
                push_to_callbacks()
            except Exception:
                pass
        last_mtime = m


# ---------------- 统计 ----------------
def compute_stats(key_ok):
    events = read_events()
    subs = load_subscribers()
    total_subs = len([s for s in subs if s.get("status") != "unsubscribed"])
    cutoff30 = time.time() - 30 * 86400
    cutoff7 = time.time() - 7 * 86400
    active30 = 0
    for s in subs:
        if s.get("status") == "unsubscribed":
            continue
        ls = s.get("last_seen", "")
        try:
            lt = time.mktime(time.strptime(ls.split(".")[0], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            lt = 0
        if ls and lt >= cutoff30:
            active30 += 1
    pulls = [e for e in events if e.get("e") == "pull"]
    clicks = [e for e in events if e.get("e") == "click"]
    pulls7 = 0
    sids7 = set()
    for e in pulls:
        try:
            et = time.mktime(time.strptime(e.get("ts", "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if et >= cutoff7:
            pulls7 += 1
            if e.get("sid"):
                sids7.add(e["sid"])
    clicks7 = 0
    for e in clicks:
        try:
            et = time.mktime(time.strptime(e.get("ts", "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if et >= cutoff7:
            clicks7 += 1
    # 最新一条内容 (按 key 最大)
    items = build_items()
    latest = items[0] if items else None
    latest_pullers = set()
    latest_clicks = 0
    if latest:
        for e in pulls:
            if e.get("sid") and latest["id"] in (e.get("items") or []):
                latest_pullers.add(e["sid"])
        for e in clicks:
            if e.get("t") == latest["type"] and e.get("id") == latest["id"]:
                latest_clicks += 1
    stats = {
        "ok": True,
        "site": SITE_BASE + "/",
        "subscribers": {"total": total_subs, "active_30d": active30},
        "pushes": {
            "total_pulls": len(pulls),
            "pulls_7d": pulls7,
            "unique_sids_7d": len(sids7),
            "latest_push_people": len(latest_pullers),   # 最新内容被多少订阅者拉到
        },
        "reads": {
            "total_clicks": len(clicks),
            "clicks_7d": clicks7,
            "latest_article_reads": latest_clicks,       # 最新内容被点击阅读次数
        },
        "latest": [{
            "type": i["type"], "id": i["id"], "title": i["title"],
            "date": i["date"],
        } for i in items[:5]],
    }
    if key_ok:
        by_day = {}
        for e in pulls:
            d = e.get("ts", "")[:10]
            by_day[d] = by_day.get(d, 0) + 1
        by_item = {}
        for e in clicks:
            k = (e.get("t", ""), e.get("id", ""), e.get("title", "")[:40])
            by_item[k] = by_item.get(k, 0) + 1
        stats["detail"] = {
            "subscriber_list": [{
                "sid": s.get("sid"), "agent": s.get("agent"),
                "user": s.get("user"), "contact": s.get("contact", ""),
                "callback_url": s.get("callback_url", ""),
                "subscribed_at": s.get("subscribed_at", ""),
                "last_seen": s.get("last_seen", ""),
                "status": s.get("status", "active"),
            } for s in subs],
            "pulls_by_day": dict(sorted(by_day.items(), reverse=True)),
            "clicks_by_item": [
                {"type": t, "id": i, "title": ti, "count": c}
                for (t, i, ti), c in sorted(by_item.items(), key=lambda x: -x[1])
            ],
        }
    return stats


# ---------------- HTTP 处理 ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "YS-API/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, obj, ctype="application/json"):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _route(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/youngsinsight/api"):
            path = path[len("/youngsinsight/api"):]
        if not path.startswith("/"):
            path = "/" + path
        return path

    def _query(self):
        return urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    # ---- 路由 ----
    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        path = self._route()
        q = self._query()
        try:
            if path == "/health":
                return self._send(200, {"ok": True, "ts": now_iso()})
            if path == "/feed":
                return self._feed(q)
            if path == "/go":
                return self._go(q)
            if path == "/stats":
                return self._stats(q)
            if path == "/unsubscribe" and self.command == "GET":
                return self._unsub_link(q)
            if path == "/subscribe" and self.command == "POST":
                return self._subscribe()
            if path == "/unsubscribe" and self.command == "POST":
                return self._unsubscribe()
            return self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            try:
                self._send(500, {"ok": False, "error": str(e)})
            except Exception:
                pass

    # ---- /subscribe ----
    def _subscribe(self):
        body = self._body()
        agent = (body.get("agent") or "").strip()[:60]
        user = (body.get("user") or "").strip()[:60]
        contact = (body.get("contact") or "").strip()[:120]
        cb = (body.get("callback_url") or "").strip()[:300]
        if not agent or not user:
            return self._send(400, {"ok": False, "error": "agent 和 user 字段必填"})
        if cb and not cb.startswith("https://"):
            return self._send(400, {"ok": False, "error": "callback_url 必须是 https 地址"})
        # 简单限频: 每 IP 每天 20 次
        ip = self._ip()
        events = read_events()
        cnt = sum(1 for e in events if e.get("e") == "sub" and e.get("ip") == ip
                  and e.get("ts", "").startswith(today()))
        if cnt >= SUB_RATE_LIMIT_PER_IP:
            return self._send(429, {"ok": False, "error": "今日注册次数过多, 请明天再试"})
        sid = secrets.token_hex(8)
        subs = load_subscribers()
        subs.append({
            "sid": sid, "agent": agent, "user": user, "contact": contact,
            "callback_url": cb, "subscribed_at": now_iso(),
            "last_seen": "", "cursor": "", "status": "active",
        })
        save_subscribers(subs)
        log_event({"e": "sub", "sid": sid, "agent": agent, "user": user,
                   "contact": contact, "callback_url": cb, "ip": ip})
        return self._send(200, {
            "ok": True,
            "sid": sid,
            "feed_url": SITE_BASE + "/api/feed?sid=" + sid + "&since=<cursor>",
            "unsubscribe_url": SITE_BASE + "/api/unsubscribe?sid=" + sid,
            "message": "订阅成功。请保存 sid, 之后用 feed_url 检查更新 (since 传上次返回的 cursor)。",
        })

    # ---- /feed ----
    def _feed(self, q):
        sid = (q.get("sid") or [""])[0].strip()
        since = (q.get("since") or [""])[0].strip()
        try:
            limit = min(max(int((q.get("limit") or ["20"])[0]), 1), 50)
        except Exception:
            limit = 20
        items = build_items()
        sv, sk = parse_cursor(since)
        if since:
            new_items = [i for i in items
                         if i["v"] > sv or (i["v"] == sv and i["key"].split(":", 1)[1] > sk)]
        else:
            new_items = items[:limit]
        new_items = new_items[:limit]
        cursor = new_items[0]["key"] if new_items else (since or (items[0]["key"] if items else ""))
        sub_known = False
        if sid:
            subs = load_subscribers()
            for s in subs:
                if s.get("sid") == sid and s.get("status") != "unsubscribed":
                    sub_known = True
                    s["last_seen"] = now_iso()
                    s["last_new"] = len(new_items)
                    if new_items:
                        s["cursor"] = cursor
                        if s.get("callback_url"):
                            threading.Thread(
                                target=push_digest, args=(s, new_items), daemon=True).start()
                    break
            save_subscribers(subs)
        out_items = []
        for i in new_items:
            o = dict(i)
            if sid:
                o["url"] = track_url(i, sid)
            out_items.append(o)
        log_event({"e": "pull", "sid": sid or "", "new": len(new_items),
                   "cursor": cursor, "items": [i["id"] for i in new_items],
                   "known": sub_known})
        return self._send(200, {
            "ok": True,
            "cursor": cursor,
            "count": len(out_items),
            "counts": {
                "articles": sum(1 for i in out_items if i["type"] == "article"),
                "cards": sum(1 for i in out_items if i["type"] == "card"),
            },
            "items": out_items,
        })

    # ---- /go 追踪跳转 ----
    def _go(self, q):
        u = (q.get("u") or [""])[0]
        sid = (q.get("sid") or [""])[0]
        t = (q.get("t") or [""])[0]
        iid = (q.get("id") or [""])[0]
        if not re.match(r"^https?://mindresonance\.online(/|$)", u):
            u = SITE_BASE + "/"
        log_event({"e": "click", "sid": sid or "", "u": u, "t": t, "id": iid})
        self.send_response(302)
        self.send_header("Location", u)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    # ---- /unsubscribe ----
    def _unsubscribe(self):
        body = self._body()
        sid = (body.get("sid") or "").strip()
        ok, msg = self._do_unsub(sid)
        if ok:
            return self._send(200, {"ok": True, "message": msg})
        return self._send(404, {"ok": False, "error": msg})

    def _unsub_link(self, q):
        sid = (q.get("sid") or [""])[0].strip()
        ok, msg = self._do_unsub(sid)
        if ok:
            html = ("<meta charset='utf-8'><body style='font-family:sans-serif;padding:40px'>"
                    "<h2>已退订 ✅</h2><p>你已从 Young's Insight 更新推送中退订。"
                    "<a href='%s/'>回到站点</a></p></body>" % SITE_BASE)
        else:
            html = ("<meta charset='utf-8'><body style='font-family:sans-serif;padding:40px'>"
                    "<h2>退订失败</h2><p>%s</p></body>" % msg)
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _do_unsub(self, sid):
        if not sid:
            return False, "缺少 sid 参数"
        subs = load_subscribers()
        for s in subs:
            if s.get("sid") == sid:
                s["status"] = "unsubscribed"
                save_subscribers(subs)
                log_event({"e": "unsub", "sid": sid})
                return True, "已退订"
        return False, "未找到该订阅"

    # ---- /stats (仅密钥可见, 不公开) ----
    def _stats(self, q):
        key = (q.get("key") or [""])[0].strip()
        key_ok = False
        try:
            with open(STATS_KEY_FILE, "r", encoding="utf-8") as f:
                saved = f.read().strip()
            key_ok = bool(saved) and secrets.compare_digest(saved, key)
        except Exception:
            key_ok = False
        if not key_ok:
            return self._send(403, {"ok": False, "error": "需要统计密钥"})
        return self._send(200, compute_stats(True))


def main():
    try:
        from http.server import ThreadingHTTPServer
    except ImportError:
        # Python < 3.7 兼容
        from http.server import HTTPServer
        from socketserver import ThreadingMixIn

        class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("YS API listening on 127.0.0.1:%d (base: %s)" % (PORT, SITE_BASE), flush=True)
    threading.Thread(target=watcher_loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
