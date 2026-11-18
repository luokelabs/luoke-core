// 「设置」里本体管的那几块给主窗口的数据（luoke-agent docs/界面接口.md §16，设计规范 §18.7）：本体只给数据、收动作，页面由主窗口画。
// 这里只做换算（两个版本各在什么状态、模型清单、版本那一行），不依赖 LuokeWindow（好测）；AgentCore 再一对一填进 HostSettings。
import Foundation

struct TierEntry: Equatable {
    enum State: Equatable {
        case inUse
        /// 下载中（选了要换过去的那一档，或正在用的这一档还缺模型在补）：0…1，不知道就是 nil
        case switching(progress: Double?)
        /// 没在用：换过去还要下载多少 GB（0 就是都在磁盘上，点了马上换）
        case available(needGB: Double)
        case failed(reason: String)
    }
    /// 这一档是什么：这台 Mac 跑得动的最大的底座（`tierChoices` 排第一的）是 full，更小的是 lighter。名字和那句话由主窗口照它写
    enum Kind: Equatable { case full, lighter }

    let id: String
    let kind: Kind
    let model: String
    let sizeGB: Double
    let state: State
    /// 现在用的就是这一档（在补下模型、没下成时也是）：主窗口照样标「使用中」（luoke-agent 11-16）
    var current: Bool = false
    /// 没下成时再下一次有没有用：换档前就挡下的（磁盘不够）没用
    var canRetry: Bool = true
}

struct CatalogEntry: Equatable {
    let name: String
    /// 是哪一档的底座（不是底座就 nil）；用途由主窗口照它和 `accessories` 写（ADR 0028）
    var base: TierEntry.Kind? = nil
    /// 专用给哪些配件（配件标识，去重、按配方顺序）
    var accessories: [String] = []
    let params: String
    let quant: String
    let sizeGB: Double
}

enum SettingsSnapshot {
    /// 配方里一档用到的模型名：底座在前，再是专用的（去重）
    static func tierModels(_ recipe: [String: Any], _ tier: String) -> [String] {
        guard let a = (recipe["assignment"] as? [String: Any])?[tier] as? [String: Any] else { return [] }
        var out: [String] = []
        if let b = a["base"] as? String { out.append(b) }
        for m in (a["special"] as? [String: String] ?? [:]).sorted(by: { $0.key < $1.key }).map(\.value) where !out.contains(m) { out.append(m) }
        return out
    }

    /// 两个版本各一行（`choices` 照 Manifests.tierChoices 从大到小排，第一个是 full）。
    /// current：正在用的档；pending：选了、等底座下好就换的档；downloads：主控 /status 的下载（按模型名）；
    /// needed：主控说还缺的模型；installed、sizeGB：这个模型在不在磁盘上、多大；tierError：换档前就挡下的那一句（磁盘不够）
    static func tiers(choices: [Manifests.TierChoice], recipe: [String: Any], current: String, pending: String?,
                      downloads: [String: [String: Any]], needed: [String], tierError: (tier: String, text: String)?,
                      installed: (String) -> Bool, sizeGB: (String) -> Double) -> [TierEntry] {
        let base = { (t: String) in tierModels(recipe, t).first }
        return choices.enumerated().map { i, c in
            let models = tierModels(recipe, c.tier)
            let failed = models.compactMap { n -> String? in
                guard let d = downloads[n], d["state"] as? String == "failed" else { return nil }
                return d["error"] as? String ?? ""
            }
            let active = models.filter { ["queued", "downloading"].contains(downloads[$0]?["state"] as? String ?? "") }
            let isPending = pending.map { base($0) == base(c.tier) } ?? false
            let isCurrent = base(current) == base(c.tier) && !isPending
            let state: TierEntry.State
            if let e = tierError, e.tier == c.tier {
                state = .failed(reason: e.text)
            } else if (isPending || isCurrent), let why = failed.first {
                state = .failed(reason: why.isEmpty ? "下载失败" : "下载失败：" + why)
            } else if isPending || (isCurrent && !active.isEmpty) {
                state = .switching(progress: progress(active.map { downloads[$0] ?? [:] }))
            } else if isCurrent {
                let missing = models.filter { needed.contains($0) }
                state = missing.isEmpty ? .inUse : .failed(reason: "缺少 " + missing.map { ModelName.display(dirName(recipe, $0)) }.joined(separator: "、"))
            } else {
                state = .available(needGB: models.filter { !installed($0) }.reduce(0) { $0 + sizeGB($1) })
            }
            let blocked = tierError.map { $0.tier == c.tier } ?? false
            return TierEntry(id: c.tier, kind: i == 0 ? .full : .lighter, model: ModelName.display(c.base), sizeGB: c.sizeGB, state: state,
                             current: base(current) == base(c.tier), canRetry: !blocked)
        }
    }

    /// 几个下载合起来的进度；还都不知道总数（排队）就是 nil
    static func progress(_ ds: [[String: Any]]) -> Double? {
        let total = ds.reduce(Int64(0)) { $0 + (($1["total"] as? NSNumber)?.int64Value ?? 0) }
        guard total > 0 else { return nil }
        let done = ds.reduce(Int64(0)) { $0 + (($1["done"] as? NSNumber)?.int64Value ?? 0) }
        return min(1, Double(done) / Double(total))
    }

    /// 配方里写的大小（没有发布清单的开发构建用它）
    static func sizeGB(_ recipe: [String: Any], _ name: String) -> Double {
        (((recipe["models"] as? [String: Any])?[name] as? [String: Any])?["size_gb"] as? NSNumber)?.doubleValue ?? 0
    }

    static func dirName(_ recipe: [String: Any], _ name: String) -> String {
        ((recipe["models"] as? [String: Any])?[name] as? [String: Any])?["dir"] as? String ?? name
    }

    /// 模型清单：这台 Mac 能选的版本用到的模型（配方里没分到任何一档的不列），底座在前。
    /// 只给事实：是哪一档的底座、专用给哪些配件；用途那一列由主窗口照它们写（ADR 0028）
    static func catalog(choices: [Manifests.TierChoice], recipe: [String: Any]) -> [CatalogEntry] {
        let models = recipe["models"] as? [String: Any] ?? [:]
        let assign = recipe["assignment"] as? [String: Any] ?? [:]
        var order: [String] = []
        var base: [String: TierEntry.Kind] = [:]
        var accessories: [String: [String]] = [:]
        for (i, c) in choices.enumerated() {
            guard let b = (assign[c.tier] as? [String: Any])?["base"] as? String else { continue }
            if !order.contains(b) { order.append(b) }
            if base[b] == nil { base[b] = i == 0 ? .full : .lighter }
        }
        for c in choices {
            let special = (assign[c.tier] as? [String: Any])?["special"] as? [String: String] ?? [:]
            for (task, m) in special.sorted(by: { $0.key < $1.key }) {
                let acc = String(task.split(separator: ".").first ?? Substring(task))
                if !order.contains(m) { order.append(m) }
                if !(accessories[m] ?? []).contains(acc) { accessories[m, default: []].append(acc) }
            }
        }
        return order.map { n in
            let m = models[n] as? [String: Any] ?? [:]
            let dir = m["dir"] as? String ?? n
            return CatalogEntry(name: ModelName.display(dir), base: base[n], accessories: accessories[n] ?? [],
                                params: params(dir), quant: quant(dir), sizeGB: (m["size_gb"] as? NSNumber)?.doubleValue ?? 0)
        }
    }

    /// 从模型名里认参数量：9B、26B、E4B、0.8B（MoE 的 a4b 不算）
    static func params(_ name: String) -> String {
        let s = name.lowercased()
        guard let r = s.range(of: #"(?:^|[-_])(e?\d+(?:\.\d+)?)b(?=[-_]|$)"#, options: .regularExpression) else { return "—" }
        return s[r].trimmingCharacters(in: CharacterSet(charactersIn: "-_")).uppercased()
    }

    /// 从模型名里认量化：4bit → 4-bit；bf16、fp16 照写
    static func quant(_ name: String) -> String {
        let s = name.lowercased()
        if let r = s.range(of: #"\d+(?=bit\b)"#, options: .regularExpression) { return "\(s[r])-bit" }
        for f in ["bf16", "fp16"] where s.contains(f) { return f.uppercased() }
        return "—"
    }

    /// 「关于螺壳」那一行的版本：本机版说清没上传
    static func versionText(_ release: String) -> String {
        release.hasPrefix("local-") ? "\(release)（本机版，没上传）" : release
    }
}
