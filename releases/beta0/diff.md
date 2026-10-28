# 差异报告 beta0（对比 baseline-all-2026-10-14）

| 任务 | 模型 | 档 | 本次 | 上次 | 硬性条目 | 结论 |
|---|---|---|---|---|---|---|
| luoke-ime.predict | qwen3.5-0.8b-base-4bit | 128 | 42.5/100.0 | 42.5/100.0 | 过 | 通过 |
| luoke-ime.rerank | qwen3.5-0.8b-base-4bit | 128 | 89.8/100.0 | 89.8/100.0 | 过 | 通过 |
| luoke-mail.cleanup | gemma-4-26b-8bit | 128 | 39/40 | 39/40 | 过 | 通过 |
| luoke-mail.cleanup | qwen3.5-9b-4bit | 32 | 38/40 | 38/40 | 过 | 通过 |
| luoke-mail.cleanup | qwen3.5-9b-4bit | 16 | 38/40 | 38/40 | 过 | 通过 |
| luoke-mail.learn-orgs | gemma-4-26b-8bit | 128 | 7/8 | 7/8 | 过 | 通过 |
| luoke-mail.learn-orgs | qwen3.5-9b-4bit | 16 | 4/8 | 5/8 | 过 | 通过 |
| luoke-mail.learn-orgs | qwen3.5-9b-4bit | 32 | 5/8 | 5/8 | 过 | 通过 |
| luoke-mail.learn-sent | gemma-4-26b-8bit | 128 | 12/14 | 12/14 | 过 | 通过 |
| luoke-mail.learn-sent | qwen3.5-9b-4bit | 32 | 13/14 | 13/14 | 过 | 通过 |
| luoke-mail.learn-sent | qwen3.5-9b-4bit | 16 | 12/14 | 13/14 | 过 | 通过 |
| luoke-mail.senders | gemma-4-26b-8bit | 128 | 25/25 | 25/25 | 过 | 通过 |
| luoke-mail.senders | qwen3.5-9b-4bit | 32 | 25/25 | 25/25 | 过 | 通过 |
| luoke-mail.senders | qwen3.5-9b-4bit | 16 | 25/25 | 25/25 | 过 | 通过 |
| luoke-mail.triage | gemma-4-26b-8bit | 128 | 44/45 | 44/45 | 过 | 通过 |
| luoke-mail.triage | qwen3.5-9b-4bit | 32 | 39/45 | 39/45 | 过 | 通过 |
| luoke-mail.triage | qwen3.5-9b-4bit | 16 | 39/45 | 39/45 | 过 | 通过 |
| luoke-mem.typing | gemma-4-26b-8bit | 128 | 82.3/100.0 | 82.3/100.0 | 过 | 通过 |
| luoke-mem.typing | qwen3.5-9b-4bit | 16 | 96.9/100.0 | 96.9/100.0 | 没过 | 通过：这一档硬性条目没过，配方里这一档不支持（上次也不支持） |
| luoke-mem.typing | qwen3.5-9b-4bit | 32 | 97.9/100.0 | 97.9/100.0 | 过 | 通过 |

**结论：放行。**
