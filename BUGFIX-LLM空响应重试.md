# BUG 记录：LLM 偶发空响应导致撰稿任务中断

- **日期**：2026-09-13
- **模块**：A股AI投资复盘助手 / `agents.py`
- **严重级别**：中（会导致整条复盘流水线中断，不产生报告与邮件）
- **状态**：已修复

---

## 一、现象

完整跑 `python main.py`（研究员 → 核查员 → 分析师 → 撰稿人 → 交付）时，
**撰稿人（writer）在第一次工具调用之后的那一轮 LLM 请求返回了空消息**，任务直接失败：

- 撰稿人先调用 `generate_index_charts` 成功返回 4 行图片；
- 紧接着的下一轮 LLM 请求触发异常：

```
ERROR:crewai.flow.flow:Error executing listener call_llm_native_tools:
Invalid response from LLM call - None or empty.
```

- 终端表现为 `Crew Execution Failed`，**没有生成报告，也没有发送邮件**（进程退出码 1）。
- 重试迹象：同一条错误在日志中多次出现，说明 CrewAI 内部重试后仍是空响应才最终放弃。

---

## 二、原因分析

### 2.1 直接原因：DeepSeek 返回了空 assistant 消息

CrewAI 在 `crewai/utilities/agent_utils.py` 中对 LLM 返回做了非空校验：

```python
# agent_utils.py:325（force_final_answer 路径）与 :439（主路径）
if not answer:
    raise ValueError("Invalid response from LLM call - None or empty.")
```

而 native provider 对响应的归一化是：

```python
# crewai/llms/providers/openai/completion.py:85
content = message.content or ""
```

因此当 DeepSeek 返回的消息 **`content` 为空、且没有 `tool_calls`** 时，
`llm.call()` 返回 `""`，被判定为致命错误。

> 结论：**根因是 DeepSeek 服务端偶发返回空 message**，CrewAI 又把它当成不可恢复错误直接抛出。

### 2.2 为什么不是「上下文太长」

- 失败发生在撰稿人**第一轮工具调用之后**，此时上下文仅为：系统提示 + 任务描述 + 分析结论 + 一条工具返回，
  粗估约 **1.5–2 万字符（≈1.5–2 万 token）**，远低于常见 64K/128K 上下文窗口，未发生溢出。
- 若真的超出上下文窗口，DeepSeek 通常返回 **HTTP 400**，日志会显示 `OpenAI API call failed: ...`；
  本次日志没有 400，而是「空响应」。

### 2.3 为什么不是「要求输出太长」

- 若输出被 `max_tokens` 截断，模型会返回**部分内容 + `finish_reason=length`**，而不会返回空串。
- 同一套配置在另一次运行（run3）与后续重跑（run5/run6）中均成功，证明不是输出长度导致的确定性失败。

### 2.4 放大因素（非根因）

- 撰稿人上下文较大 + 要求输出较长（8 张表 + 5 章节 + 图片），**长输入 + 长生成更容易撞上 DeepSeek 的偶发空回复**。
- 撰稿人 `temperature=0.8` 偏高。
- **CrewAI 对「空响应」没有专门的退避重试**，直接判定任务失败。

### 2.5 最有力证据

> 同样的代码、同样的任务，**紧接着重跑即恢复正常**。
> 故定性为：**DeepSeek 服务端偶发空响应 + 缺少空响应重试机制**，而非确定性逻辑缺陷。

---

## 三、解决办法

在**不改动 CrewAI 源码**的前提下，于实例层给 LLM 的 `call` 方法包一层「空响应自动重试」。

### 3.1 实现（`agents.py`）

```python
EMPTY_RETRY_TIMES = int(os.environ.get("LLM_EMPTY_RETRY", "3") or "3")
EMPTY_RETRY_DELAY = float(os.environ.get("LLM_RETRY_DELAY", "2") or "2")


def with_empty_retry(model, retries=EMPTY_RETRY_TIMES, delay=EMPTY_RETRY_DELAY):
    """给 LLM 实例的 call 方法加「空响应重试」，返回空则退避重试 retries 次。"""
    original_call = model.call

    def call_with_retry(*args, **kwargs):
        last = None
        for attempt in range(1, retries + 1):
            last = original_call(*args, **kwargs)
            if last:                      # 非空即成功（文本或 tool_calls 列表）
                return last
            if attempt < retries:
                time.sleep(delay * attempt)  # 线性退避：2s, 4s, ...
        return last

    # 绕过 pydantic 字段校验，把函数写进实例 __dict__，遮蔽原绑定方法
    object.__setattr__(model, "call", call_with_retry)
    return model


llm = with_empty_retry(llm)
llm_precise = with_empty_retry(llm_precise)
```

要点：

1. **实例级包裹**：`LLM(...)` 实际返回 native provider 实例（pydantic 模型），
   用 `object.__setattr__` 把包装函数写入实例 `__dict__`，从而遮蔽类上的绑定方法，无需继承或改源码。
2. **只重试「空响应」**：返回 `""`/`None` 才重试；正常文本或 `tool_calls` 列表立即返回。
3. **重试安全**：LLM 调用本身无副作用（工具执行由 CrewAI executor 负责），重复请求不会造成重复下单、重复发信等。
4. **有上限**：最多重试 `retries` 次，仍为空则返回最后一次结果，交由上层报错，避免死循环。

### 3.2 新增配置项（`.env` / `.env.example`）

```ini
# 空响应自动重试：次数与每次退避基数（秒）
LLM_EMPTY_RETRY=3
LLM_RETRY_DELAY=2
```

### 3.3 验证

- 单元验证：
  - 模拟「前 2 次空、第 3 次成功」→ 返回成功结果，调用 3 次；
  - 模拟「一直为空」→ 调用 3 次后返回空，不无限重试；
  - 真实 LLM 实例被成功遮蔽（`llm.call` 为包装函数）且正常返回内容。
- 全流程验证：包裹后重跑 `python main.py` 正常完成并发送邮件。

---

## 四、影响与回滚

- **影响范围**：仅 `agents.py` 中的 `llm` / `llm_precise` 两个实例，不影响工具、任务与邮件逻辑。
- **回滚方式**：删除 `with_empty_retry(...)` 两行调用即可恢复原行为。
- **成本**：仅在出现空响应时增加最多 `retries` 次额外请求与少量等待；正常情况无额外开销。

---

## 五、相关代码位置

| 位置 | 说明 |
|---|---|
| `agents.py:85` | `with_empty_retry()` 实现 |
| `agents.py:105-106` | 对 `llm`、`llm_precise` 应用重试包裹 |
| `crewai/utilities/agent_utils.py:325` | force_final_answer 路径的空响应校验 |
| `crewai/utilities/agent_utils.py:439` | 主路径的空响应校验 |
| `crewai/llms/providers/openai/completion.py:85` | `content = message.content or ""` |

---

## 六、后续可选优化

1. 撰稿人改用低温模型（如 `llm_precise`）或把 `temperature` 降到 0.4–0.5，降低空响应/跑偏概率。
2. 精简撰稿人上下文：只传「核查后的修正情报 + 分析结论」，避免塞入三份全文。
3. 将空响应重试下沉为通用能力：把「空响应 / 瞬时 API 错误」都纳入重试并记录告警日志。
4. 交付环节增加降级：即使 LLM 异常，也先把已生成的报告落盘，避免「无产物」。
