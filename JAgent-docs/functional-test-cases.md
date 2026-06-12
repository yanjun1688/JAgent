# 功能测试用例（2026-06-08 变更验证）

> 验证 flattening 移除后 LLM 变量路径解析的正确性。
> 全部用例依赖 httpbin.org（在线服务），部分可用 `httpbin.org/anything` 替代。

**启动**：
```powershell
uvicorn harness.api.serve:app --reload --port 8000
```
前端打开 `http://localhost:8000` → Create Run → 粘贴 intent → 执行

---

## 基础 GET（无依赖）

### TC01 — 单步 GET 返回结果
**intent**: 请求 httpbin.org/uuid 获取 uuid
**预期**: 直接返回结果，无变量依赖
**观察点**: LLM 不应生成 `$` 语法，直接展示 body.uuid

### TC02 — 单步 GET /ip 返回 IP
**intent**: 请求 httpbin.org/ip 获取我的 IP 地址
**预期**: 直接返回结果
**观察点**: body 里的 `origin` 字段是否正确展示

### TC03 — 单步 GET /headers
**intent**: 请求 httpbin.org/headers 获取请求头信息
**预期**: 直接返回结果
**观察点**: headers 字段结构清晰可见

### TC04 — 单步 POST
**intent**: 向 httpbin.org/post 发送一条测试消息，内容为 {"msg": "hello"}
**预期**: POST 成功，body.json 回显 msg
**观察点**: 无变量依赖，输入写死

---

## 单链依赖（核心验证）

### TC05 — GET uuid → POST uuid（body.field 路径）
**intent**: 去 httpbin.org/uuid 拿到 uuid 的值，然后 POST 到 httpbin.org/post
**结构**: s1=GET /uuid, s2=POST /post depends_on s1
**预期**: s2.body.uuid 应为实际 uuid 字符串
**观察点**: **关键** — LLM 写 `$s1.body.uuid` 还是 `$s1.uuid`？如果是 `$s1.uuid`，实际值会变成 `"None"`（flattening 移除后 flat 字段不存在）

### TC06 — GET ip → POST 上报（字段名推断）
**intent**: 获取我的公网 IP，然后上报到 httpbin.org/post
**结构**: s1=GET /ip, s2=POST /post depends_on s1
**预期**: s2 应包含 IP 值
**观察点**: 注意 httpbin.org/ip 返回的 key 是 `origin` 不是 `ip`。LLM 能否正确推断为 `$s1.body.origin`？如果写了 `$s1.body.ip` 则值为 `None`

### TC07 — GET uuid → 保存到文件
**intent**: 去 httpbin.org/uuid 获取一个 uuid，然后保存到 file.txt 中
**结构**: s1=GET /uuid, s2=file_op write depends_on s1
**预期**: s2.content 应为 uuid 字符串
**观察点**: file_op 参数 `content` 应该用 `$s1.body.uuid`。如果用 bare `$s1` 会写入整个 dict

### TC08 — GET uuid → POST 自定义 body 结构
**intent**: 去 httpbin.org/uuid 拿到 uuid，然后以 {"id": "uuid的值", "source": "httpbin"} 的形式 POST 到 httpbin.org/post
**结构**: s1=GET /uuid, s2=POST /post depends_on s1
**预期**: s2.body.id = uuid 值, s2.body.source = "httpbin"
**观察点**: LLM 需要在 body 里写 `$s1.body.uuid` 同时保留文字 `"httpbin"`

---

## 多源汇聚（fan-in DAG）

### TC09 — uuid + IP 合并 POST
**intent**: 去 httpbin.org/uuid 拿到 uuid，同时去 httpbin.org/ip 拿到 IP，然后把 uuid 和 IP 合并 POST 到 httpbin.org/post
**结构**: s1=GET /uuid, s2=GET /ip, s3=POST /post depends_on s1,s2
**预期**: s3.body.uuid=实际uuid, s3.body.origin=实际IP
**观察点**: s3 依赖两个上游，变量路径分别是 `$s1.body.uuid` 和 `$s2.body.origin`

### TC10 — uuid + headers 合并 POST
**intent**: 从 httpbin.org/uuid 拿 uuid，同时从 httpbin.org/headers 拿请求头，然后把 uuid 和 user-agent 一起 POST 到 httpbin.org/post
**结构**: s1=GET /uuid, s2=GET /headers, s3=POST /post depends_on s1,s2
**预期**: s3.body.uuid + s3.body.user-agent
**观察点**: User-Agent 在 headers 返回结构中的路径是 `body.headers.["User-Agent"]` — 多层嵌套

---

## 链式传递（三层依赖）

### TC11 — uuid → POST → 保存结果
**intent**: 去 httpbin.org/uuid 拿到 uuid，POST 到 httpbin.org/post，然后把 POST 返回的 json 字段保存到文件
**结构**: s1=GET /uuid, s2=POST /post depends_on s1, s3=file_op write depends_on s2
**预期**: s3.content = $s2.body.json（httpbin 的 echo 行为）
**观察点**: 三层依赖的变量路径链 `$s1.body.uuid` → `$s2.body.json`

### TC12 — ip → 加工 → 保存
**intent**: 从 httpbin.org/ip 获取 IP，然后构造一条消息 "My IP is: 获取到的IP"，保存到 ip.txt
**结构**: s1=GET /ip, s2=file_op write depends_on s1
**预期**: s2.content = "My IP is: 1.2.3.4"
**观察点**: 需要字符串拼接语法，如果 LLM 用 `$s1.body.origin` 嵌入到文本中

---

## 并行执行

### TC13 — 两个独立 GET
**intent**: 同时请求 httpbin.org/uuid 和 httpbin.org/ip
**结构**: s1=GET /uuid, s2=GET /ip（互不依赖）
**预期**: 两个 step 同时执行（同一 layer）
**观察点**: 无变量引用，纯并行测试

### TC14 — 双路并行 + 合并
**intent**: 同时获取 uuid 和 ip，等两者都完成后，把 uuid 作为 id 和 IP 作为 address 合并 POST 到 httpbin.org/post
**结构**: s1=GET /uuid, s2=GET /ip, s3=POST /post depends_on s1,s2
**预期**: s3.body.id=uuid, s3.body.address=origin
**观察点**: 变量路径 `$s1.body.uuid` 和 `$s2.body.origin`

---

## 嵌套路径与复杂结构

### TC15 — 请求 anything 返回全结构
**intent**: 向 httpbin.org/anything 发送一个 GET 请求，查看返回的所有字段
**预期**: 直接展示结果
**观察点**: 返回结构包含 method, url, headers, args 等字段

### TC16 — 提取嵌套字段
**intent**: 向 httpbin.org/anything 发送 GET 请求，提取返回结果中的 url 字段，POST 到 httpbin.org/post
**结构**: s1=GET /anything, s2=POST /post depends_on s1
**预期**: s2.body.url = $s1.body.url
**观察点**: 路径 `$s1.body.url`（/anything 的返回值里 url 在 body 顶层）

---

## 重试与容错（不需要 httpbin 特定行为）

### TC17 — 请求不存在的路径
**intent**: 访问 httpbin.org/status/404 看看返回什么
**预期**: status_code=404，LLM 应能读取结果
**观察点**: 即使 status_code 非 200，body 字段仍然存在，LLM 应该能看到

### TC18 — 请求延时端点
**intent**: 访问 httpbin.org/delay/3，等待 3 秒后获取结果
**预期**: tool 等待 3 秒后成功返回
**观察点**: elapsed_ms 应为 ~3000，超时未触发

---

## 幂等性验证

### TC19 — 重复执行同一 intent
**intent**: 去 httpbin.org/uuid 拿到 uuid，然后 POST 到 httpbin.org/post
**执行方式**: 连续提交 2 次同样的 intent
**预期**: 第二次的 GET 请求应触发幂等缓存（Cache HIT）
**观察点**: 日志中出现 `[idem] Cache HIT` 且第二个 run 的 ToolCalled 比第一次少

---

## 混合场景

### TC20 — 三步链式：GET → 加工 → POST
**intent**: 从 httpbin.org/uuid 获取 uuid，用这个 uuid 作为参数请求 httpbin.org/anything?uuid=获取到的uuid，然后把 anything 返回的 url 字段保存到 url.txt
**结构**: s1=GET /uuid, s2=GET /anything?uuid=$s1.body.uuid, s3=file_op write depends_on s2
**预期**: s2 的 URL 参数包含实际 uuid，s3 内容为 s2 返回的 url
**观察点**: url 参数中的变量解析（LLM 需要在 URL 里嵌入 `$s1.body.uuid`）
