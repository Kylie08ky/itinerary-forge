# 本地模型对比：qwen2.5 7b vs 14b

用 `scripts/eval_models.py` 跑同一批 persona，对比生成质量。硬件：RTX 5070 Laptop（8GB VRAM）+ 32GB RAM。

## 结果（`--repair`，最多 2 轮修复）

| 模型 | persona | 耗时 | error | 说明 |
|---|---|---:|---|---|
| **7b** | 01 博物馆美食 | 27s | — | 干净 |
| **7b** | 02 带娃家庭 | 42s | after_departure | 早走日仍排满上午 |
| **7b** | 03 摄影强度 | 89s | after_departure, before_arrival | 夜到 + 晨走的硬约束 |
| **14b** | 01 | 146s | — | 干净 |
| **14b** | 02 | 318s | after_departure | 同上 |
| **14b** | 03 | 433s | **生成失败** | 反复输出 6 天（需求 5 天），重试也没救回来 |

## 结论

- **14b 慢 5–10 倍**。9GB 模型放不进 8GB 显存，大量 CPU 卸载，单次生成 2–7 分钟，不适合交互式流程。
- **质量基本持平**：两者都能把 persona-01 做干净；persona-02 都卡在 `after_departure`；persona-03 上 14b 反而整个失败。
- 剩下的硬错误（早走日排满、夜到日安排太早）是**提示词/约束问题，不是模型尺寸问题**——本地再大也解决不了。真要提升，方向是更强的云端模型（Claude / GPT-4 级）或把这类时间约束做成生成前的硬性 slot 规划。

**决定：默认保持 `qwen2.5:7b-instruct`。** 想试云端模型时，在 `app/llm.py` 加一个 `AnthropicClient` / `OpenAIClient`，`LLM_BACKEND` 切过去即可，其余代码不动。

## 复现

```bash
ollama pull qwen2.5:14b-instruct
python -m scripts.eval_models --repair qwen2.5:7b-instruct qwen2.5:14b-instruct
ollama rm qwen2.5:14b-instruct   # 用完想清掉（9GB）
```
