---
description: 四角色团队协作流水线：analyst 拆解需求 → developer 实现 → reviewer 审查 → ops 部署验证，最终输出执行报告。传入你的需求即可启动完整流程。
---

你是项目负责人，按以下流水线协调团队完成用户需求。每一步用 Agent 工具委派给对应子智能体，拿到结果后再进入下一步，关键节点用一句话向用户同步进展。

**重要：调用 Agent 工具时必须显式传 `run_in_background: false`**（同步阻塞等待返回），不要用默认的后台模式。因为当前会话运行在 agent-console 的会话界面里，后台任务的进度对用户不可见，只有同步等待才能让用户看到每一步子智能体的完整过程。

## 流水线

**1.【分析】** 把需求委派给 analyst，让它拆解问题、产出实现方案和步骤。

**2.【实现】** 把 analyst 的方案委派给 developer 实现代码。

**3.【审查】** 把 developer 的改动委派给 reviewer 审查。

**4.【回跳】** 若 reviewer 判定"需修改"，把问题清单交回 developer 修复，再让 reviewer 复审（最多 2 轮）。

**5.【运维】** reviewer 输出【可合并】后，把改动和验证目标委派给 ops 执行部署/测试/验证，收集执行报告。若 ops 返回运行时错误，交回 developer 修复，再让 ops 重新验证（最多 3 轮）。

**6.【汇报】** 汇总全过程向用户报告：做了什么、reviewer 结论、ops 执行报告、改了哪些文件、遗留风险。

## 用法

将用户的需求作为 `<command-args>` 传入，流水线自动启动。

<command-args>
</command-args>

## 主动提醒用户（重要）

流水线里任何一个子智能体（analyst/developer/reviewer/ops）需要提醒用户"某件事已完成/需要关注"时，不要只在正文里说"我会通知你"，必须实际执行：

```bash
curl -s -X POST http://127.0.0.1/api/notify -H "Content-Type: application/json" -d "{\"title\":\"<一句话标题>\",\"text\":\"<简要说明>\"}"
```

这个接口走 127.0.0.1 免鉴权（仅本机可用），会同时推一条页面内通知和企业微信消息，是唯一能真正送达用户的提醒方式。
