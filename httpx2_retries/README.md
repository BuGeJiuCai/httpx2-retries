# httpx2_retries

HTTPX2 的同步和异步重试层，支持 asyncio / uvloop / Trio。
Python >= 3.11，运行依赖为 `httpx2>=2.13,<3` 和 `anyio>=4,<5`。

提供两种入口：

| 入口 | 适用场景 | 重试范围 |
| --- | --- | --- |
| `RetryTransport` / `AsyncRetryTransport` | 继续使用原来的 client API，包括 stream | 发送、接收响应头、状态码与显式校验 |
| `retry_request` / `aretry_request` | 普通 API 请求，需要完整正文和业务校验 | 整个 client.send，包括正文读取、重定向和校验 |

helper 会缓冲完整响应，不适合无限流或超大下载。已交付给调用方的流式内容不能透明重试。

## 安装和目录

在 `E:\Github\TEST-HTTPX2` 父项目目录执行：

```powershell
uv pip install --python .venv/Scripts/python.exe ./httpx2_retries
# 使用 Trio 时：
uv pip install --python .venv/Scripts/python.exe './httpx2_retries[trio]'
```

也可以在本目录执行 `python -m pip install .`。
`httpx2_retries/` 只存放库源码、类型标记、包元数据和文档；
测试位于父目录 `tests/`，环境、缓存和构建产物也放在父目录。

## Transport 用法

```python
import httpx2
from httpx2_retries import Retry, RetryTransport

policy = Retry(total=5, max_total_sleep=30, max_elapsed_time=60)

with httpx2.Client(
    transport=RetryTransport(retry=policy, trust_env=False),
    timeout=10,
) as client:
    response = client.get("https://example.com")
    print(response.extensions["retry_state"])
```

异步可以使用同一个 `RetryTransport` 类，也可以保留明确的 `AsyncRetryTransport` 名称：

```python
import httpx2
from httpx2_retries import Retry, AsyncRetryTransport

async def fetch(url: str, proxy: str | None = None):
    transport = AsyncRetryTransport(
        retry=Retry(total=3, max_total_sleep=20, max_elapsed_time=45),
        proxy=proxy,
        limits=httpx2.Limits(max_connections=10, max_keepalive_connections=5),
        trust_env=False,
        retries=0,
    )
    async with httpx2.AsyncClient(transport=transport, timeout=10) as client:
        return await client.get(url)
```

`verify`、`cert`、`proxy`、`limits`、`http1`、`http2`、`uds`、`local_address`、
`socket_options`、`trust_env` 等参数传给 transport。超时、认证、Cookie、重定向等由 client 处理。
HTTP/2 和 SOCKS 需要 HTTPX2 对应的可选依赖。

也可传入已有的 `httpx2.HTTPTransport`、`AsyncHTTPTransport`、`MockTransport` 或自定义 transport；
此时不能再传底层构造参数。传入仅支持同步/异步的 transport 后，包装器也只能服务对应模式。
包装器拥有底层 transport 的生命周期，client 退出时关闭它。不要跨不同生命周期的 client 共享 transport。
默认 transport 按实际使用模式延迟创建；共享的是不可变 `Retry` 配置，而不是连接池。

注意 HTTPX2 的路由规则：

- 在 client 上另传 `proxy=` 会创建独立代理 transport，可能绕开包装；把代理放在重试 transport 上。
- 使用 `mounts` 时，每个需要重试的路由都要包装。
- 显式指定自定义 transport 后，HTTPX2 不会自动通过 client 加载环境代理；需要明确设置代理。
- 默认拒绝叠加底层 `retries>0` 或另一个重试包装器。确有需要时传 `allow_nested_retries=True`；
  此时外层统计不含内层连接尝试，`extensions={"retry": False}` 也不能关闭底层连接重试。

## 完整正文与业务校验

```python
import httpx2
from httpx2_retries import Retry, aretry_request

class TemporaryBusinessError(Exception):
    pass

def validate(response: httpx2.Response) -> None:
    if response.json().get("code") == "TEMPORARY_FAILURE":
        raise TemporaryBusinessError("server is temporarily unavailable")

async def fetch_json(url: str):
    policy = Retry(
        total=3,
        max_total_sleep=10,
        max_elapsed_time=30,
        validate_response=validate,
        retry_on_exceptions=(*Retry.RETRYABLE_EXCEPTIONS, TemporaryBusinessError),
    )
    # helper 自己负责重试，使用普通 client。
    async with httpx2.AsyncClient(timeout=10) as client:
        response = await aretry_request(client, "GET", url, retry=policy)
        response.raise_for_status()
        return response.json()
```

同步入口为 `retry_request(client, method, url, *, retry=None, **kwargs)`；异步为 `aretry_request`。
支持 `params`、`headers`、`cookies`、`json`、`data`、`files`、`content`、`timeout`、`extensions`，
以及发送阶段的 `auth` 和 `follow_redirects`。`stream=True` 会明确报错。

校验在非重试状态码的响应上执行，包括最后一次响应；校验失败必须匹配 `retry_on_exceptions` 才能重试。
异步入口接受普通函数、协程函数和异步 callable 对象；同步入口拒绝异步校验器。
helper 会先读完正文；transport 校验器若要访问正文，需要显式 `read()` / `await aread()`，
这会改变流式语义，因此正文校验优先放在 helper 中。

helper 拒绝带重试 transport、重试 mounts 或已开启连接重试的内置 transport 的 client，避免次数相乘。
检测使用 HTTPX2 的内部 transport/pool 属性，并识别通过 `.transport` 暴露内部对象的包装器；
无法保证识别任意第三方隐藏的重试实现，使用方仍需避免重复配置。

## 策略、等待和截止时间

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `total` | 5 | 初次请求之外允许的重试次数 |
| `allowed_methods` | HEAD / GET / PUT / DELETE / OPTIONS / TRACE | POST/PATCH 需显式允许 |
| `status_forcelist` | 429 / 500 / 502 / 503 / 504 | 可重试响应状态码 |
| `retry_on_exceptions` | TimeoutException / NetworkError / ProxyError / RemoteProtocolError | HTTPX2 异常类型 |
| `backoff_factor` | 1 | 指数退避系数，0 禁用指数等待 |
| `max_backoff_wait` | 60 秒 | 单次等待上限 |
| `backoff_jitter` | 1 | 0 禁用抖动，1 为完整抖动 |
| `respect_retry_after_header` | True | 优先使用合法 Retry-After |
| `max_total_sleep` | None | 每次调用累计等待上限 |
| `total_timeout` | None | max_total_sleep 的别名；不表示网络总超时 |
| `max_elapsed_time` | None | 包括发送、等待与校验的整体时间预算 |
| `validate_response` | None | 响应校验回调 |

退避上限为 2、4、8、16、32、60 秒，默认在 0 到当次上限之间随机等待。
Retry-After 支持秒数与 HTTP 日期；0 和过去时间立即重试，无效值回退到退避。
若 Retry-After 超过单次上限，或下一次等待不能完整装入累计等待预算，则停止，返回当前响应或抛出当前异常；
不会截短服务器指定的等待后提前重试。累计等待统计包含已完成等待的调度超时。

`max_elapsed_time` 耗尽或下一次等待无法装入剩余时间时，抛出 `RetryDeadlineExceeded`，
它继承 `httpx2.TimeoutException`，不会被本库再次重试。
异步使用 AnyIO 取消作用域，覆盖可取消的发送、读正文、等待和异步校验。
同步会缩短 HTTPX2 各阶段的 I/O 超时，并在发送、校验与等待的边界检查实际耗时；
**无法强制中断任意同步阻塞代码、自定义 transport 或耗时校验器**，也不能保证多个 I/O 阶段的总时间绝不越界。
异步回调里的同步阻塞代码同样不能被强制抢占。

transport 的预算按每次交换计算，重定向和认证产生的新请求各自计数。
helper 的预算覆盖一次完整 client.send 及其重试；重定向会在下一轮从原始请求重新执行。

## 单请求覆盖和上传

```python
# 仅在业务确认可重复执行时重试 POST。
post_policy = policy.copy_with(allowed_methods={"POST"})
response = client.post(
    "https://example.com/query",
    json={"q": "example"},
    extensions={"retry": post_policy},
)

# 对本库禁用单请求重试；不禁用校验器。
response = client.get("https://example.com", extensions={"retry": False})
```

`extensions['retry']` 的优先级高于 transport/helper 默认策略。
`None` 集合表示采用默认值，空的 `allowed_methods`、`status_forcelist`、`retry_on_exceptions` 分别禁用对应条件。

仅自动重放 `ByteStream` 请求体，包括普通 JSON、urlencoded 表单、字符串和 bytes。
生成器、流式内容、multipart、文件上传默认只发送一次，包括连接阶段失败的情况。
对于明确可缓冲的小型上传，先 `build_request()`，然后 `request.read()` / `await request.aread()`，再通过重试 transport `send()`。
本库不会自动无限缓冲上传。读写失败不代表服务端尚未执行操作，POST/PATCH 重试应配合服务端幂等机制。

## 统计与扩展

每个响应提供：

- `extensions['retry_attempts']`：已执行重试次数，不含初次发送。
- `extensions['retry_state']`：不可变 `RetryState`，包含 `attempts_made`、`requests_made`、
  `elapsed_sleep`、`elapsed_time`、`stop_reason`、`last_status_code`、`last_exception`。
- `extensions['retry']`：带最终计数的不可变 Retry 快照，便于迁移旧读取代码。

相同状态写入原始请求的 `extensions['retry_state']`。异常保留原始类型和对象，并尽可能附加 `exc.retry_state`；
异常类型禁止设置属性时，从请求读取。没有等待的请求 `elapsed_sleep=0`；
取消发生在等待中时，该段未完成等待不计入 elapsed_sleep，但包含在 elapsed_time 内。

`stop_reason` 包括 `success`（无需继续重试，不等价于 HTTP 2xx）、`retries_exhausted`、
`method_not_allowed`、`body_not_replayable`、`retry_after_exceeds_limit`、`sleep_budget_exhausted`、
`deadline_exceeded`、`exception_not_retryable`、`cancelled`、`internal_error`。
状态只保存最近一次结果，不保留请求正文、URL、凭据或异常消息历史。
DEBUG 日志名为 `httpx2_retries.transport`，记录方法、次数、等待、状态码和异常类名。

公开策略方法：

```text
copy_with(**changes) -> Retry
increment(*, elapsed_sleep=0) -> Retry
is_retryable_method(method) -> bool
is_retryable_status_code(status_code) -> bool
is_retryable_exception(exception) -> bool
is_retry(method, status_code, has_retry_after=False) -> bool
is_exhausted() -> bool
parse_retry_after(value) -> float
backoff_strategy() -> float
delay(retry_number, headers=None) -> float | None
sleep(response_or_exception, *, delay=None) -> float | None
asleep(response_or_exception, *, delay=None) -> float | None
```

`Retry` 本身不变。手动循环使用 `increment()` 的返回值推进次数，用 `elapsed_sleep=` 传入新增等待；
`sleep/asleep` 返回实际计划等待秒数或 None，不修改共享策略。
`attempts_made` 和 `elapsed_sleep` 构造参数用于兼容手动状态快照，正常共享策略保持默认 0。
不要把带最终计数的响应快照当作全新请求的默认策略。

自定义退避不会影响 Retry-After 处理：

```python
from httpx2_retries import Retry

class LinearRetry(Retry):
    def backoff_strategy(self) -> float:
        return self.attempts_made * 0.5
```

已提供 `py.typed`，以及 `RetryChanges`、`TransportOptions`、`RequestOptions` 参数类型。

## 与 httpx_retries 0.6.0 的迁移差异

本库使用 HTTPX2 类型，不能混用 `httpx` 的 client、transport 或异常类型。
已补齐策略方法、统一同步/异步 transport、helper、校验与累计等待接口，但不是完全相同的行为替换。

| 项目 | 本库 | httpx_retries 0.6.0 |
| --- | --- | --- |
| total / factor / 单次上限 | 5 / 1 / 60 | 10 / 0 / 120 |
| 默认状态码 | 429、500、502、503、504 | 429、502、503、504 |
| 默认异常 | 含 HTTPX2 ProxyError | 默认不含 ProxyError |
| 空方法/状态码集合 | 禁用对应条件 | 回退默认集合 |
| 过长 Retry-After | 停止重试 | 截短等待 |
| Retry-After 为 0 或过去时间 | 立即重试 | 可能回退到指数等待 |
| 抖动顺序 | 先限制上限再抖动 | 先抖动再限制上限 |
| is_retry 的 has_retry_after | 不改变状态码资格 | 会影响判断结果 |
| 状态对象 | 不可变快照，sleep 不修改配置 | sleep 会更新 elapsed_sleep |
| 构造参数位置 | 保留本项目原顺序 | 顺序不同 |

迁移时使用关键字参数。希望沿用上游数值默认值时明确设置：

```python
retry = Retry(
    total=10, backoff_factor=0, max_backoff_wait=120,
    status_forcelist={429, 502, 503, 504},
)
```

从本项目早期版本升级时，默认状态码新增 429，其他原有数值默认值保留；
需要保持原行为可显式设置 `status_forcelist={500, 502, 503, 504}`。
原有 `AsyncRetryTransport`、`copy_with`、`delay` 和 `retry_attempts` 仍可使用。

## 测试

在父项目目录执行，测试全部使用本地 MockTransport，不访问业务地址：

```powershell
uv pip install --python .venv/Scripts/python.exe './httpx2_retries[test]'
.venv/Scripts/python.exe -B -m unittest discover -s tests -v
```

测试包含同步/异步、Trio、正文读取失败、业务校验、资源释放、取消、预算、截止时间、并发隔离、
重定向、上传重放保护、参数校验和重复重试配置。未安装 Trio 时仅跳过 Trio 测试。

参考：[HTTPX2 transports](https://pydantic.dev/docs/httpx2/advanced/transports/)、
[HTTPX Retries API](https://will-ockmore.github.io/httpx-retries/api/)。
