#!/bin/bash
# Young's Insight 订阅 API — ECS 一次性安装脚本（GitHub Actions runner 执行）
# 幂等：可重复运行
set -e

BASE=/var/www/youngsinsight
DATA=$BASE/.ysdata

echo "==> [1/5] 初始化数据目录"
mkdir -p $DATA
chmod 755 $DATA

echo "==> [2/5] 写入统计密钥"
if [ -n "$YS_STATS_KEY" ]; then
  echo -n "$YS_STATS_KEY" > $DATA/stats_key
fi
chmod 600 $DATA/stats_key
chmod 644 $BASE/ys_api.py $BASE/subscribe.html $BASE/ys-subscribe.md 2>/dev/null || true

echo "==> [3/5] 安装 systemd 服务"
cat > /etc/systemd/system/ys-api.service <<'EOF'
[Unit]
Description=Young's Insight Subscription API
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /var/www/youngsinsight/ys_api.py
WorkingDirectory=/var/www/youngsinsight
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ys-api >/dev/null 2>&1 || true
systemctl restart ys-api
sleep 1

echo "==> [4/5] 注入 nginx 反代配置（幂等）"
python3 <<'PYEOF'
import os, re, shutil, sys

LOC_BLOCK = """    location /youngsinsight/api/ {
        proxy_pass http://127.0.0.1:8080/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
"""

def scan(path):
    out = []
    try:
        for fn in sorted(os.listdir(path)):
            if fn.endswith(".conf"):
                out.append(os.path.join(path, fn))
    except Exception:
        pass
    return out

files = scan("/etc/nginx/conf.d") + ["/etc/nginx/nginx.conf"]
targets = []
for p in files:
    try:
        src = open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        continue
    if "location /youngsinsight/api/" in src:
        print("already present:", p)
        continue
    if re.search(r"location\s+(?:=|~|~\*|\^~)?\s*/youngsinsight\b", src):
        targets.append(p)

if not targets:
    print("WARN: no youngsinsight location found, skip nginx injection")
    sys.exit(0)

for p in targets:
    src = open(p, encoding="utf-8", errors="replace").read()
    m = re.search(r"^(\s*)location\s+(?:=|~|~\*|\^~)?\s*/youngsinsight\b", src, re.M)
    if not m:
        continue
    indent = m.group(1) if m.group(1) else "    "
    block = "\n" + "\n".join(indent + line if line.strip() else line
                             for line in LOC_BLOCK.strip("\n").split("\n")) + "\n"
    backup = p + ".bak-ysapi"
    if not os.path.exists(backup):
        shutil.copy(p, backup)
    src2 = src[:m.start()] + block + src[m.start():]
    open(p, "w", encoding="utf-8").write(src2)
    print("injected into:", p)
PYEOF

if ! nginx -t 2>&1; then
  echo "!! nginx -t failed, restoring backups"
  for b in /etc/nginx/conf.d/*.bak-ysapi /etc/nginx/nginx.conf.bak-ysapi; do
    [ -f "$b" ] && cp "$b" "${b%.bak-ysapi}" || true
  done
  nginx -t
  exit 1
fi
systemctl reload nginx

echo "==> [5/5] 验证"
sleep 1
curl -s http://127.0.0.1:8080/health || { echo "!! API health failed"; exit 1; }
systemctl is-active ys-api
echo "SETUP DONE"
