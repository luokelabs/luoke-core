// 应用管理（早会 #13，负责人：螺壳和应用解耦、各自单独升级，螺壳里一个应用列表页来装、升级、卸载）：菜单「应用」打开。
// 每个应用一行：没装的「安装」（走加装流程），发布清单里有比已装更新的「升级」（只换这一个应用），已装的还能换回上一版、卸载。
// 螺壳本身只在「检查更新」里升级；可装的版本就是这一版螺壳的发布清单里那个（评测过的那个），换下来的旧版靠验证记录照常能用。
import AppKit
import SwiftUI

enum AppsWindow {
    static var window: NSWindow?
    static var model: AppsModel?

    static func show() {
        // 带主窗口的这一份：应用目录画在主窗口的「添加应用」页里，不另开窗口（负责人 11-09）。
        // 菜单栏、「去升级应用」、卸载完回来这些入口都经这里
        if let m = MainWindow.showApps() { model = m; return }
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
        guard let m = model else { return }
        m.resetDone()
        m.load()   // 主窗口的「添加应用」页在页面出现时才 load，这里马上就要找那一行
        guard case .idle = m.phase, let row = m.rows.first(where: { $0.id == id }), row.installed == nil, row.unavailable == nil else { return }
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
    /// 清单里的一项权限（工具协议 §1a 的 permissions：发布清单里抄的 listing.permissions，装了以后是应用自己的清单）。
    /// 协议的四种里，`system`（系统权限）、`account`（要你连的账号）、`each-time`（每次都要你点的事）是你给它的，放「会要」；
    /// `delegate`（交给它、不用点就自己做的）是它替你做的，放「会代你做」。不认得的种类当作要你给的
    struct Need: Equatable {
        let title: String
        let kind: String
        let facts: [String]
        var tcc: String? = nil
        /// 系统权限：`kind: "system"`，或者应用自己的清单写了 `tcc`（发布清单不抄 `tcc`）
        var system: Bool { kind == "system" || tcc != nil }
        /// 交给它自己做的：放「会代你做」
        var delegated: Bool { kind == "delegate" && tcc == nil }
    }
    /// 页上的三段，照这个次序
    enum Section: CaseIterable {
        case update, install, installed
        var title: String {
            switch self {
            case .update: return "有更新"
            case .install: return "可以装"
            case .installed: return "装好了"
            }
        }
    }
    struct Row: Identifiable {
        let id: String
        let name: String
        let type: String
        let installed: String?       // 已装的版本；nil = 没装
        let latest: String?          // 发布清单里的版本
        let size: Int64
        let unavailable: String?     // 这台 Mac 装不了的原因
        var bundled = false          // 在螺壳里：跟着螺壳升级，这里只显示版本
        var disabled = false         // 在螺壳里、还没开启（或用户停用了）
        var tagline: String? = nil   // 一句话介绍（listing.tagline）
        var needs: [Need] = []
        var memory = false           // 读写记忆
        var reports: [String]? = nil // 会主动找你（清单 capabilities.reports 的 title）：装了才读得到，nil 是不知道
        var actions: [String]? = nil // 会代你做（清单里会改东西的动作）：同上
        var data: [String] = []      // 数据放在哪（清单 data）：同上
        var minTier: String? = nil   // 从哪一档起能用：配方里它的任务开着的最低一档
        var canUpdate: Bool {
            guard let cur = installed, let new = latest else { return false }
            return unavailable == nil && new.compare(cur, options: .numeric) == .orderedDescending && !AccessoryRollback.skipped(id, new)
        }
        var section: Section { canUpdate ? .update : (installed == nil ? .install : .installed) }
        /// 装的时候走的几步（主控下载、核对签名、装到位；工具协议没有「本机试跑」这一步给应用）
        var steps: [String] { ["下载", "核对签名", type == "input-method" ? "装进「输入法」" : "装进「应用程序」"] }
    }
    enum Phase: Equatable { case idle, updating(String), done(String, Bool) }
    @Published var imeAdded = false               // 刚装好输入法：提示行旁边给「打开键盘设置」

    @Published var rows: [Row] = []
    @Published var previous: [String: [String: Any]] = [:]     // 有上一版可以换回的应用（主控 /accessories/previous 那一项）
    @Published var phase: Phase = .idle
    @Published var progress = ""
    /// 下载了多少（0…1）；字节齐了、主控还没说装好，就是在核对签名、装到位
    @Published var fraction: Double?
    /// 正看着哪个应用的详情（nil 是目录）
    @Published var shown: String?

    var busy: Bool { if case .updating = phase { return true } else { return false } }

    /// 装、升级这一个应用走到第几步（`Row.steps` 的下标）；没在装它是 nil
    func step(_ r: Row) -> Int? {
        guard case .updating(let id) = phase, id == r.id else { return nil }
        return (fraction ?? 0) >= 1 ? 1 : 0
    }

    /// 上一件做完的那句收起来，回到 .idle（还在做的 .updating 不动）。主窗口里这一份整个进程共用，
    /// 每次进页面、交来预选时调：不然预选装不了，「有新版本」也不出
    func resetDone() {
        guard case .done = phase else { return }
        phase = .idle
        imeAdded = false
    }

    func load() {
        if FirstRunModel.previewing { return }
        // 旧的「螺壳 agent.app」已并进螺壳：还装着也不列（卸载螺壳时照旧列出来删）
        let installed = Accessories.installed(includeDisabled: true).filter { $0.bundleID != Accessories.memoryBundleID && !Accessories.retired($0) }
        let items = Manifests.items(), recipe = Manifests.recipe()
        var out: [Row] = []
        for a in Manifests.accessories() where a.type != "component" && !installed.contains(where: { $0.accessory == a.id && $0.bundled }) {   // 本体里带的走下面
            let cur = installed.first { $0.accessory == a.id }
            let unavailable: String? = Manifests.macOSSatisfies(a.minMacOS) ? nil
                : "需要 macOS \(a.minMacOS ?? "") 或更新；这台 Mac 是 \(ProcessInfo.processInfo.operatingSystemVersion.majorVersion)"
            var row = Row(id: a.id, name: a.name, type: a.type, installed: cur?.version, latest: a.version, size: a.size, unavailable: unavailable)
            Self.describe(&row, manifest: cur.flatMap(Self.manifest), item: items[a.id], recipe: recipe)
            out.append(row)
        }
        for m in installed where !out.contains(where: { $0.id == m.accessory }) {   // 清单里没有的（开发版没有清单）：只列出来
            var row = Row(id: m.accessory, name: m.name, type: m.type, installed: m.version, latest: nil, size: 0, unavailable: nil,
                          bundled: m.bundled,
                          disabled: m.bundled && BundledApps.named(m.accessory)?.enabled == false)
            Self.describe(&row, manifest: Self.manifest(m), item: nil, recipe: recipe)
            out.append(row)
        }
        rows = out
        if let id = shown, !out.contains(where: { $0.id == id }) { shown = nil }
        DispatchQueue.global().async {
            let l = (try? HubModel.shared.client.request("GET", "/accessories/previous", timeout: 20))?.1["accessories"] as? [[String: Any]] ?? []
            var ok: [String: [String: Any]] = [:]
            for c in l where c["verified"] as? Bool == true { if let id = c["accessory"] as? String { ok[id] = c } }
            DispatchQueue.main.async { self.previous = ok }
        }
    }

    /// 装着的应用自己的清单：本体里带的在 Contents/Resources/<名字>/，单独装的在它自己的包里
    static func manifest(_ m: Accessories.Manifest) -> [String: Any]? {
        let url = m.bundled
            ? AppInfo.resources.appendingPathComponent(m.accessory.replacingOccurrences(of: "luoke-", with: "")).appendingPathComponent("luoke-accessory.json")
            : m.app.appendingPathComponent("Contents/Resources/luoke-accessory.json")
        return Manifests.json(url.path)
    }

    /// 装之前看得到的（「添加应用」，工具协议 §1a）：一句话介绍、会要什么、读不读写记忆从发布清单来；装了以后照应用自己的清单，
    /// 还看得到会主动找你、会代你做、数据在哪（发布清单不抄这几样）。从哪一档起照配方
    static func describe(_ row: inout Row, manifest: [String: Any]?, item: [String: Any]?, recipe: [String: Any]?) {
        let caps = manifest?["capabilities"] as? [String: Any]
        let listing = (caps?["listing"] as? [String: Any]) ?? (item?["listing"] as? [String: Any])
        row.tagline = (listing?["tagline"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        let perms = (caps?["permissions"] as? [[String: Any]]) ?? ((item?["listing"] as? [String: Any])?["permissions"] as? [[String: Any]]) ?? []
        row.needs = perms.compactMap { p in
            guard let t = p["title"] as? String, !t.isEmpty else { return nil }
            return Need(title: t, kind: p["kind"] as? String ?? "", facts: p["facts"] as? [String] ?? [], tcc: p["tcc"] as? String)
        }
        row.memory = (manifest?["memory"] as? Bool) ?? (item?["memory"] as? Bool) ?? false
        if let caps {
            row.reports = (caps["reports"] as? [[String: Any]] ?? []).compactMap { $0["title"] as? String }
            row.actions = (caps["actions"] as? [[String: Any]] ?? []).filter { $0["writes"] as? Bool == true }.compactMap { $0["title"] as? String }
        }
        row.data = manifest?["data"] as? [String] ?? []
        row.minTier = minTier(row.id, recipe: recipe)
    }

    /// 这个应用从哪一档起能用：配方里它的任务（`tasks.*.accessory`）开着的档里最低的那个；配方里没有它的任务是 nil
    static func minTier(_ accessory: String, recipe r: [String: Any]?) -> String? {
        guard let r, let tasks = r["tasks"] as? [String: Any], let tiers = r["tiers"] as? [String: Any] else { return nil }
        let mine = tasks.keys.filter { (tasks[$0] as? [String: Any])?["accessory"] as? String == accessory }
        let open = tiers.keys.filter { t in mine.contains { Manifests.taskOpen($0, tier: t, recipe: r) } }
        return open.min { (Int($0) ?? 0) < (Int($1) ?? 0) }
    }

    /// 只升级这一个应用：先让它退出（常驻的压住重启），请螺壳按清单下载、核对、替换，装好再拉起来
    func update(_ row: Row) {
        guard let cur = Accessories.installed().first(where: { $0.accessory == row.id }) else { return }
        phase = .updating(row.id)
        fraction = nil
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
        AppInstall.download([row.id]) { text, f in DispatchQueue.main.async { self.progress = text; self.fraction = f } }
    }

    /// 螺壳里带的应用：开启时要权限，要到才开
    func enable(_ row: Row) {
        // 首次运行没走完时螺壳进程还没起来，开启会报「连不上螺壳」：和安装一样先回欢迎窗口（不预选：螺壳里带的应用不在那里的勾选列表里）
        if FirstRun.needed() { AppsWindow.window?.close(); FirstRun.show(); return }
        guard row.id != SearchHelper.accessory else {
            if !SearchActivation.show(done: { self.load() }) { notOpened(row) }
            return
        }
        guard row.id != ScanHelper.accessory else {
            if !PermissionWindow.show(app: row.id, id: "fda", enabling: true, done: { self.turnOn(row) }) { notOpened(row) }
            return
        }
        turnOn(row)
    }

    /// 主窗口里另一张卡片正忙着（正问着系统），开启的那张没开：说一声，不像点了没反应
    private func notOpened(_ row: Row) {
        phase = .done("\(row.name)还没开启：螺壳正等着另一件事做完，做完再点「开启」。", false)
    }

    private func turnOn(_ row: Row) {
        phase = .updating(row.id)
        progress = "正在开启\(row.name)…"
        BundledApps.named(row.id)?.enable { why in
            self.phase = .done(why.map { "\(row.name)没能开启：\($0)" } ?? "\(row.name)开启了", why == nil)
            self.load()
        }
    }

    /// 螺壳里带的应用：停用就在这一行里做，不进卸载窗口
    func disable(_ row: Row) {
        phase = .updating(row.id)
        progress = "正在停用\(row.name)…"
        BundledApps.named(row.id)?.disable { why in
            self.phase = .done(why.map { "\(row.name)没能停用：\($0)" } ?? "\(row.name)停用了，数据都留着。要用时点「开启」。", why == nil)
            self.load()
        }
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
        shown = row.id               // 装的时候看着它的详情：一步步走到哪了（对照稿）
        phase = .updating(row.id)
        fraction = nil
        progress = "正在准备…"
        DispatchQueue.global().async {
            let (ok, why) = AppInstall.install(row.id) { text, f in DispatchQueue.main.async { self.progress = text; self.fraction = f } }
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
    /// 画在主窗口的「添加应用」页里：标题和滚动由主窗口给，没有「完成」，宽度跟着窗口；页面出现时收起上次那句、load（从侧栏进来也有目录）
    var inWindow = false

    var body: some View {
        if inWindow {
            VStack(alignment: .leading, spacing: 0) { content }
                .frame(maxWidth: .infinity, alignment: .leading)
                .onAppear {
                    m.resetDone()
                    if !m.busy { m.shown = nil }      // 从侧栏回来是目录；正装着的那个照旧看着它
                    m.load()
                }
        } else {
            LKPage {
                LKTitle(title: "应用")
                content
            } footer: {
                Spacer()
                Button("完成") { AppsWindow.window?.close() }.buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction)
            }
            .frame(width: 560, height: 460)
        }
    }

    @ViewBuilder private var content: some View {
        if case .done(let text, let ok) = m.phase {
            LKNote(icon: ok ? "circle-check" : "triangle-alert", text: text, kind: ok ? .success : .danger).padding(.bottom, m.imeAdded ? 8 : 12)
            if m.imeAdded {
                Button("打开键盘设置") { FirstRunModel(preselect: nil).openKeyboardSettings() }.buttonStyle(LKSecondaryButton()).padding(.bottom, 12)
            }
        }
        if let id = m.shown, let r = m.rows.first(where: { $0.id == id }) {
            AppDetailView(m: m, r: r, wide: inWindow)
        } else if m.rows.isEmpty {
            LKNote(icon: "info", text: "这一版螺壳没带发布清单（开发版），没有可装的应用。")
        } else {
            // 对照稿（luoke-agent docs/主窗口原型.html 的「应用」）：有更新、可以装、装好了三段，一个应用一张卡片；
            // 主窗口里两列（同一行的卡片一样高），自己的小窗口里一列
            let sections = AppsModel.Section.allCases.filter { s in m.rows.contains { $0.section == s } }
            let cols = inWindow ? 2 : 1
            ForEach(Array(sections.enumerated()), id: \.element) { i, s in
                let rows = m.rows.filter { $0.section == s }
                // 第一段接在页标题下面：主窗口的页标题下面空 16，对照稿 .ph 和 .sec 之间是 28；自己的小窗口里标题下面空 20
                AppsSectionTitle(title: s.title, count: rows.count, top: i > 0 ? 28 : (inWindow ? 12 : 0))
                Grid(alignment: .topLeading, horizontalSpacing: 12, verticalSpacing: 12) {
                    ForEach(Array(stride(from: 0, to: rows.count, by: cols)), id: \.self) { k in
                        GridRow {
                            ForEach(rows[k..<min(k + cols, rows.count)]) { r in AppTile(m: m, r: r) }
                            if k + cols > rows.count {
                                Color.clear.frame(maxWidth: .infinity).gridCellUnsizedAxes(.vertical)
                            }
                        }
                    }
                }
            }
        }
    }
}

// 下面几样照对照稿（luoke-agent docs/主窗口原型.html 的「应用」）的 CSS 画：正文 14、行高 1.6；段标题 .sec；卡片 .mk；按钮 .btn.sm；
// 小标记 .badge；详情 .hero、.two、.group、.step、.kv。行高 1.6 用上下各留一点补出来（系统字自己的行高约 1.2 倍字号）

private extension View {
    /// CSS 的 line-height 1.6：一行字上下各补 0.2 倍字号；几行的再加行距
    func cssLines(_ size: CGFloat) -> some View { lineSpacing(size * 0.4).padding(.vertical, size * 0.2) }
}

/// 对照稿的卡片和一组行（.mk、.group）：card 底、圆角 14；阴影是 --card-shadow：浅色两层淡影，深色一圈 6% 的亮边
private struct AppsCardSurface: ViewModifier {
    @Environment(\.colorScheme) private var scheme
    private static let shade = Color(red: 58 / 255, green: 31 / 255, blue: 20 / 255)
    func body(content: Content) -> some View {
        content
            .background(Color.lkCard).clipShape(.rect(cornerRadius: 14))
            .shadow(color: Self.shade.opacity(scheme == .dark ? 0 : 0.06), radius: 1, y: 1)
            .shadow(color: Self.shade.opacity(scheme == .dark ? 0 : 0.10), radius: 6, y: 4)
            .overlay { if scheme == .dark { RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.06)) } }
    }
}

/// 对照稿的按钮 .btn.sm：高 30、圆角 8、左右 12、13 号中等字重、图标和字隔 8。
/// tint 是淡底（--tint：浅色 6% 的深褐、深色 8% 的米白），安装、开启、详情顶上的按钮都用它；primary 是深底（升级）；ghost 没有底（返回）
private struct AppsButton: ButtonStyle {
    enum Kind { case tint, primary, ghost }
    var kind = Kind.tint
    @Environment(\.isEnabled) private var enabled
    @Environment(\.colorScheme) private var scheme

    private func fill(pressed: Bool) -> Color {
        let ink = scheme == .dark ? Color(red: 1, green: 248 / 255, blue: 238 / 255) : Color(red: 58 / 255, green: 31 / 255, blue: 20 / 255)
        switch kind {
        case .primary: return Color.lkPrimary.opacity(pressed ? 0.88 : 1)
        case .tint: return ink.opacity(scheme == .dark ? (pressed ? 0.12 : 0.08) : (pressed ? 0.10 : 0.06))
        case .ghost: return pressed ? ink.opacity(scheme == .dark ? 0.08 : 0.06) : .clear
        }
    }

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .medium))
            .foregroundStyle(kind == .primary ? Color.lkOnPrimary : Color.lkForeground)
            .padding(.horizontal, kind == .ghost ? 8 : 12).frame(height: 30)
            .background(fill(pressed: configuration.isPressed)).clipShape(.rect(cornerRadius: 8))
            .opacity(enabled ? 1 : 0.4)
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .contentShape(.rect)
    }
}

/// 段的小标题：「有更新 1」「会要」（.sec：上 28、下 12，13 号半粗、次要色，数是常规字重）。
/// top：这一段上面空多少；页上第一段接在主窗口的页标题下面（标题自己下面空 16），补到 28
private struct AppsSectionTitle: View {
    let title: String
    var count: Int? = nil
    var top: CGFloat = 28
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(title).font(.system(size: 13, weight: .semibold))
            if let n = count { Text("\(n)").font(.system(size: 13)).monospacedDigit() }
        }
        .cssLines(13)
        .foregroundStyle(Color.lkMuted)
        .padding(.top, top).padding(.bottom, 12)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// 卡片上的小标记（.badge）：高 20、左右 7、圆角 6，12 号中等字重，图标 12、和字隔 4，次要色底
private struct AppFact: View {
    let icon: String
    let text: String
    var body: some View {
        HStack(spacing: 4) {
            LKIcon(icon, size: 12)
            Text(text).font(.system(size: 12, weight: .medium)).lineLimit(1)
        }
        .foregroundStyle(Color.lkMuted)
        .padding(.horizontal, 7).frame(height: 20)
        .background(Color.lkMutedBg).clipShape(.rect(cornerRadius: 6))
    }
}

/// 一个应用一张卡片（.mk）：四边 16；图标 40 在名字和介绍两行的中间，和字隔 12；名字 14 中等，介绍 13 次要色，两行隔 4；
/// 下面一行（.meta，上面再空 6）可以装的是大小、从哪一档起、要什么，别的是版本（等宽 14）。点卡片看详情
private struct AppTile: View {
    @ObservedObject var m: AppsModel
    let r: AppsModel.Row

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                LKAppIcon(accessory: r.id, size: 40)
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .center, spacing: 12) {
                        // 名字一行高 22.4（行高 1.6）；有按钮的一行跟着按钮是 30（对照稿 .mk 第一行）
                        Text(r.name).font(.system(size: 14, weight: .medium)).foregroundStyle(Color.lkForeground).lineLimit(1).cssLines(14)
                        Spacer(minLength: 0)
                        AppActions(m: m, r: r)
                    }
                    if let t = r.tagline {
                        Text(t).font(.system(size: 13)).foregroundStyle(Color.lkMuted).cssLines(13).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            meta.padding(.leading, 52).padding(.top, 10)
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .modifier(AppsCardSurface())
        .contentShape(.rect)
        .onTapGesture { m.shown = r.id }
        .accessibilityElement(children: .contain)
        .accessibilityAction(named: Text("看详情")) { m.shown = r.id }
    }

    @ViewBuilder private var meta: some View {
        if m.step(r) != nil {
            Text(m.progress).font(.system(size: 12)).foregroundStyle(Color.lkMuted).lineLimit(1)
        } else if r.installed == nil {
            if let why = r.unavailable {
                Text(why).font(.system(size: 12)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
            } else {
                LKFlow(spacing: 6, lineSpacing: 6) {
                    if r.size > 0 { AppFact(icon: "hard-drive", text: Diagnostics.humanSize(r.size)) }
                    if let t = r.minTier { AppFact(icon: "cpu", text: "\(t) GB 档起") }
                    if r.type == "input-method" { AppFact(icon: "keyboard", text: "要启用输入源") }
                    ForEach(r.needs.filter(\.system), id: \.title) { n in AppFact(icon: "settings", text: "要\(n.title)") }
                }
            }
        } else {
            Text(AppDetailView.versionText(r)).font(.system(size: 14, design: .monospaced)).foregroundStyle(Color.lkForeground)
                .lineLimit(1).cssLines(14)
        }
    }
}

/// 卡片和详情右上角：安装、开启是淡底按钮，升级是深底；已是最新是一个勾；装好了的还有「…」（换回上一版、停用、删除数据、卸载、重新启动输入法）
private struct AppActions: View {
    @ObservedObject var m: AppsModel
    let r: AppsModel.Row

    var body: some View {
        HStack(spacing: 6) {
            if m.step(r) != nil {
                ProgressView().controlSize(.small)
            } else if r.installed == nil {
                Button { m.install(r) } label: {
                    HStack(spacing: 8) { LKIcon("circle-arrow-down", size: 14); Text("安装") }
                }
                .buttonStyle(AppsButton()).disabled(r.unavailable != nil || m.busy)
            } else if r.disabled {
                Button("开启") { m.enable(r) }.buttonStyle(AppsButton()).disabled(m.busy)
                more
            } else if r.canUpdate {
                Button("升级") { m.update(r) }.buttonStyle(AppsButton(kind: .primary)).disabled(m.busy)
                more
            } else {
                LKIcon("circle-check", size: 14).foregroundStyle(Color.lkSuccess).help("已是最新").accessibilityLabel("已是最新")
                more
            }
        }
        .fixedSize()
    }

    private var more: some View {
        Menu {
            if r.type == "input-method" { Button("重新启动") { m.restartInputMethod(r) } }
            if let c = m.previous[r.id] { Button("回到上一版本（\(c["previous"] as? String ?? "")）") { m.rollback(r) } }
            if r.bundled {
                if !r.disabled { Button("停用") { m.disable(r) } }
                Button("删除数据…") { AppsWindow.window?.close(); DeleteData.show(accessory: r.id, name: r.name) }      // 一次只一个窗口
            } else {
                Button("卸载") { AppsWindow.window?.close(); Uninstall.show(preselect: r.id, back: { AppsWindow.show() }) }   // 一次只一个窗口：卸载窗口接替应用窗口
            }
        } label: { Lucide.menuImage("ellipsis") }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().disabled(m.busy)
            .foregroundStyle(Color.lkMuted)
            .frame(width: 24, height: 22)       // 不比名字那一行高：已是最新的卡片第一行照对照稿是 22.4
            .accessibilityLabel("更多操作")
    }
}

/// 一个应用的详情（对照稿的应用详情）：会要什么（系统权限、读写记忆）、会主动找你、会代你做（交给它的事、每次问你的、要你连的账号、
/// 会改东西的动作）；没装、有更新的有装的几步；版本、大小、档位、数据。画在同一页里，「全部应用」回到目录。
/// wide：主窗口里左右两栏（右栏 260、隔 20），自己的小窗口里一栏
struct AppDetailView: View {
    @ObservedObject var m: AppsModel
    let r: AppsModel.Row
    var wide = true

    static func versionText(_ r: AppsModel.Row) -> String {
        guard let cur = r.installed else { return r.latest ?? "" }
        if r.canUpdate { return "\(cur) → \(r.latest ?? "")" }
        if let why = r.unavailable, let new = r.latest, new.compare(cur, options: .numeric) == .orderedDescending { return "\(cur) · 新版 \(new) \(why)" }
        return cur
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { m.shown = nil } label: {
                HStack(spacing: 8) { LKIcon("chevron-left", size: 14); Text("全部应用") }
            }
            .buttonStyle(AppsButton(kind: .ghost)).padding(.leading, -8).padding(.bottom, 10)
            // .hero：图标 40、隔 16，名字 26 半粗（行高 1.25），下面一句 14；右边按钮，装的时候换成进度条（宽 200、高 6）
            HStack(alignment: .center, spacing: 16) {
                LKAppIcon(accessory: r.id, size: 40)
                VStack(alignment: .leading, spacing: 0) {
                    Text(r.name).font(.system(size: 26, weight: .semibold)).tracking(-0.26).foregroundStyle(Color.lkForeground)
                        .padding(.vertical, 26 * 0.025)
                    if let t = r.tagline {
                        Text(t).font(.system(size: 14)).foregroundStyle(Color.lkForeground).cssLines(14).fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 16)
                if let step = m.step(r) {
                    ProgressView(value: step == 0 ? (m.fraction ?? 0) : 1).progressViewStyle(AppsBar()).frame(width: 200)
                } else {
                    AppActions(m: m, r: r)
                }
            }
            if wide {
                HStack(alignment: .top, spacing: 20) {
                    VStack(alignment: .leading, spacing: 0) { left }.frame(maxWidth: .infinity, alignment: .leading)
                    VStack(alignment: .leading, spacing: 0) { right }.frame(width: 260, alignment: .leading)
                }
                .padding(.top, 20)
            } else {
                VStack(alignment: .leading, spacing: 0) { left; right }.padding(.top, 20)
            }
        }
    }

    // MARK: 左栏：会要、会主动找你、会代你做

    private enum Mark { case settings, withLuoke }
    private struct Line { let icon: String?; let title: String; let facts: [String]; var mark: Mark? = nil }

    /// 会要：你给它的（系统权限、要你连的账号、每次都要你点的事，照清单的次序）和读写记忆；系统权限带齿轮，读写记忆带锁
    private var needs: [Line] {
        var out: [Line] = []
        if r.type == "input-method" { out.append(Line(icon: "keyboard", title: "启用输入源", facts: [], mark: .settings)) }
        for n in r.needs where !n.delegated {
            out.append(Line(icon: "shield-check", title: n.title, facts: n.facts, mark: n.system ? .settings : nil))
        }
        if r.memory { out.append(Line(icon: "brain", title: "读写记忆", facts: [], mark: .withLuoke)) }
        return out
    }

    /// 会代你做：清单里交给它自己做的（`delegate`），再是会改东西的动作。
    /// 没装的发布清单不抄动作：只有权限说明里的几项时照列，什么都不知道就不画这一段
    private var doing: [Line]? {
        let delegated = r.needs.filter(\.delegated).map { Line(icon: nil, title: $0.title, facts: $0.facts) }
        guard let actions = r.actions else { return delegated.isEmpty ? nil : delegated }
        return delegated + actions.map { Line(icon: nil, title: $0, facts: []) }
    }

    @ViewBuilder private var left: some View {
        group("会要", needs)
        if let reports = r.reports { group("会主动找你", reports.map { Line(icon: nil, title: $0, facts: []) }) }
        if let doing { group("会代你做", doing) }
    }

    @ViewBuilder private func group(_ title: String, _ lines: [Line]) -> some View {
        AppsSectionTitle(title: title)
        VStack(spacing: 0) {
            if lines.isEmpty {
                Text("—").font(.system(size: 12)).foregroundStyle(Color.lkMuted)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 16).padding(.vertical, 12)
            }
            ForEach(Array(lines.enumerated()), id: \.offset) { i, l in
                if i > 0 { Rectangle().fill(Color.lkBorder).frame(height: 1) }
                row(l)
            }
        }
        .modifier(AppsCardSurface())
    }

    /// 一行（.row）：上下 12、左右 16，图标 16、隔 12；字 14；说的几条 12 次要色；右边的小标（.badge 只放图标）
    private func row(_ l: Line) -> some View {
        HStack(alignment: .center, spacing: 12) {
            if let icon = l.icon { LKIcon(icon, size: 16).foregroundStyle(Color.lkForeground) }
            VStack(alignment: .leading, spacing: 0) {
                Text(l.title).font(.system(size: 14)).foregroundStyle(Color.lkForeground).cssLines(14).fixedSize(horizontal: false, vertical: true)
                if !l.facts.isEmpty {
                    Text(l.facts.joined(separator: "；")).font(.system(size: 12)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            switch l.mark {
            case .settings: badge("settings", "在系统设置里打开")
            case .withLuoke: badge("lock", "随螺壳")
            case nil: EmptyView()
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
    }

    private func badge(_ icon: String, _ help: String) -> some View {
        LKIcon(icon, size: 12).foregroundStyle(Color.lkMuted)
            .padding(.horizontal, 7).frame(height: 20).background(Color.lkMutedBg).clipShape(.rect(cornerRadius: 6))
            .help(help).accessibilityLabel(help)
    }

    // MARK: 右栏：装的几步、版本、大小、档位、数据

    @ViewBuilder private var right: some View {
        if r.installed == nil || r.canUpdate {
            AppsSectionTitle(title: "步骤", top: 28)
            VStack(spacing: 0) {
                let now = m.step(r)
                ForEach(Array(r.steps.enumerated()), id: \.offset) { k, t in
                    if k > 0 { Rectangle().fill(Color.lkBorder).frame(height: 1) }
                    HStack(spacing: 10) {
                        AppsStepDot(state: now.map { k < $0 ? .done : (k == $0 ? .now : .wait) } ?? .wait)
                        Text(t).font(.system(size: 14)).foregroundStyle(Color.lkForeground).cssLines(14)
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 14).padding(.vertical, 10)
                }
            }
            .modifier(AppsCardSurface())
            if m.step(r) != nil {
                Text(m.progress).font(.system(size: 12)).foregroundStyle(Color.lkMuted).padding(.top, 6)
            }
        }
        // .kv：上面空 16（右栏只有它时和左栏的段标题对齐），左边一栏 120、隔 16，上下隔 8，13 号；版本、数据等宽
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
            GridRow { key("版本"); Text(Self.versionText(r)).font(.system(size: 13, design: .monospaced)) }
            if r.size > 0 { GridRow { key("大小"); Text(Diagnostics.humanSize(r.size)).font(.system(size: 13)) } }
            if let t = r.minTier { GridRow { key("档位"); Text("\(t) GB 起").font(.system(size: 13)) } }
            if !r.data.isEmpty {
                GridRow { key("数据"); Text(r.data.joined(separator: "\n")).font(.system(size: 13, design: .monospaced)).fixedSize(horizontal: false, vertical: true) }
            }
        }
        .foregroundStyle(Color.lkForeground)
        .padding(.top, (r.installed == nil || r.canUpdate) ? 16 : (wide ? 28 + 13 * 1.6 + 12 : 28))
    }

    private func key(_ t: String) -> some View {
        Text(t).font(.system(size: 13)).foregroundStyle(Color.lkMuted).frame(width: 120, alignment: .leading).gridColumnAlignment(.leading)
    }
}

/// 步骤前面的圈（.dotst）：18、1 像素的边；在做的是品牌色的边，做完的是绿底白勾
private struct AppsStepDot: View {
    enum State { case wait, now, done }
    let state: State
    var body: some View {
        ZStack {
            switch state {
            case .wait: Circle().strokeBorder(Color.lkInput, lineWidth: 1)
            case .now: Circle().strokeBorder(Color.lkBrand, lineWidth: 1)
            case .done:
                Circle().fill(Color.lkSuccess)
                LKIcon("check", size: 12).foregroundStyle(.white)
            }
        }
        .frame(width: 18, height: 18)
    }
}

/// 装的时候详情右上角的进度条（.prog）：高 6、圆角 3，次要色底、品牌色
private struct AppsBar: ProgressViewStyle {
    func makeBody(configuration: Configuration) -> some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.lkMutedBg)
                Capsule().fill(Color.lkBrand).frame(width: g.size.width * (configuration.fractionCompleted ?? 0))
            }
        }
        .frame(height: 6)
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
