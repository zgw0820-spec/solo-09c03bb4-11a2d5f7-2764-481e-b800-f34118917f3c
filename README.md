# C Build Cache Service

面向 CI 并行构建场景的小型 C 工程编译服务：调用方通过 JSON 按相对路径提交
`.c` 源码和 `.h` 头文件，服务用固定 GCC 参数编译，按"源文件 + 实际引用的各级
项目头文件（路径和内容）"复用目标文件，只链接本次提交对应的目标文件，返回
可执行文件和复用数量。

## 功能

- **JSON 提交整个工程**：`POST /build`，`files` 为 `{path, content}` 列表，
  路径为项目相对 POSIX 路径（仅 `.c` / `.h`）。
- **固定 GCC 参数**：`-O2 -Wall -Wextra -std=c11`，并用 `-I<项目根目录>` 把
  项目根加入头文件搜索路径。
- **目标文件级缓存**：缓存键 = 固定编译参数 + 源文件路径与内容 + 该源文件
  传递闭包内所有项目头文件的路径与内容 的 SHA-256。四者一致才复用。
- **Singleflight**：相同编译输入同时到达时，按缓存键加锁，全进程只编译一次，
  其余请求等待后直接复用结果（`/stats` 中 `singleflight_waits_total` 可见）。
- **同名头文件遮蔽**：`#include` 解析规则与 GCC 一致（引号包含先查包含指令
  所在文件的目录，再查项目根；尖括号只查项目根）。新增同名头文件改变引用
  指向时，受影响单元的依赖闭包变化 → 缓存键变化 → 自动重编译，未受影响
  的单元继续命中缓存。
- **只链接本次提交的目标文件**，每次构建产出独立可执行文件。
- **失败诊断**：编译/链接失败返回 `400`，`detail.diagnostics` 为 GCC 的
  stderr；只有成功编译的目标文件才进入缓存（失败不会污染缓存）。
- **拆分符号模式（可选）**：`POST /build` 传 `"split_symbols": true`，工程以
  `-g` 编译；返回的程序已剥离调试信息但**保留 ELF Build ID**（响应头
  `X-Build-Id`），配套符号文件按该 ID 持久保存，随时可用
  `GET /symbols/{build_id}` 取回——服务重启后依然有效。详见下文
  「拆分符号与现场调试」。
- **缓存持久化**：对象缓存落在磁盘目录，Docker 部署时挂载命名卷，重启后
  继续复用。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`：

- Swagger UI：<http://localhost:8000/docs> —— `POST /build` 已内置可运行的
  三文件示例（普通模式和 `split_symbols` 拆分符号模式各一），点
  **Try it out → Execute** 即可发起构建，响应区出现 **Download file** 链接
  下载可执行文件；复用数量在响应头 `X-Compiled-Count` / `X-Reused-Count` /
  `X-Unit-Count` 中。拆分符号模式下从响应头 `X-Build-Id` 复制 Build ID，
  即可在 `GET /symbols/{build_id}` 中下载配套符号文件。
- `GET /symbols`：已保存符号的 Build ID 列表。
- `GET /stats`：缓存对象数（普通/调试分开计数）、累计编译/复用次数、
  singleflight 等待次数、符号保存/复用/冲突次数。
- `GET /healthz`：健康检查。

对象缓存与符号库都保存在命名卷 `cbuild-cache` 中，容器重启后仍然有效。

## API

### `POST /build`

请求体：

```json
{
  "output": "app",
  "files": [
    {"path": "main.c", "content": "#include <stdio.h>\n#include \"util.h\"\nint main(void){ printf(\"%d\\n\", util_answer()); }"},
    {"path": "util.c", "content": "#include \"util.h\"\nint util_answer(void){ return UTIL_ANSWER; }"},
    {"path": "util.h", "content": "#ifndef UTIL_H\n#define UTIL_H\n#define UTIL_ANSWER 42\nint util_answer(void);\n#endif"}
  ]
}
```

- `files[].path`：项目相对路径，只允许 `.c` / `.h`，拒绝 `..`、绝对路径、
  重复路径；最多 128 个文件，单文件最大 256 KB。
- `output`：可选，下载文件名（默认 `app`）。
- `split_symbols`：可选，默认 `false`。为 `true` 时启用拆分符号模式
  （见下文「拆分符号与现场调试」）。

成功（`200`）：`application/octet-stream` 的可执行文件，响应头：

| 头 | 含义 |
| --- | --- |
| `X-Compiled-Count` | 本次请求实际编译的单元数 |
| `X-Reused-Count` | 本次请求复用的单元数（含 singleflight 等待） |
| `X-Unit-Count` | 本次提交的编译单元总数 |
| `X-Build-Id` | 程序的 ELF Build ID（仅拆分符号模式） |
| `X-Symbols-Status` | `stored`（本次新存）/ `reused`（已有相同符号，仅拆分符号模式） |

失败（`400`）：

```json
{
  "detail": {
    "stage": "compile",
    "message": "compilation of main.c failed",
    "unit": "main.c",
    "diagnostics": "main.c: In function 'main':\nmain.c:2:5: error: ..."
  }
}
```

`stage` 取值：`validation`（请求非法）/ `compile` / `link` / `symbols`
（符号提取或保存失败，此时为 500）。拆分符号模式下若同一 Build ID 已存在
**内容不同**的符号文件，返回 `409`（`stage` 为 `symbols`），已保存的原件
不会被覆盖，本次构建的程序不返回。

### `GET /symbols`

列出所有已保存符号的 Build ID：

```json
{"count": 1, "build_ids": ["5d254717428fe32bd3b369b69c1a825f25ec5248"]}
```

### `GET /symbols/{build_id}`

按 Build ID 下载配套符号文件（`<build_id>.debug`，
`application/octet-stream`）。Build ID 格式非法返回 `400`，未收录返回
`404`。符号库持久化在磁盘目录（Docker 部署时在命名卷上），服务重启后
仍可下载。

### curl 示例

```bash
curl -sS -X POST http://localhost:8000/build \
  -H 'Content-Type: application/json' \
  -d @request.json \
  -o app -D -        # 响应头里看 X-Compiled-Count / X-Reused-Count
chmod +x app && ./app
```

## 拆分符号与现场调试

巡检程序在现场崩溃时，值班员从核心转储/程序中读出 ELF Build ID，即可取回
配套符号定位源码行。构建时开启拆分符号模式：

```bash
# 1. 拆分符号构建：返回的程序已剥离调试信息，但保留 Build ID
curl -sS -X POST http://localhost:8000/build \
  -H 'Content-Type: application/json' \
  -d '{"split_symbols": true, "output": "app", "files": [...]}' \
  -o app -D headers.txt
BID=$(grep -i '^X-Build-Id:' headers.txt | tr -d '\r' | awk '{print $2}')

# 2. 核对程序里的 Build ID（与响应头一致）
readelf -n app | grep 'Build ID'

# 3. 随时按 Build ID 下载配套符号（服务重启后也可以）
curl -sS http://localhost:8000/symbols/$BID -o app.debug

# 4. GDB 定位：把符号里的 /project 前缀替换成仓库检出路径
gdb -q app \
  -ex "symbol-file app.debug" \
  -ex "set substitute-path /project $PWD" \
  -ex "info line *main"
```

工作方式与设计约束：

- **构建**：单元以 `-g`（加固定参数）编译，链接时加
  `-Wl,--build-id=sha1`；随后 `objcopy --only-keep-debug` 抽出符号文件，
  `strip --strip-debug` 剥离程序中的调试信息。`.note.gnu.build-id` 是
  已分配段，剥离后保留在程序里。
- **Build ID 与临时目录无关**：编译时加
  `-ffile-prefix-map=<临时目录>=/project`，调试信息中的编译目录和源码
  路径全部改写为 `/project/...` 前缀（与仓库相对路径一一对应），因此
  构建临时目录的变化不会改变目标文件内容，也就不会改变 Build ID——
  同一工程在任何时刻重建都得到同一个 Build ID。
- **调试对象缓存按整份工程识别**：拆分符号模式下，每个目标文件的缓存键
  覆盖提交的全部文件（而非普通模式的单单元依赖闭包），且使用独立的缓存
  目录与键命名空间，与普通构建互不影响。一份符号始终对应唯一的一份
  完整工程状态。
- **先存符号，再返回程序**：符号文件写入临时文件、fsync、原子发布后
  （并对目录 fsync）才返回响应；程序只有在符号完整保存后才会被返回。
- **同 ID 冲突规则**：发布是"比较-并设置"——目标已存在时，内容一致则
  复用（`X-Symbols-Status: reused`），内容不同则本次构建返回 `409` 且
  **保留原件**。落盘用 `os.link` 原子占位（已存在即失败，绝不覆盖），
  并发/多进程写入都遵循同一规则；并发相同构建时符号只保存一次。

## 缓存设计要点

```
key(unit) = SHA256( 固定GCC参数
                   ‖ 源文件路径 ‖ 源文件内容
                   ‖ 排序后的每个依赖头文件(路径 ‖ 内容) )
```

- 依赖闭包由静态解析 `#include` 得到，解析规则与 GCC 一致（引号包含先查
  指令所在文件目录，再查 `-I` 项目根；尖括号查项目根）。解析不到项目文件
  的包含视为系统头，不参与缓存键。
- 未引用的文件不影响缓存键：提交里新增无关文件不会造成失效。
- 新增同名头文件改变解析结果时，闭包集合变化 → 键变化 → 仅受影响单元
  重编译。
- 并发同一键：每键一把锁，双重检查 + 原子 rename 发布，编译只发生一次；
  编译失败抛异常，缓存不写入任何内容。

拆分符号模式另有独立的一套键（`cbuild-debug-v1`）与缓存目录：

```
debug_key(unit) = SHA256( "cbuild-debug-v1" ‖ 固定GCC参数 ‖ "-g"
                       ‖ 整份工程(排序后的每个文件: 路径 ‖ 内容)
                       ‖ 源文件路径 )
```

`-ffile-prefix-map` 的值含临时目录、逐次构建变化，但其效果（把临时目录
改写为 `/project`）是恒定的，因此不进入缓存键。

## 本地开发与验证

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt httpx
CACHE_DIR=./data/cache DEBUG_CACHE_DIR=./data/cache-debug \
  SYMBOLS_DIR=./data/symbols WORK_DIR=./data/work \
  .venv/bin/python -m uvicorn app.main:app --port 8000

# 端到端验证（需要服务已启动；假设缓存为空）
.venv/bin/python tests/e2e_test.py http://127.0.0.1:8000

# 符号存储单元测试（不需要服务；含并发写入与冲突规则）
python3 tests/symbol_store_test.py
```

`tests/e2e_test.py` 覆盖：首次构建全编译、原样重提全复用、头文件变更只
重编译受影响单元、同名头遮蔽、8 路并发相同构建全局只编译一次、6 路并发
不同工程互不干扰、编译/链接失败诊断、失败不入缓存、路径校验；拆分符号
模式覆盖：Build ID 响应头与格式、程序剥离调试信息但保留 Build ID、
重复构建 Build ID 不变（临时目录不外泄）、符号按 ID 下载且路径为
`/project/...` 仓库相对路径、符号文件与程序 Build ID 一致、未知/非法
ID 的 404/400、调试缓存与普通缓存互相独立、整份工程键（新增未引用文件
导致调试构建全量重编译而普通构建不受影响）、8 路并发拆分构建符号只保存
一次且无冲突。

重启后取回的验证（本地手动）：

```bash
# 构建一次拆分符号版本并记录 X-Build-Id，然后重启 uvicorn（数据目录保留）
curl -sS http://127.0.0.1:8000/symbols/<build_id> -o after.debug   # 200，内容一致
```

## 遗留事项 / 限制

- 依赖闭包是**静态解析**的保守近似：`#if/#ifdef` 条件包含、宏拼接包含
  （`#include FOO`）不展开，可能把未实际参与编译的头文件算进键里（只会
  多重编译，不会错用旧产物）。如需精确依赖，可在首次编译时用 `gcc -MMD`
  校准。
- 只链接 libc，未暴露额外链接库（如 `-lm`）或自定义编译参数的入口——
  参数按需求固定。拆分符号模式沿用 `-O2`：GDB 可用，但优化代码中部分
  变量可能被优化掉（与线上产物的常规取舍一致）。
- 拆分符号模式依赖 binutils（`readelf` / `objcopy` / `strip`，镜像已
  安装）；符号发布依赖文件系统支持硬链接（`os.link`，命名卷/常见 Linux
  文件系统均支持）。符号文件未写入 `--add-gnu-debuglink` 链接名，匹配
  完全依靠 Build ID。
- 单请求内各单元串行编译（工程小、并发主要来自多请求）；如需可改为
  请求内并行。
- 服务会执行调用方提交的 C 代码编译产物之外的编译器本身，请只在可信
  网络内对可信代码开放；如需对外，应加认证、资源限额与沙箱（如
  seccomp/只读根fs/超时已部分具备）。
- 缓存目录与符号库无容量上限与 LRU 淘汰，长期运行需自行清理或加配额。
- 本环境没有 Docker，镜像未实际构建；`Dockerfile` / `docker-compose.yml`
  已经过静态检查，行为与本地验证的 uvicorn 运行方式一致（本地已安装
  binutils 验证拆分符号全流程）。
