---
name: asr-experiment-analysis
description: 对任意数量的 ASR 实验目录做 WER 收敛、稳定性、分组能力、Context 权衡和异常测试集分析，并推荐模型/checkpoint。适用于目录中包含 ckpt/iter_*/.../wer_summary.txt 与 wer_group_summary.txt 的实验；当指标更新时也用此指令刷新结论。
---

你负责比较用户提供的**任意数量** ASR 实验目录。不要假设固定为 4 个实验；按用户实际给出的目录数量处理。

## 输入

用户应在本指令后提供：

- 两个或更多实验目录，可选自定义简称；
- 是否包含 Context / Context-dialogue；
- 若未说明，默认同时输出：
  1. 排除 Context 的主能力对比；
  2. 包含 Context 的综合对比；
- 可选业务权重、共同 checkpoint 或指定 checkpoint。

若实验目录未给全，先追问目录，不要猜路径。若已给出足够目录，直接开始分析，不重复确认。

## 工作流

1. 读取上级 `AGENTS.md`，按项目/临时分析归档约定选择输出目录并登记 `worklog/INDEX.md`。
2. 扫描每个实验的 `ckpt/iter_*`：
   - 统计 `wer_summary.txt` 和 `wer_group_summary.txt`；
   - 标记评测完整与不完整 checkpoint；
   - 只用完整 checkpoint 做公平比较。
3. 使用本 Skill 的脚本进行确定性解析：

```bash
python3 /apdcephfs_gy2/share_302533218/zhihangxu/agent-console/.claude/skills/asr-experiment-analysis/scripts/compare_wer_experiments.py \
  --experiment 名称1=/path/to/experiment1 \
  --experiment 名称2=/path/to/experiment2 \
  [继续追加任意数量 --experiment] \
  --dataset-map all_anycontext_test_list_results_with_context=context \
  --dataset-map all_anycontext_test_list_results_with_context_dialogue=context-dialogue \
  --dataset-map asr_test_ct_results_vllm0.14=asr \
  --dataset-map fangyan_ood_abs_paths_results_fangyan_ood=dialect-ood \
  --dataset-map fangyan_yewu_results_fangyan=dialect-business \
  --dataset-map leimu_testset_results_leimu=leimu \
  --selection best \
  --out <输出目录>
```

排除 Context 时增加：

```bash
--exclude-group-pattern '^context'
```

脚本自动生成 `report.md`、CSV、JSON、收敛图和后期稳定性图。

4. 至少区分两种比较口径：
   - **同成本/同 checkpoint**：使用所有实验共同存在的完整 checkpoint，比较训练效率；
   - **各自最佳**：比较每个实验自己的最佳完整 checkpoint，给部署推荐。
   不要把两种口径混在同一个排名里。
5. 分析：
   - 收敛速度、当前最佳、最新点是否回退；
   - 最近 4–6 个完整点的均值、标准差、极差；
   - group 强弱；
   - 具体子测试集的异常好、异常差和系统间大差异；
   - Context 与非 Context 是否改变模型选择。
6. 对高差异或高波动任务下钻目录，读取：
   - `summary.json`
   - `wer_pengcheng.log`
   - `pred.txt` / `lab.txt`
   - 存在时读取 `pred_30s.txt`、`lab_bg30s.txt`、`pred_bg30s.txt`
7. 判断异常原因：
   - substitution/deletion 普遍升高：识别能力退化；
   - insertion 和最大输出长度突然暴涨：重复生成/终止不稳定；
   - 同一任务在相邻 checkpoint 剧烈来回：checkpoint 或解码稳定性问题；
   - 不要把少量重复生成直接解释成“Context 理解能力差”。
8. 若需要详细指标解释，读取：
   `/apdcephfs_gy2/share_302533218/zhihangxu/agent-console/.claude/skills/asr-experiment-analysis/references/metric-interpretation.md`

## 输出要求

先给结论，控制正文长度：

1. 一句话推荐；
2. 决策表：模型、推荐 checkpoint、核心指标、适用场景；
3. 3–6 条关键原因；
4. 重要异常任务和风险；
5. 产物路径。

默认不要输出冗长的逐 checkpoint 流水账。只有用户追问时再展开具体曲线和完整子测试集表。

必须说明：

- 是否包含 Context；
- 使用同 checkpoint 还是各自最佳；
- macro 是 group WER 的非加权平均，不等于 token-pooled WER；
- 最新 checkpoint 不一定是最佳 checkpoint。

## 用户需求

<command-args>
</command-args>
