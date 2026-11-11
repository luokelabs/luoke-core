#!/usr/bin/env python3
"""bench3.json 的汇总（luoke-core#42 第三轮）。速度都按行配对：同一行里各边紧挨着做，比值取中位数。

    python3 analyze3.py bench3.json
"""
import json
import statistics as st
import sys

DECISION = ("action", "category", "priority", "due_date", "suspicious")
med = st.median


def parsed(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def main() -> None:
    R = json.load(open(sys.argv[1], encoding="utf-8"))
    rows = R["rows"]
    arms = [k for k in (rows.get("triage") or rows.get("prefill"))[0] if k not in ("i", "tag", "chars", "target")]
    print("arms", arms, "release waits", R.get("release_waits"), "done", R.get("done"))
    for k in ("footprint_loaded_gb", "footprint_after_batch_gb", "footprint_after_triage_gb"):
        print(k, R.get(k))

    if "prefill" in rows:
        print("\n## 1 预填充首字（中位），括号里是 ÷ oMLX（同一行配对的中位）")
        for t in sorted({r["target"] for r in rows["prefill"]}):
            rs = [r for r in rows["prefill"] if r["target"] == t]
            cells = []
            for n in arms:
                v = med([r[n]["ttft"] for r in rs if r[n]["ttft"]])
                ratio = med([r[n]["ttft"] / r["omlx"]["ttft"] for r in rs if r[n]["ttft"] and r["omlx"]["ttft"]])
                cells.append(f"{n} {v:6.2f}s ({ratio:.3f})")
            print(f"{t:6d}  " + "  ".join(cells))

    if "decode" in rows:
        print("\n## 2 生成速度 tok/s（中位）")
        print("  ".join(f"{n} {med([r[n]['decode'] for r in rows['decode'] if r[n].get('decode')]):.1f}" for n in arms))

    if "batch" in rows:
        print("\n## 3 批处理：效率 / 合计 tok/s / 最慢那条（中位）")
        for c in (2, 4, 8):
            rs = [r for r in rows["batch"] if r["n"] == c]
            print(f"N={c}  " + "  ".join(
                f"{n} {med([r[n]['efficiency'] for r in rs]):.2f}/{med([r[n]['agg_tok_s'] for r in rs]):.0f}/{med([r[n]['total_max'] for r in rs]):.1f}s"
                for n in arms))

    if "interrupt" in rows:
        print("\n## 4 打断后下一条首字（中位）")
        print("  ".join(f"{n} {med([r[n]['next_ttft'] for r in rows['interrupt'] if r[n]['next_ttft']]):.2f}s" for n in arms))

    if "prefix" in rows:
        print("\n## 5 同一段 3000 token 系统提示连发 4 条：各条首字（中位）/ 接上的 token")
        for n in arms:
            t = [med([r[n]["ttft"][k] for r in rows["prefix"]]) for k in range(4)]
            c = [rows["prefix"][0][n]["cached"][k] for k in range(4)]
            print(f"{n:7s} " + " ".join(f"{x:5.2f}" for x in t) + f"   cached {c}")
    if "multiturn" in rows:
        print("\n## 5b 四轮对话：各轮首字（中位）/ 输入 token / 接上的 token")
        for n in arms:
            t = [med([r[n][k]["ttft"] for r in rows["multiturn"] if r[n][k]["ttft"]]) for k in range(4)]
            pt = [rows["multiturn"][0][n][k]["pt"] for k in range(4)]
            c = [rows["multiturn"][0][n][k]["cached"] for k in range(4)]
            print(f"{n:7s} " + " ".join(f"{x:5.2f}" for x in t) + f"   pt {pt} cached {c}")

    if "triage" in rows:
        T = rows["triage"]
        print(f"\n## 6 真实分拣 {len(T)} 封，一条接一条")
        for n in arms:
            ok = [r for r in T if not r[n]["error"]]
            tt = [r[n]["ttft"] for r in ok if r[n]["ttft"]]
            tot = [r[n]["total"] for r in ok]
            rt = med([r[n]["ttft"] / r["omlx"]["ttft"] for r in ok if r[n]["ttft"] and r["omlx"]["ttft"]])
            rtot = med([r[n]["total"] / r["omlx"]["total"] for r in ok if r["omlx"]["total"]])
            cached = [r[n]["cached"] for r in ok if r[n]["cached"]]
            pts = [r[n]["pt"] for r in ok if r[n]["pt"]]
            bad = sum(parsed(r[n]["text"]) is None for r in T)
            print(f"{n:7s} 首字 {med(tt):.2f}s (÷oMLX {rt:.3f})  每封 {med(tot):.2f}s (÷oMLX {rtot:.3f})  合计 {sum(tot):.0f}s  "
                  f"输入 {med(pts):.0f} tok，接上 {len(cached)} 封、中位 {med(cached) if cached else 0} tok  JSON 坏 {bad}  出错 {len(T) - len(ok)}")
        for ref in ("omlx", "thin2"):
            if ref not in arms:
                continue
            for n in arms:
                if n == ref:
                    continue
                same_text = same = 0
                diffs = []
                for r in T:
                    a, b = parsed(r[n]["text"]), parsed(r[ref]["text"])
                    same_text += r[n]["text"] == r[ref]["text"]
                    if a is None or b is None:
                        continue
                    d = [k for k in DECISION if a.get(k) != b.get(k)]
                    if d:
                        diffs.append((r["tag"], {k: (b.get(k), a.get(k)) for k in d}))
                    else:
                        same += 1
                print(f"   {n} 对 {ref}：判断字段一样 {same}/{len(T)}，原文一样 {same_text}/{len(T)}" + (f"  不一样的：{diffs[:6]}" if diffs else ""))


if __name__ == "__main__":
    main()
