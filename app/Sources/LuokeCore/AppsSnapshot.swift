// 「应用管理」页给主窗口的数据（luoke-agent docs/界面接口.md §16，设计规范 §18.6）：本体只给数据、收动作，页面由主窗口画。
// 这里只把 AppsModel 的行和正在做的事换成给窗口的样子，不依赖 LuokeWindow（好测）；AgentCore 再一对一填进 HostApps。
import Foundation

struct AppEntry: Equatable {
    enum State: Equatable {
        case installed, disabled, installable
        case update(to: String)
        case unavailable(reason: String)
        case working(label: String, progress: Double?)
    }
    enum Action: Equatable { case enable, disable, restart, deleteData, uninstall, rollback(to: String), openKeyboardSettings }

    let id: String
    let name: String
    let tagline: String
    let version: String
    let poweredBy: Bool          // 不是螺壳出品的（id 不以 luoke- 开头）：放「Powered by 螺壳」
    let state: State
    let bundled: Bool
    let facts: [String]
    let actions: [Action]
}

struct AppsNoticeEntry: Equatable {
    let text: String
    let ok: Bool
    /// 输入法装好、重新启动以后：「打开键盘设置」
    let keyboardSettingsFor: String?
}

enum AppsSnapshot {
    static func entries(_ m: AppsModel) -> [AppEntry] {
        entries(rows: m.rows, phase: m.phase, progress: m.progress, fraction: m.fraction,
                previous: m.previous.mapValues { $0["previous"] as? String ?? "" })
    }

    static func entries(rows: [AppsModel.Row], phase: AppsModel.Phase, progress: String, fraction: Double?,
                        previous: [String: String]) -> [AppEntry] {
        rows.map { r in
            let state: AppEntry.State
            if case .updating(let id) = phase, id == r.id {
                state = .working(label: progress, progress: fraction)
            } else if r.installed == nil, let why = r.unavailable {
                state = .unavailable(reason: why)
            } else if r.canUpdate, let v = r.latest {
                state = .update(to: v)
            } else if r.installed == nil {
                state = .installable
            } else if r.disabled {
                state = .disabled
            } else {
                state = .installed
            }
            // 「更多」里的次序照原来的菜单：重新启动输入法、回到上一版、停用 / 删除数据；卸载是行尾的图标
            var actions: [AppEntry.Action] = []
            if r.installed != nil {
                if r.disabled { actions.append(.enable) }
                if r.type == "input-method" { actions.append(.restart) }
                if let v = previous[r.id] { actions.append(.rollback(to: v)) }
                if r.bundled {
                    if !r.disabled { actions.append(.disable) }
                    actions.append(.deleteData)
                } else {
                    actions.append(.uninstall)
                }
            }
            return AppEntry(id: r.id, name: r.name, tagline: r.tagline ?? "", version: r.installed ?? r.latest ?? "",
                            poweredBy: !r.id.hasPrefix("luoke-"), state: state, bundled: r.bundled,
                            facts: r.installed == nil ? facts(r) : [], actions: actions)
        }
    }

    /// 没装的：大小、从哪一档起能用、会要什么
    static func facts(_ r: AppsModel.Row) -> [String] {
        var out: [String] = []
        if r.size > 0 { out.append("约 " + Diagnostics.humanSize(r.size)) }
        if let t = r.minTier, let gb = Int(t), gb > 16 { out.append("\(gb) GB 内存起能用") }
        let needs = r.needs.map(\.title)
        if !needs.isEmpty { out.append("要开：" + needs.joined(separator: "、")) }
        return out
    }

    /// 做完一件事的那一句（AppsModel.phase 的 .done）
    static func notice(phase: AppsModel.Phase, imeAdded: Bool, rows: [AppsModel.Row]) -> AppsNoticeEntry? {
        guard case .done(let text, let ok) = phase else { return nil }
        let ime = imeAdded ? rows.first { $0.type == "input-method" }?.id : nil
        return AppsNoticeEntry(text: text, ok: ok, keyboardSettingsFor: ime)
    }
}
