// 应用管理（早会 #13，负责人：螺壳和应用解耦、各自单独升级，螺壳里一个应用列表页来装、升级、卸载）：菜单「应用」打开。
// 每个应用一行：没装的「安装」（走加装流程），发布清单里有比已装更新的「升级」（只换这一个应用），已装的还能换回上一版、卸载。
// 螺壳本身只在「检查更新」里升级；可装的版本就是这一版螺壳的发布清单里那个（评测过的那个），换下来的旧版靠验证记录照常能用。
import AppKit
import SwiftUI

enum AppsWindow {
    static var window: NSWindow?
    static var model: AppsModel?

    static func show() {
        if let w = window, w.isVisible { model?.load(); Uninstall.bringFront(w); return }
        let m = AppsModel()
        let w = NSWindow(contentViewController: NSHostingController(rootView: AppsView(m: m)))
        w.title = "应用"
        w.styleMask = [.titled, .closable]
        w.setContentSize(NSSize(width: 560, height: 460))
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        model = m
        AppWindows.track(w)
        Uninstall.bringFront(w)
        m.load()
    }

    /// 应用的安装器交来的预选（首次安装之后）：打开应用窗口，在那一行里装（10-23：装应用只在这一个窗口里）
    static func install(_ id: String) {
        // 首次运行没走完（底座、引擎还没有）：先回首次运行，把这个应用预选上，不在「应用」里单独装（10-24 核出）
        if FirstRun.needed() { FirstRun.show(preselect: id); return }
        show()
        guard let m = model, case .idle = m.phase, let row = m.rows.first(where: { $0.id == id }), row.installed == nil, row.unavailable == nil else { return }
        m.install(row)
    }

    /// 已装的应用里，发布清单有更新版本的（用户换回上一版时跳过的那一版不算）：菜单上提示「有新版本」用
    static func updatable() -> [Manifests.AccessoryItem] {
        let installed = Accessories.installed()
        return Manifests.accessories().filter { a in
            guard a.type != "component", let cur = installed.first(where: { $0.accessory == a.id }) else { return false }
            return Manifests.macOSSatisfies(a.minMacOS) && a.version.compare(cur.version, options: .numeric) == .orderedDescending && !AccessoryRollback.skipped(a.id, a.version)
        }
    }
}

final class AppsModel: ObservableObject {
    struct Row: Identifiable {
        let id: String
        let name: String
        let type: String
        let installed: String?       // 已装的版本；nil = 没装
        let latest: String?          // 发布清单里的版本
        let size: Int64
        let unavailable: String?     // 这台 Mac 装不了的原因
        var canUpdate: Bool {
            guard let cur = installed, let new = latest else { return false }
            return unavailable == nil && new.compare(cur, options: .numeric) == .orderedDescending && !AccessoryRollback.skipped(id, new)
        }
    }
    enum Phase: Equatable { case idle, updating(String), done(String, Bool) }
    @Published var imeAdded = false               // 刚装好输入法：提示行旁边给「打开键盘设置」

    @Published var rows: [Row] = []
    @Published var previous: [String: [String: Any]] = [:]     // 有上一版可以换回的应用（主控 /accessories/previous 那一项）
    @Published var phase: Phase = .idle
    @Published var progress = ""

    func load() {
        if FirstRunModel.previewing { return }
        let installed = Accessories.installed().filter { $0.bundleID != Accessories.memoryBundleID }
        var out: [Row] = []
        for a in Manifests.accessories() where a.type != "component" {
            let cur = installed.first { $0.accessory == a.id }
            let unavailable: String? = Manifests.macOSSatisfies(a.minMacOS) ? nil
                : "需要 macOS \(a.minMacOS ?? "") 或更新；这台 Mac 是 \(ProcessInfo.processInfo.operatingSystemVersion.majorVersion)"
            out.append(Row(id: a.id, name: a.name, type: a.type, installed: cur?.version, latest: a.version, size: a.size, unavailable: unavailable))
        }
        for m in installed where !out.contains(where: { $0.id == m.accessory }) {   // 清单里没有的（开发版没有清单）：只列出来
            out.append(Row(id: m.accessory, name: m.name, type: m.type, installed: m.version, latest: nil, size: 0, unavailable: nil))
        }
        rows = out
        DispatchQueue.global().async {
            let l = (try? HubModel.shared.client.request("GET", "/accessories/previous", timeout: 20))?.1["accessories"] as? [[String: Any]] ?? []
            var ok: [String: [String: Any]] = [:]
            for c in l where c["verified"] as? Bool == true { if let id = c["accessory"] as? String { ok[id] = c } }
            DispatchQueue.main.async { self.previous = ok }
        }
    }

    /// 只升级这一个应用：先让它退出（常驻的压住重启），请螺壳按清单下载、核对、替换，装好再拉起来
    func update(_ row: Row) {
        guard let cur = Accessories.installed().first(where: { $0.accessory == row.id }) else { return }
        phase = .updating(row.id)
        progress = "先让\(row.name)退出…"
        let isApp = cur.type == "app"
        let wasRunning = isApp && Accessories.isRunning(cur.bundleID)
        if isApp { Accessories.noRestart.insert(cur.bundleID) }
        DispatchQueue.global().async {
            // 输入法不在这里退出：下载可能要几分钟，提前结束只会让用户没输入法可用；螺壳换好包以后再结束旧进程、立刻拉起新版（d1d7ef6）
            if isApp { Uninstall.terminate(bundleID: cur.bundleID, markNoRestart: false) }
            let (ok, why) = self.download(row)
            DispatchQueue.main.async {
                Accessories.noRestart.remove(cur.bundleID)
                Accessories.launchResident()
                if wasRunning, let m = Accessories.installed().first(where: { $0.accessory == row.id }), !Accessories.isRunning(m.bundleID) {
                    AppPlacement.open(m.app)
                }
                let now = Accessories.installed().first { $0.accessory == row.id }?.version ?? ""   // 以装上的为准，不照抄清单
                self.phase = .done(ok ? (now == row.latest ? "\(row.name)已升级到 \(now)" : "\(row.name)装好了，但装上的是 \(now)，不是清单上的 \(row.latest ?? "")")
                                      : "\(row.name)没升级上" + (why.isEmpty ? "" : "：\(why)") + "。旧版还在，照常能用。", ok)
                self.load()
            }
        }
    }

    private func download(_ row: Row) -> (Bool, String) {
        AppInstall.download([row.id]) { text, _ in DispatchQueue.main.async { self.progress = text } }
    }

    /// 换回上一版也在这一行里做，不另开窗口（10-23）
    func rollback(_ row: Row) {
        guard let c = previous[row.id] else { return }
        phase = .updating(row.id)
        progress = "正在回到上一版本 \(c["previous"] as? String ?? "")…"
        AccessoryRollback.run(c) { title, _, ok in
            self.phase = .done(title, ok)
            self.load()
        }
    }

    /// 输入法在系统的输入法菜单里变灰、切不过去时（luoke-ime#18）：重启输入法进程就好，和换版以后拉起新包一样
    func restartInputMethod(_ row: Row) {
        phase = .updating(row.id)
        progress = "正在重新启动\(row.name)…"
        DispatchQueue.global().async {
            let r = Result { try HubModel.shared.client.request("POST", "/accessories/restart", body: ["accessory": row.id], timeout: 30) }
            DispatchQueue.main.async {
                switch r {
                case .success: self.phase = .done("\(row.name)已重新启动", true)
                case .failure(let e): self.phase = .done("\(row.name)没能重新启动：\((e as? HubError)?.message ?? "\(e)")", false)
                }
            }
        }
    }

    /// 没装的应用就在这一行里装（10-23：首次安装之后都走简单流程，不另开窗口）
    func install(_ row: Row) {
        // 首次运行没走完：回首次运行、把它预选上（行上的「安装」按钮也走这里，不只 AppsWindow.install；luoke-top 10-24 核出）
        if FirstRun.needed() { AppsWindow.window?.close(); FirstRun.show(preselect: row.id); return }
        phase = .updating(row.id)
        progress = "正在准备…"
        DispatchQueue.global().async {
            let (ok, why) = AppInstall.install(row.id) { text, _ in DispatchQueue.main.async { self.progress = text } }
            DispatchQueue.main.async {
                self.imeAdded = ok && row.type == "input-method"
                self.phase = .done(ok ? AppInstall.doneText(row.id, name: row.name) : "\(row.name)没装上" + (why.isEmpty ? "" : "：\(why)") + "。可以再点「安装」重试。", ok)
                self.load()
            }
        }
    }

}

struct AppsView: View {
    @ObservedObject var m: AppsModel

    var body: some View {
        LKPage {
            LKTitle(title: "应用")
            if case .done(let text, let ok) = m.phase {
                LKNote(icon: ok ? "checkmark.circle" : "exclamationmark.triangle", text: text, kind: ok ? .success : .danger).padding(.bottom, m.imeAdded ? 8 : 12)
                if m.imeAdded {
                    Button("打开键盘设置") { FirstRunModel(preselect: nil).openKeyboardSettings() }.buttonStyle(LKSecondaryButton()).padding(.bottom, 12)
                }
            }
            let updatable = m.rows.filter(\.canUpdate)
            if m.phase == .idle, !updatable.isEmpty {
                LKNote(icon: "arrow.up.circle", text: updatable.map(\.name).joined(separator: "、") + " 有新版本。升级螺壳不会顺带升级应用，在下面单独升级。", kind: .warning)
                    .padding(.bottom, 12)
            }
            if m.rows.isEmpty {
                LKNote(icon: "info.circle", text: "这一版螺壳没带发布清单（开发版），没有可装的应用。")
            } else {
                LKGroup {
                    ForEach(Array(m.rows.enumerated()), id: \.element.id) { i, r in
                        if i > 0 { Divider().overlay(Color.lkBorder) }
                        row(r)
                    }
                }
            }
        } footer: {
            Spacer()
            Button("完成") { AppsWindow.window?.close() }.buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction)
        }
        .frame(width: 560, height: 460)
    }

    private func detail(_ r: AppsModel.Row) -> String {
        if case .updating(let id) = m.phase, id == r.id { return m.progress }
        if let why = r.unavailable, r.installed == nil { return why }
        guard let cur = r.installed else { return [r.latest, r.size > 0 ? Diagnostics.humanSize(r.size) : nil].compactMap { $0 }.joined(separator: " · ") }
        if r.canUpdate { return "\(cur) → \(r.latest ?? "")" }
        if let why = r.unavailable, let new = r.latest, new.compare(cur, options: .numeric) == .orderedDescending { return "\(cur) · 新版 \(new) \(why)" }
        return cur
    }

    private var busy: Bool { if case .updating = m.phase { return true } else { return false } }

    @ViewBuilder private func row(_ r: AppsModel.Row) -> some View {
        HStack(spacing: 12) {
            LKAppIcon(accessory: r.id)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(r.name).font(.system(size: 14, weight: .medium))
                    if r.installed == nil { LKBadge(text: "未安装", kind: .outline) }
                    else if r.canUpdate { LKBadge(text: "有新版本", kind: .brand) }
                }
                Text(detail(r)).font(.system(size: 13)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            if case .updating(let id) = m.phase, id == r.id {
                ProgressView().controlSize(.small)
            } else if r.installed == nil {
                Button("安装") { m.install(r) }
                    .buttonStyle(LKPrimaryButton()).disabled(r.unavailable != nil || busy)
            } else {
                if r.canUpdate { Button("升级") { m.update(r) }.buttonStyle(LKPrimaryButton()).disabled(busy) }
                Menu {
                    if r.type == "input-method" { Button("重新启动") { m.restartInputMethod(r) } }
                    if let c = m.previous[r.id] { Button("回到上一版本（\(c["previous"] as? String ?? "")）") { m.rollback(r) } }
                    Button("卸载") { AppsWindow.window?.close(); Uninstall.show(preselect: r.id, back: { AppsWindow.show() }) }   // 一次只一个窗口：卸载窗口接替应用窗口
                } label: { Image(systemName: "ellipsis") }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().disabled(busy)
                    .accessibilityLabel("更多操作")
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
    }
}

/// 装一个应用 / 升级一个应用都走这里：交给螺壳按清单下载、核对、替换，看 /status 的进度直到完成
enum AppInstall {
    /// 要一起装的：这个应用，加上它读写记忆、记忆还没装时的螺壳记忆
    static func names(for id: String) -> [String] {
        var out = [id]
        let all = Manifests.accessories()
        let installed = Set(Accessories.installed().map(\.accessory))
        if all.first(where: { $0.id == id })?.memory == true, let mem = all.first(where: { $0.type == "component" }), !installed.contains(mem.id) { out.append(mem.id) }
        return out
    }

    /// 装好后打开：常驻的按常驻拉起，别的应用照普通打开；输入法登记一下，让它出现在系统的输入法列表里
    static func install(_ id: String, progress: @escaping (String, Double?) -> Void) -> (Bool, String) {
        let r = download(names(for: id), progress: progress)
        guard r.0, let m = Accessories.installed().first(where: { $0.accessory == id }) else { return r }
        DispatchQueue.main.async {
            Accessories.launchResident()
            if m.type == "app", !Accessories.isRunning(m.bundleID) { AppPlacement.open(m.app) }
        }
        if m.type == "input-method", let exe = Bundle(url: m.app)?.executableURL,
           FirstRunModel.inputMethodState(exe: exe, bundleID: m.bundleID) == "not_registered" { Installer.shell(exe.path, ["--register"]) }
        return r
    }

    static func doneText(_ id: String, name: String) -> String {
        let ime = Accessories.installed().first(where: { $0.accessory == id })?.type == "input-method"
        return ime ? "\(name)装好了。" + imeHint : "\(name)装好了，已经打开。"
    }
    private static let imeHint = "到 系统设置 → 键盘 → 输入法 里点「+」，添加「螺壳」。"


    /// POST /downloads，看 /status 的进度直到都完成。返回（成没成，失败原因）
    static func download(_ names: [String], progress: @escaping (String, Double?) -> Void) -> (Bool, String) {
        let client = HubModel.shared.client
        do { _ = try client.request("POST", "/downloads", body: ["names": names]) }
        catch { return (false, (error as? HubError)?.message ?? "螺壳没接下载请求") }
        var misses = 0
        while true {
            Thread.sleep(forTimeInterval: 1)
            guard let (_, st) = try? client.request("GET", "/status"), let dl = st["downloads"] as? [String: [String: Any]],
                  names.allSatisfy({ dl[$0] != nil }) else {
                misses += 1
                if misses >= 30 { return (false, "螺壳 30 秒没响应") }   // 螺壳退出或重启了：别一直停在「正在下载」
                continue
            }
            misses = 0
            if let bad = names.first(where: { dl[$0]?["state"] as? String == "failed" }) { return (false, dl[bad]?["error"] as? String ?? "") }
            if names.allSatisfy({ dl[$0]?["state"] as? String == "done" }) { return (true, "") }
            let done = names.reduce(0.0) { $0 + ((dl[$1]?["done"] as? NSNumber)?.doubleValue ?? 0) }
            let total = names.reduce(0.0) { $0 + ((dl[$1]?["total"] as? NSNumber)?.doubleValue ?? 0) }
            progress(total > 0 ? "正在下载 \(Int(done / total * 100))%（\(Diagnostics.humanSize(Int64(done))) / \(Diagnostics.humanSize(Int64(total)))）" : "正在下载…", total > 0 ? done / total : nil)
        }
    }
}
