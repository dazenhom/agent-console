#!/usr/bin/env python3
"""Compare WER summaries across experiment checkpoint directories.

The parser expects:
  EXP/ckpt/iter_*/RESULT_SET/wer_summary.txt
  EXP/ckpt/iter_*/RESULT_SET/wer_group_summary.txt

It emits machine-readable CSV/JSON plus concise Markdown and optional PNG plots.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ITER_RE = re.compile(r"iter_(\d+)")
GROUP_RE = re.compile(r"组:\s*(.*?)\s*\|\s*组 WER\(all\):\s*([0-9.]+)%")


@dataclass(frozen=True)
class Experiment:
    name: str
    root: Path


def parse_named_path(text: str) -> tuple[str, Path]:
    if "=" in text:
        name, raw = text.split("=", 1)
        return name.strip(), Path(raw).expanduser().resolve()
    path = Path(text).expanduser().resolve()
    return path.name, path


def parse_mapping(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def checkpoint_step(path: Path) -> int | None:
    match = ITER_RE.search(str(path))
    return int(match.group(1)) if match else None


def parse_group_data(exps: list[Experiment], dataset_map: dict[str, str]):
    data: dict[str, dict[int, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    sources: dict[str, dict[int, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    for exp in exps:
        for path in sorted((exp.root / "ckpt").glob("iter_*/*/wer_group_summary.txt")):
            step = checkpoint_step(path)
            if step is None:
                continue
            dataset_dir = path.parent.name
            dataset_label = dataset_map.get(dataset_dir, dataset_dir)
            text = path.read_text(encoding="utf-8", errors="replace")
            matches = GROUP_RE.findall(text)
            if len(matches) == 1:
                group, value = matches[0]
                key = dataset_label
                data[exp.name][step][key] = float(value)
                sources[exp.name][step][key] = str(path)
            else:
                for group, value in matches:
                    key = group.strip()
                    data[exp.name][step][key] = float(value)
                    sources[exp.name][step][key] = str(path)
    return data, sources


def parse_task_data(exps: list[Experiment], dataset_map: dict[str, str]):
    data: dict[str, dict[int, dict[str, dict[str, float | str]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    counts: dict[str, dict[int, dict[str, dict[str, int]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for exp in exps:
        for path in sorted((exp.root / "ckpt").glob("iter_*/*/wer_summary.txt")):
            step = checkpoint_step(path)
            if step is None:
                continue
            dataset_dir = path.parent.name
            dataset_label = dataset_map.get(dataset_dir, dataset_dir)
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            count_info: dict[str, int] = {}
            for line in lines[:4]:
                if ":" in line:
                    key, raw = line.split(":", 1)
                    if key.strip() in {"Long  Pred", "Short Pred"}:
                        try:
                            count_info[key.strip()] = int(raw.strip())
                        except ValueError:
                            pass
            counts[exp.name][step][dataset_label] = count_info
            for line in lines:
                parts = line.split()
                if len(parts) < 2:
                    continue
                try:
                    wer = float(parts[1])
                    ser = float(parts[-1]) if len(parts) >= 10 else math.nan
                except ValueError:
                    continue
                task = parts[0]
                key = f"{dataset_label}/{task}"
                data[exp.name][step][key] = {
                    "wer": wer,
                    "ser": ser,
                    "dataset": dataset_label,
                    "task": task,
                    "source": str(path),
                }
    return data, counts


def mean(values):
    return statistics.fmean(values)


def pstdev(values):
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def select_groups(
    data: dict[str, dict[int, dict[str, float]]],
    exps: list[Experiment],
    include_groups: list[str],
    exclude_patterns: list[str],
) -> list[str]:
    all_groups = set()
    for exp in exps:
        for values in data[exp.name].values():
            all_groups.update(values)
    if include_groups:
        chosen = [g for g in include_groups if g in all_groups]
    else:
        regexes = [re.compile(x, re.I) for x in exclude_patterns]
        chosen = [g for g in sorted(all_groups) if not any(r.search(g) for r in regexes)]
    if not chosen:
        raise SystemExit("No groups selected. Check --include-group/--exclude-group-pattern.")
    return chosen


def complete_steps(group_data, exp: str, groups: list[str]) -> list[int]:
    return [
        step
        for step, values in sorted(group_data[exp].items())
        if all(group in values for group in groups)
    ]


def macro(group_data, exp: str, step: int, groups: list[str]) -> float:
    return mean([group_data[exp][step][g] for g in groups])


def choose_step(group_data, exp: str, groups: list[str], policy: str) -> int:
    steps = complete_steps(group_data, exp, groups)
    if not steps:
        raise SystemExit(f"No complete checkpoints for {exp} and groups={groups}")
    if policy == "latest":
        return steps[-1]
    return min(steps, key=lambda s: (macro(group_data, exp, s, groups), s))


def sustained_band_step(curve: dict[int, float], final_step: int, tolerance: float):
    threshold = curve[final_step] * (1 + tolerance)
    steps = sorted(s for s in curve if s <= final_step)
    for i, step in enumerate(steps):
        if all(curve[s] <= threshold for s in steps[i:]):
            return step
    return None


def rank_task_anomalies(
    task_data,
    chosen_steps: dict[str, int],
    exp_names: list[str],
    exclude_dataset_patterns: list[str],
):
    common = set.intersection(
        *[set(task_data[e][chosen_steps[e]]) for e in exp_names]
    )
    dataset_regexes = [re.compile(x, re.I) for x in exclude_dataset_patterns]
    common = {
        task
        for task in common
        if not any(regex.search(task.split("/", 1)[0]) for regex in dataset_regexes)
    }
    rows = []
    wins = defaultdict(float)
    for task in sorted(common):
        values = {e: float(task_data[e][chosen_steps[e]][task]["wer"]) for e in exp_names}
        best = min(values.values())
        winners = [e for e, value in values.items() if math.isclose(value, best)]
        for winner in winners:
            wins[winner] += 1 / len(winners)
        ordered = sorted(values.items(), key=lambda x: x[1])
        rows.append(
            {
                "task": task,
                "dataset": task.split("/", 1)[0],
                **values,
                "mean": mean(values.values()),
                "median": statistics.median(values.values()),
                "spread": max(values.values()) - min(values.values()),
                "winner": ",".join(winners),
                "best": ordered[0][1],
                "second_best": ordered[1][1] if len(ordered) > 1 else ordered[0][1],
            }
        )
    return rows, dict(wins)


def task_trends(
    task_data,
    exp_names: list[str],
    tail_points: int,
    exclude_dataset_patterns: list[str],
):
    rows = []
    all_tasks = set()
    for exp in exp_names:
        for values in task_data[exp].values():
            all_tasks.update(values)
    dataset_regexes = [re.compile(x, re.I) for x in exclude_dataset_patterns]
    all_tasks = {
        task
        for task in all_tasks
        if not any(regex.search(task.split("/", 1)[0]) for regex in dataset_regexes)
    }
    for task in sorted(all_tasks):
        row = {"task": task, "dataset": task.split("/", 1)[0]}
        seen = False
        for exp in exp_names:
            curve = {
                s: float(values[task]["wer"])
                for s, values in task_data[exp].items()
                if task in values
            }
            if not curve:
                continue
            seen = True
            steps = sorted(curve)
            tail_steps = steps[-tail_points:]
            best_step = min(steps, key=lambda s: (curve[s], s))
            row.update(
                {
                    f"{exp}_first_step": steps[0],
                    f"{exp}_last_step": steps[-1],
                    f"{exp}_first_wer": curve[steps[0]],
                    f"{exp}_last_wer": curve[steps[-1]],
                    f"{exp}_best_step": best_step,
                    f"{exp}_best_wer": curve[best_step],
                    f"{exp}_regress_from_best": curve[steps[-1]] - curve[best_step],
                    f"{exp}_tail_std": pstdev([curve[s] for s in tail_steps]),
                    f"{exp}_tail_range": max(curve[s] for s in tail_steps)
                    - min(curve[s] for s in tail_steps),
                    f"{exp}_max_jump": max(
                        [abs(curve[steps[i]] - curve[steps[i - 1]]) for i in range(1, len(steps))]
                        or [0.0]
                    ),
                }
            )
        if seen:
            rows.append(row)
    return rows


def inspect_task_logs(task_rows, task_data, chosen_steps, exp_names, top_n):
    """Read per-task summary/log files for high-spread tasks and detect insertion bursts."""
    log_re = re.compile(
        r"%WER\s+[\d.]+\s+\[\s*(\d+)\s*/\s*(\d+),\s*(\d+)\s+ins,\s*(\d+)\s+del,\s*(\d+)\s+sub"
    )
    findings = []
    for task_row in sorted(task_rows, key=lambda r: r["spread"], reverse=True)[:top_n]:
        task = task_row["task"]
        for exp in exp_names:
            source = Path(str(task_data[exp][chosen_steps[exp]][task]["source"]))
            task_dir = source.parent / task.split("/", 1)[1]
            log_path = task_dir / "wer_pengcheng.log"
            pred_path = task_dir / "pred.txt"
            lab_path = task_dir / "lab.txt"
            item = {"task": task, "experiment": exp, "step": chosen_steps[exp]}
            if log_path.exists():
                match = log_re.search(log_path.read_text(encoding="utf-8", errors="replace"))
                if match:
                    edits, tokens, ins, delete, sub = map(int, match.groups())
                    item.update(
                        {
                            "edits": edits,
                            "tokens": tokens,
                            "insertions": ins,
                            "deletions": delete,
                            "substitutions": sub,
                            "insertion_rate": 100 * ins / tokens if tokens else math.nan,
                        }
                    )
            if pred_path.exists():
                pred_lines = pred_path.read_text(encoding="utf-8", errors="replace").splitlines()
                lengths = [len(x.split("\t", 1)[-1]) for x in pred_lines]
                item.update(
                    {
                        "pred_lines": len(pred_lines),
                        "pred_max_chars": max(lengths) if lengths else 0,
                        "pred_p99_chars": sorted(lengths)[int(0.99 * (len(lengths) - 1))]
                        if lengths
                        else 0,
                    }
                )
            if lab_path.exists():
                lab_lines = lab_path.read_text(encoding="utf-8", errors="replace").splitlines()
                item["lab_lines"] = len(lab_lines)
            findings.append(item)
    return findings


def write_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def plot_curves(out_dir, group_data, exps, groups, chosen_steps, tail_points):
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return []
    colors = ["#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2", "#B279A2"]
    generated = []
    fig, ax = plt.subplots(figsize=(11, 6.5))
    for idx, exp in enumerate(exps):
        steps = complete_steps(group_data, exp.name, groups)
        ys = [macro(group_data, exp.name, s, groups) for s in steps]
        ax.plot(np.array(steps) / 1000, ys, marker="o", ms=4, lw=2, label=exp.name, color=colors[idx % len(colors)])
        chosen = chosen_steps[exp.name]
        ax.scatter([chosen / 1000], [macro(group_data, exp.name, chosen, groups)], s=100, facecolors="none", edgecolors=colors[idx % len(colors)], lw=2)
    ax.set_title(f"WER convergence ({len(groups)} selected groups)")
    ax.set_xlabel("Training step (k)")
    ax.set_ylabel("Macro WER (%) ↓ better")
    ax.grid(alpha=0.25)
    ax.legend(frameon=False)
    fig.tight_layout()
    path = out_dir / "convergence.png"
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    generated.append(str(path))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.8), gridspec_kw={"width_ratios": [1.5, 1]})
    for idx, exp in enumerate(exps):
        steps = complete_steps(group_data, exp.name, groups)[-tail_points:]
        ys = [macro(group_data, exp.name, s, groups) for s in steps]
        ax1.plot(np.array(steps) / 1000, ys, marker="o", lw=2, label=exp.name, color=colors[idx % len(colors)])
    ax1.set_title("Late-stage trajectory")
    ax1.set_xlabel("Training step (k)")
    ax1.set_ylabel("Macro WER (%) ↓ better")
    ax1.grid(alpha=0.25)
    ax1.legend(frameon=False)
    names, stds, ranges = [], [], []
    for exp in exps:
        steps = complete_steps(group_data, exp.name, groups)[-tail_points:]
        ys = [macro(group_data, exp.name, s, groups) for s in steps]
        names.append(exp.name)
        stds.append(pstdev(ys))
        ranges.append(max(ys) - min(ys))
    x = np.arange(len(names))
    width = 0.36
    ax2.bar(x - width / 2, stds, width, label="std")
    ax2.bar(x + width / 2, ranges, width, label="range")
    ax2.set_xticks(x, names, rotation=18)
    ax2.set_title(f"Variation over last {tail_points} points")
    ax2.set_ylabel("WER points ↓ smaller")
    ax2.grid(axis="y", alpha=0.25)
    ax2.legend(frameon=False)
    fig.tight_layout()
    path = out_dir / "late_stability.png"
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    generated.append(str(path))
    return generated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", action="append", required=True, help="NAME=/path/to/experiment or /path/to/experiment")
    parser.add_argument("--dataset-map", action="append", default=[], help="RESULT_DIR=LABEL; repeat as needed")
    parser.add_argument("--include-group", action="append", default=[], help="Exact group/label to include in macro")
    parser.add_argument("--exclude-group-pattern", action="append", default=[], help="Regex of groups to exclude; repeat as needed")
    parser.add_argument(
        "--exclude-task-dataset-pattern",
        action="append",
        default=[],
        help="Regex of task dataset labels to exclude. Defaults to --exclude-group-pattern.",
    )
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--selection", choices=["best", "latest"], default="best")
    parser.add_argument("--tail-points", type=int, default=4)
    parser.add_argument("--band-tolerance", type=float, default=0.05)
    parser.add_argument("--inspect-top", type=int, default=12, help="Inspect logs for top cross-system spread tasks")
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()

    exps = [Experiment(*parse_named_path(item)) for item in args.experiment]
    for exp in exps:
        if not (exp.root / "ckpt").is_dir():
            raise SystemExit(f"Missing ckpt directory: {exp.root / 'ckpt'}")
    dataset_map = parse_mapping(args.dataset_map)
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    group_data, group_sources = parse_group_data(exps, dataset_map)
    task_data, count_data = parse_task_data(exps, dataset_map)
    groups = select_groups(group_data, exps, args.include_group, args.exclude_group_pattern)
    exp_names = [e.name for e in exps]
    chosen_steps = {e.name: choose_step(group_data, e.name, groups, args.selection) for e in exps}

    group_rows = []
    curve_rows = []
    summary = {"groups": groups, "selection": args.selection, "experiments": {}}
    for exp in exps:
        steps = complete_steps(group_data, exp.name, groups)
        curve = {s: macro(group_data, exp.name, s, groups) for s in steps}
        best_step = min(steps, key=lambda s: (curve[s], s))
        latest_step = steps[-1]
        chosen = chosen_steps[exp.name]
        tail_steps = steps[-args.tail_points :]
        tail_values = [curve[s] for s in tail_steps]
        summary["experiments"][exp.name] = {
            "root": str(exp.root),
            "complete_steps": steps,
            "chosen_step": chosen,
            "chosen_macro": curve[chosen],
            "best_step": best_step,
            "best_macro": curve[best_step],
            "latest_step": latest_step,
            "latest_macro": curve[latest_step],
            "tail_steps": tail_steps,
            "tail_mean": mean(tail_values),
            "tail_std": pstdev(tail_values),
            "tail_range": max(tail_values) - min(tail_values),
            "sustained_band_step": sustained_band_step(curve, latest_step, args.band_tolerance),
        }
        for s in steps:
            curve_rows.append({"experiment": exp.name, "step": s, "macro": curve[s]})
        for g in groups:
            group_rows.append(
                {
                    "experiment": exp.name,
                    "step": chosen,
                    "group": g,
                    "wer": group_data[exp.name][chosen][g],
                    "source": group_sources[exp.name][chosen].get(g, ""),
                }
            )

    task_excludes = args.exclude_task_dataset_pattern or args.exclude_group_pattern
    task_rows, task_wins = rank_task_anomalies(
        task_data, chosen_steps, exp_names, task_excludes
    )
    trend_rows = task_trends(
        task_data, exp_names, args.tail_points, task_excludes
    )
    log_findings = inspect_task_logs(task_rows, task_data, chosen_steps, exp_names, args.inspect_top)
    summary["task_count_common_at_chosen"] = len(task_rows)
    summary["fractional_task_wins"] = task_wins
    summary["log_findings"] = log_findings

    write_csv(out_dir / "macro_curves.csv", curve_rows)
    write_csv(out_dir / "chosen_group_comparison.csv", group_rows)
    write_csv(out_dir / "chosen_task_comparison.csv", task_rows)
    write_csv(out_dir / "task_trends.csv", trend_rows)
    write_csv(out_dir / "log_anomaly_findings.csv", log_findings)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    generated_plots = [] if args.no_plots else plot_curves(out_dir, group_data, exps, groups, chosen_steps, args.tail_points)

    ranked = sorted(exp_names, key=lambda e: summary["experiments"][e]["chosen_macro"])
    lines = [
        "# ASR experiment comparison",
        "",
        f"Selected groups ({len(groups)}): " + ", ".join(groups),
        "",
        "## Decision table",
        "",
        "| Rank | Experiment | Chosen step | Macro | Best step | Latest step | Tail std | Tail range |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, exp in enumerate(ranked, 1):
        x = summary["experiments"][exp]
        lines.append(
            f"| {rank} | {exp} | {x['chosen_step']} | {x['chosen_macro']:.3f} | "
            f"{x['best_step']} | {x['latest_step']} | {x['tail_std']:.3f} | {x['tail_range']:.3f} |"
        )
    lines.extend(["", "## Group comparison at chosen checkpoints", ""])
    lines.append("| Group | " + " | ".join(exp_names) + " |")
    lines.append("|---|" + "|".join(["---:"] * len(exp_names)) + "|")
    for group in groups:
        cells = [group]
        vals = [group_data[e][chosen_steps[e]][group] for e in exp_names]
        best = min(vals)
        for value in vals:
            text = f"{value:.2f}"
            if math.isclose(value, best):
                text = f"**{text}**"
            cells.append(text)
        lines.append("| " + " | ".join(cells) + " |")
    lines.extend(["", "## Highest cross-system task spread", ""])
    lines.append("| Task | Spread | Winner | " + " | ".join(exp_names) + " |")
    lines.append("|---|---:|---|" + "|".join(["---:"] * len(exp_names)) + "|")
    for row in sorted(task_rows, key=lambda r: r["spread"], reverse=True)[:12]:
        lines.append(
            "| " + " | ".join(
                [row["task"], f"{row['spread']:.2f}", row["winner"]]
                + [f"{row[e]:.2f}" for e in exp_names]
            ) + " |"
        )
    lines.extend(["", "## Recommendation", ""])
    winner = ranked[0]
    winner_info = summary["experiments"][winner]
    lines.append(
        f"Use **{winner}@{winner_info['chosen_step']}** for the selected macro unless business weighting or anomaly constraints change the decision."
    )
    lines.append(
        "Before finalizing, inspect high-spread tasks and insertion-heavy outputs in `log_anomaly_findings.csv`; a low macro can be dominated by a lucky checkpoint or a few repetition failures."
    )
    if generated_plots:
        lines.extend(["", "Generated plots: " + ", ".join(Path(x).name for x in generated_plots)])
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
