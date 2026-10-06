#!/bin/sh
# 可直接执行的端到端 HTTP 调用示例（依赖 curl）。
# 用法：./examples.sh            # 默认 http://127.0.0.1:8000
#       BASE=http://localhost:8000 ./examples.sh
set -eu

BASE="${BASE:-http://127.0.0.1:8000}"

post_publish() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/publish" \
    -H 'Content-Type: application/json' -d "$1"
}

post_merge() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/merge" \
    -H 'Content-Type: application/json' -d "$1"
}

post_relocate() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/relocate" \
    -H 'Content-Type: application/json' -d "$1"
}

post_redirect() {
  curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/redirects" \
    -H 'Content-Type: application/json' -d "$1"
}

# 只打印 HTTP 状态码与 308 跳转目标（不跟随跳转）
show_get() {
  printf '  GET %s -> HTTP %s, location=%s\n' "$1" \
    "$(curl -sS -o /dev/null -w '%{http_code}' "$BASE$1")" \
    "$(curl -sS -o /dev/null -w '%{redirect_url}' "$BASE$1")"
}

echo "== 健康检查"
curl -sS "$BASE/health"; echo

echo "== 当前修订号（初始为 0）"
curl -sS "$BASE/revision"; echo

echo "== 1) 首次发布必须包含 /index.html（预期 422）"
post_publish '{"expected_revision":0,"changes":[{"op":"upsert","path":"/about.html","content":"<p>about</p>"}]}'

echo "== 2) 首次发布：/index.html + /about.html（预期 200，revision=1）"
post_publish '{"expected_revision":0,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1><a href=\"/about.html\">关于</a> <a href=\"https://example.com\">外链不检查</a>"},
  {"op":"upsert","path":"/about.html","content":"<a href=\"/#top\">回到首页锚点</a>"}
]}'

echo "== 3) 删除仍被 index 引用的 about.html（预期 422，列出来源与错误目标）"
post_publish '{"expected_revision":1,"changes":[{"op":"delete","path":"/about.html"}]}'

echo "== 4) 用过期修订号发布（预期 409 conflict）"
post_publish '{"expected_revision":0,"changes":[]}'

echo "== 5) 第二批：替换 index 去掉 about 链接，同时删除 about；再加相对/目录/查询串链接（预期 200，revision=2）"
post_publish '{"expected_revision":1,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/docs/\">文档目录</a>"},
  {"op":"delete","path":"/about.html"},
  {"op":"upsert","path":"/docs/index.html","content":"<a href=\"../index.html?x=1#top\">查询参数不改变目标</a>"}
]}'

echo "== 6) 读取当前修订的完整页面集合"
curl -sS "$BASE/pages"; echo

echo "== 7) 读取历史 revision=1 的完整集合"
curl -sS "$BASE/pages?revision=1"; echo

echo "== 并行合入：基线仍是 revision 2（当前也是 2），先看本端视图"

echo "== 8) 其他人并行发布 revision 3：新增 /news.html 并替换 /index.html 加上新闻链接（预期 200，revision=3）"
post_publish '{"expected_revision":2,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/docs/\">文档目录</a> <a href=\"/news.html\">新闻</a>"},
  {"op":"upsert","path":"/news.html","content":"<a href=\"/#top\">回到首页</a>"}
]}'

echo "== 9) 并行合入：本端基于过期基线 2，只改未被触碰的 /docs/index.html（预期 200，revision=4，/news.html 原样保留）"
post_merge '{"base_revision":2,"changes":[
  {"op":"upsert","path":"/docs/index.html","content":"<a href=\"../index.html?x=1#top\">文档目录（本端更新）</a>"}
]}'

echo "== 10) 并行合入冲突：本端仍基于基线 2 改已被别人修改的 /index.html（预期 409，列出冲突路径与当前修订号，整批不写入）"
post_merge '{"base_revision":2,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1>本端旧视图</h1>"}
]}'

echo "== 11) 基线修订不存在（预期 404）"
post_merge '{"base_revision":99,"changes":[]}'

echo "== 12) 其他人从 revision 4 发布 revision 5：删除 /news.html 并清掉引用；本端基于旧基线 4 新增仍链接它的页面（预期 422，列出来源与错误目标，内容及修订号不变）"
post_publish '{"expected_revision":4,"changes":[
  {"op":"upsert","path":"/index.html","content":"<h1 id=\"top\">首页</h1> <a href=\"/docs/\">文档目录</a>"},
  {"op":"delete","path":"/news.html"}
]}'
post_merge '{"base_revision":4,"changes":[
  {"op":"upsert","path":"/mine.html","content":"<a href=\"/news.html\">新闻已被别人删除</a>"}
]}'

echo "== 13) 既有接口不受影响：/publish 过期 expected_revision 仍为 409"
post_publish '{"expected_revision":1,"changes":[]}'

echo "== 站点读取：真实路径直接返回 HTML（HTTP 200）"
show_get /index.html
show_get /docs/index.html
echo "  -- 未知路径返回 404"
show_get /no/such/page.html

echo "== 14) 登记旧地址跳转（不推进修订号）：/docs/old-guide.html -> /docs/index.html"
post_redirect '{"old_path":"/docs/old-guide.html","target_path":"/docs/index.html"}'
echo "  -- 跳转登记列表与当前修订号（修订号保持 5）"
curl -sS "$BASE/redirects"; echo
curl -sS "$BASE/revision"; echo
echo "  -- 旧地址访问：308 永久跳转到真实路径"
show_get /docs/old-guide.html

echo "== 15) 跳转登记冲突（均为 422，跳转与修订号不变）"
echo "  -- 旧地址是真实页面"
post_redirect '{"old_path":"/docs/index.html","target_path":"/index.html"}'
echo "  -- 旧地址已被其他跳转占用"
post_redirect '{"old_path":"/docs/old-guide.html","target_path":"/index.html"}'
echo "  -- 目标不是当前真实页面（指向另一条旧地址也在这一类，不形成跳转链）"
post_redirect '{"old_path":"/docs/chain.html","target_path":"/docs/old-guide.html"}'
echo "  -- 发布也不得占用旧地址（422，修订号不变）"
post_publish '{"expected_revision":5,"changes":[
  {"op":"upsert","path":"/docs/old-guide.html","content":"<p>占用旧地址应失败</p>"}
]}'

echo "== 16) 页面搬迁：/docs/index.html -> /manual/index.html（同一事务自动登记/改指跳转）"
post_relocate '{"expected_revision":5,"old_path":"/docs/index.html","new_path":"/manual/index.html"}'
echo "  -- 腾出的旧路径自动 308 到新路径；此前登记的跳转也改指新路径（无跳转链）"
show_get /docs/index.html
show_get /docs/old-guide.html
show_get /manual/index.html
curl -sS "$BASE/redirects"; echo
echo "  -- 搬迁到已被跳转占用的路径必须失败（422，页面、跳转、修订号均不变）"
post_relocate '{"expected_revision":6,"old_path":"/index.html","new_path":"/docs/index.html"}'

echo "== 17) 撤销跳转后才能复用旧地址"
echo "  -- 撤销前：发布到旧地址仍是 422"
post_publish '{"expected_revision":6,"changes":[
  {"op":"upsert","path":"/docs/index.html","content":"<p>still blocked</p>"}
]}'
curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/redirects/revoke" \
  -H 'Content-Type: application/json' -d '{"old_path":"/docs/index.html"}'
echo "  -- 撤销后 GET /docs/index.html 变为未知路径 404，修订号不变（仍为 6）"
show_get /docs/index.html
curl -sS "$BASE/revision"; echo
echo "  -- 现在可把页面搬迁进该路径（旧地址复用成功）"
post_relocate '{"expected_revision":6,"old_path":"/manual/index.html","new_path":"/docs/index.html"}'
show_get /docs/index.html
echo "  -- 撤销未登记的旧地址 -> 404"
curl -sS -w '\n  -> HTTP %{http_code}\n' -X POST "$BASE/redirects/revoke" \
  -H 'Content-Type: application/json' -d '{"old_path":"/never/registered.html"}'

echo "== 当前修订号与最终页面集合"
curl -sS "$BASE/revision"; echo
curl -sS "$BASE/pages"; echo
echo "== 历史快照不受跳转影响：revision=1 仍是搬迁前的原快照"
curl -sS "$BASE/pages?revision=1"; echo

