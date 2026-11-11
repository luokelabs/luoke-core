import AppKit
import SwiftUI

// 螺壳界面设计规范 v1（luoke-brand ui/DESIGN.md）在 SwiftUI 里的写法：颜色 token（浅色暖、深色中性灰）、主按钮、列表组、
// 安装 / 升级窗口的左栏和底栏。安装、升级、完成三页照样板（ui/src/install/Install.tsx）改（10-21 定 beta1 前改）。

extension Color {
    init(light: UInt32, dark: UInt32) {
        self.init(nsColor: NSColor(name: nil) { a in
            let v = a.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: CGFloat((v >> 16) & 0xFF) / 255, green: CGFloat((v >> 8) & 0xFF) / 255,
                           blue: CGFloat(v & 0xFF) / 255, alpha: 1)
        })
    }
    static let lkBackground = Color(light: 0xFCFAF7, dark: 0x111111)
    static let lkCard       = Color(light: 0xFFFFFF, dark: 0x1A1A1A)
    static let lkSidebar    = Color(light: 0xF7F2EB, dark: 0x161616)
    static let lkForeground = Color(light: 0x2B1A12, dark: 0xFAFAF9)
    static let lkMuted      = Color(light: 0x7A6558, dark: 0xB8B8B6)
    static let lkMutedBg    = Color(light: 0xF4EEE6, dark: 0x232323)
    static let lkBorder     = Color(light: 0xECE4DA, dark: 0x2A2A2A)
    static let lkInput      = Color(light: 0xE2D8CC, dark: 0x3A3A3A)
    static let lkPrimary    = Color(light: 0x3A1F14, dark: 0xF5F5F4)
    static let lkOnPrimary  = Color(light: 0xFDF8F1, dark: 0x141414)
    static let lkBrand      = Color(light: 0xE8556B, dark: 0xF2707F)
    static let lkBrandSoft  = Color(light: 0xFCE8EA, dark: 0x3B1F24)
    static let lkSuccess    = Color(light: 0x2F7A4F, dark: 0x5CC98A)
    static let lkSuccessSoft = Color(light: 0xE4F2E8, dark: 0x16301F)
    static let lkWarning    = Color(light: 0xA86A12, dark: 0xE8B45A)
    static let lkWarningSoft = Color(light: 0xFBF0D9, dark: 0x33280F)
    static let lkDanger     = Color(light: 0xC2412D, dark: 0xF07A66)
}

/// 这块界面画在哪（11-10：螺壳只有一个窗口）：自己的小窗口（没带主窗口的这一份、安装器），或者主窗口里的一页、一张卡片、整个窗口
/// （`MainWindow.present` 设）。主窗口里的一页：主窗口给标题和滚动（没有「返回」，用户点侧栏或界面里自己的按钮离开）；
/// 卡片：主窗口给标题、× 和边距；整个窗口：内容顶到窗口上沿，红黄绿压在左上角，照 `lkTopInset` 让开
enum LKHostForm { case window, page, card, takeover }

private struct LKHostFormKey: EnvironmentKey { static let defaultValue = LKHostForm.window }

extension EnvironmentValues {
    var lkHost: LKHostForm {
        get { self[LKHostFormKey.self] }
        set { self[LKHostFormKey.self] = newValue }
    }
    /// 顶上要给红黄绿让开多高：占整个主窗口时由主窗口给（它的 `lkHostTopInset`），自己的小窗口有标题栏，是 0
    var lkTopInset: CGFloat {
        get { self[LKTopInsetKey.self] }
        set { self[LKTopInsetKey.self] = newValue }
    }
}

private struct LKTopInsetKey: EnvironmentKey { static let defaultValue: CGFloat = 0 }

struct LKWindowSize: ViewModifier {
    let width: CGFloat
    let height: CGFloat?
    @Environment(\.lkHost) private var host

    func body(content: Content) -> some View {
        switch host {
        case .window, .card: content.frame(width: width, height: height)
        case .page: content.frame(maxWidth: .infinity, alignment: .topLeading)        // 主窗口那一栏的宽度，高度由主窗口滚
        case .takeover: content.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

/// 问一句话的小卡片（要权限、开启搜索）：自己的小窗口里四周留 24、宽 460；主窗口的卡片自己留边、定宽，这里铺满它（按钮和 × 对齐）
struct LKCardBody: ViewModifier {
    @Environment(\.lkHost) private var host

    func body(content: Content) -> some View {
        if host == .card { content.frame(maxWidth: .infinity, alignment: .leading) } else { content.padding(24).frame(width: 460) }
    }
}

extension View {
    /// 原来那个小窗口的大小；画在主窗口里时跟着主窗口
    func lkWindowSize(width: CGFloat, height: CGFloat? = nil) -> some View { modifier(LKWindowSize(width: width, height: height)) }
    func lkCardBody() -> some View { modifier(LKCardBody()) }
}

/// 主按钮：lkPrimary 底 + 背景色的字，高 36、圆角 10、14/500（规范 §11：系统 .borderedProminent 深色下字看不清）
/// destructive：只用于把东西移到废纸篓（卸载），lkDanger 底白字（规范 §11 Button）
struct LKPrimaryButton: ButtonStyle {
    var destructive = false
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 14, weight: .medium))
            .foregroundStyle(destructive ? Color.white : Color.lkOnPrimary)
            .padding(.horizontal, 16).frame(height: 36)
            .background((destructive ? Color.lkDanger : Color.lkPrimary).opacity(enabled ? (configuration.isPressed ? 0.85 : 1) : 0.4))
            .clipShape(.rect(cornerRadius: 10))
    }
}

/// 次按钮（outline）和轻按钮（ghost）
struct LKSecondaryButton: ButtonStyle {
    var ghost = false
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 14, weight: .medium))
            .foregroundStyle(Color.lkForeground.opacity(enabled ? 1 : 0.4))
            .padding(.horizontal, 14).frame(height: ghost ? 36 : 32)
            .background(ghost ? (configuration.isPressed ? Color.lkMutedBg : Color.clear) : (configuration.isPressed ? Color.lkMutedBg : Color.lkCard))
            .clipShape(.rect(cornerRadius: 10))
            .overlay { if !ghost { RoundedRectangle(cornerRadius: 10).stroke(Color.lkBorder) } }
    }
}

/// 没有步骤栏的小窗口（卸载、配件回退、主控小窗口、设置、加装）：上面可滚动的正文，下面 64 高的底栏放按钮
struct LKPage<Content: View, Footer: View>: View {
    @ViewBuilder var content: Content
    @ViewBuilder var footer: Footer
    @Environment(\.lkHost) private var host

    var body: some View {
        if host == .page {
            // 主窗口里的一页：标题、滚动、边距是主窗口的；按钮接在正文下面
            VStack(alignment: .leading, spacing: 0) {
                content
                HStack(spacing: 8) { footer }.padding(.top, 20)
            }
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .foregroundStyle(Color.lkForeground)
        } else {
            window
        }
    }

    private var window: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) { content }
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                    .padding(.horizontal, 32).padding(.top, 28).padding(.bottom, 16)
            }
            .scrollIndicators(.never)
            HStack(spacing: 8) { footer }
                .padding(.horizontal, 32).frame(height: 64)
                .overlay(alignment: .top) { Rectangle().fill(Color.lkBorder).frame(height: 1) }
        }
        .background(Color.lkBackground)
        .foregroundStyle(Color.lkForeground)
    }
}

/// 设置行（规范 §11）：左边 16px 图标，中间标题 14 + 说明 13，右边控件
struct LKSettingRow<Control: View>: View {
    let icon: String
    let title: String
    var detail: String? = nil
    @ViewBuilder var control: Control
    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            LKIcon(icon).foregroundStyle(Color.lkMuted).frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 14))
                if let d = detail { Text(d).font(.system(size: 13)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true) }
            }
            Spacer(minLength: 12)
            control
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
    }
}

/// 界面上显示的模型名（brand #15 第 4 条）：去掉量化后缀和 it、a4b 这类变体标记，首字母大写、参数量的 b 大写——
/// gemma-4-26b-a4b-it-8bit → Gemma-4-26B，Qwen3.5-9B-4bit → Qwen3.5-9B。诊断信息照旧写全名
enum ModelName {
    static func display(_ raw: String) -> String {
        var s = raw
        for pat in [#"-\d+bit$"#, #"-it$"#, #"-a\d+b$"#, #"-bf16$"#, #"-fp16$"#] {
            s = s.replacingOccurrences(of: pat, with: "", options: .regularExpression)
        }
        s = s.replacingOccurrences(of: #"(\d)b\b"#, with: "$1B", options: .regularExpression)
        return s.prefix(1).uppercased() + s.dropFirst()
    }
}

/// 「这一版有什么不同」：发布正文的更新说明一节，列在窗口里（检查更新窗口、升级页共用）。有多长画多长，
/// 滚动交给外面的正文区（UpgradeCard、LKPage）：段数看不出实际高度，几段长的就能把窗口撑破
struct LKReleaseNotes: View {
    let text: String
    var body: some View {
        if !text.isEmpty {
            let lines = Self.lines(text)
            Text("这一版有什么不同").font(.system(size: 14, weight: .semibold)).padding(.bottom, 8)
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                    if line.bullet {
                        HStack(alignment: .top, spacing: 8) {
                            Text("•").foregroundStyle(Color.lkMuted)
                            Text(line.text).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
                        }
                    } else {
                        Text(line.text).fixedSize(horizontal: false, vertical: true).padding(.top, 4)
                    }
                }
            }
            .font(.system(size: 13)).tint(.lkForeground)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(Color.lkCard).clipShape(.rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.lkBorder))
        }
    }

    static func lines(_ text: String) -> [(bullet: Bool, text: AttributedString)] {
        text.components(separatedBy: "\n").compactMap { raw in
            let t = raw.trimmingCharacters(in: .whitespaces)
            guard !t.isEmpty else { return nil }
            let bullet = t.hasPrefix("- ") || t.hasPrefix("* ")
            let body = bullet ? String(t.dropFirst(2)) : t
            let a = (try? AttributedString(markdown: body)) ?? AttributedString(body)
            return (bullet, a)
        }
    }
}

/// 应用图标：已知的配件用品牌图，别的用系统符号
struct LKAppIcon: View {
    let accessory: String
    var size: CGFloat = 36
    var body: some View {
        let asset = ["luoke-mail": "app-mail.png", "luoke-ime": "app-ime.png", "luoke-core": "app-core.png",
                     "luoke-scan": "app-scan.png", "luoke-search": "app-search.png"][accessory]
        if let a = asset, let img = LKAsset.image(a) {
            Image(nsImage: img).resizable().frame(width: size, height: size).clipShape(.rect(cornerRadius: size * 0.22))
        } else {
            LKIcon("app-window", size: size * 0.6).foregroundStyle(Color.lkMuted).frame(width: size, height: size)
        }
    }
}

/// 列表组：card 底、1px 边、圆角 12，行之间用分隔线
struct LKGroup<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        VStack(spacing: 0) { content }
            .background(Color.lkCard)
            .clipShape(.rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.lkBorder))
    }
}

/// 提示块：圆角 10、13px，左边一个图标。info 是 muted 底；warning、danger 用对应颜色的淡底和字（规范 §0「Note 警告 / 出错两种底色」）
struct LKNote: View {
    enum Kind { case info, success, warning, danger }
    let icon: String
    let text: String
    var kind: Kind = .info
    private var colors: (fg: Color, text: Color, bg: Color) {
        switch kind {
        case .info: return (.lkForeground, .lkMuted, .lkMutedBg)
        case .success: return (.lkSuccess, .lkSuccess, .lkSuccessSoft)
        case .warning: return (.lkWarning, .lkWarning, .lkWarningSoft)
        case .danger: return (.lkDanger, .lkDanger, Color.lkDanger.opacity(0.12))
        }
    }
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            LKIcon(icon, size: 15).foregroundStyle(colors.fg)
            Text(text).font(.system(size: 13)).foregroundStyle(colors.text).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .background(colors.bg).clipShape(.rect(cornerRadius: 10))
    }
}

/// 分段选择：选中项用 primary 底，不用系统蓝（brand §6，10-23 审计）
struct LKSegmented: View {
    let options: [(String, String)]   // (值, 显示)
    @Binding var selection: String
    var body: some View {
        HStack(spacing: 2) {
            ForEach(options, id: \.0) { v, label in
                let on = v == selection
                Button { selection = v } label: {
                    Text(label).font(.system(size: 12, weight: on ? .medium : .regular))
                        .foregroundStyle(on ? Color.lkOnPrimary : Color.lkForeground)
                        .padding(.horizontal, 12).frame(height: 24)
                        .background(on ? Color.lkPrimary : Color.clear, in: .rect(cornerRadius: 6))
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(2).background(Color.lkMutedBg, in: .rect(cornerRadius: 8))
    }
}

/// 小标记（Badge）
struct LKBadge: View {
    enum Kind { case brand, success, warning, secondary, outline }
    let text: String
    var kind: Kind = .secondary
    var body: some View {
        let (fg, bg): (Color, Color) = {
            switch kind {
            case .brand: return (.lkBrand, .lkBrandSoft)
            case .success: return (.lkSuccess, .lkSuccessSoft)
            case .warning: return (.lkWarning, .lkWarningSoft)
            case .secondary: return (.lkForeground, .lkMutedBg)
            case .outline: return (.lkForeground, .clear)
            }
        }()
        Text(text).font(.system(size: 12, weight: .medium)).foregroundStyle(fg)
            .padding(.horizontal, 7).frame(height: 20)
            .background(bg).clipShape(.rect(cornerRadius: 6))
            .overlay { if kind == .outline { RoundedRectangle(cornerRadius: 6).stroke(Color.lkBorder) } }
    }
}

/// 状态小图标：完成（绿底勾）、进行中（转圈）、等待（空心圆）、没成（红底叉）
struct LKStateIcon: View {
    enum State { case done, busy, wait, fail, later }
    let state: State
    var body: some View {
        switch state {
        case .done:
            LKIcon("check", size: 11).foregroundStyle(.white)
                .frame(width: 16, height: 16).background(Circle().fill(Color.lkSuccess))
        case .busy: ProgressView().controlSize(.small).scaleEffect(0.7).frame(width: 16, height: 16).tint(.lkBrand)
        case .wait: Circle().stroke(Color.lkInput, lineWidth: 1.5).frame(width: 14, height: 14).frame(width: 16, height: 16)
        case .fail:
            LKIcon("x", size: 11).foregroundStyle(.white)
                .frame(width: 16, height: 16).background(Circle().fill(Color.lkDanger))
        case .later:       // 推迟了：warning 色的描边圆 + 叹号
            LKIcon("circle-alert").foregroundStyle(Color.lkWarning)
        }
    }
}

/// 打在主控里的品牌素材（app/Resources/ui，来自 luoke-brand ui/public/img）
/// 一行放不下就换行的横排（输入法添加步骤的标签用）
struct LKFlow: Layout {
    var spacing: CGFloat = 4
    var lineSpacing: CGFloat = 6

    private func rows(_ width: CGFloat, _ subviews: Subviews) -> [[(Int, CGSize)]] {
        var rows: [[(Int, CGSize)]] = [[]]
        var x: CGFloat = 0
        for (i, v) in subviews.enumerated() {
            let size = v.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width { rows.append([]); x = 0 }
            rows[rows.count - 1].append((i, size))
            x += size.width + spacing
        }
        return rows
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        // 分开写、标明类型：写成一个表达式时 release 构建的类型检查超时（internal-1029-3 卡在这里）
        var height: CGFloat = 0, used: CGFloat = 0
        for (n, r) in rows(width, subviews).enumerated() {
            var w: CGFloat = 0, h: CGFloat = 0
            for (_, size) in r { w += size.width; h = max(h, size.height) }
            w += spacing * CGFloat(max(r.count - 1, 0))
            used = max(used, w)
            height += h + (n > 0 ? lineSpacing : 0)
        }
        return CGSize(width: proposal.width ?? used, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for r in rows(bounds.width, subviews) {
            let h = r.map(\.1.height).max() ?? 0
            var x = bounds.minX
            for (i, size) in r {
                subviews[i].place(at: CGPoint(x: x, y: y + (h - size.height) / 2), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += h + lineSpacing
        }
    }
}

enum LKAsset {
    static func image(_ name: String) -> NSImage? {
        if let dir = ProcessInfo.processInfo.environment["LUOKE_UI_ASSETS"] {
            return NSImage(contentsOf: URL(fileURLWithPath: dir).appendingPathComponent(name))
        }
        guard let url = Bundle.main.resourceURL?.appendingPathComponent("ui/\(name)") else { return nil }
        return NSImage(contentsOf: url)
    }
}

/// 安装 / 升级窗口的左栏：螺壳的 logo + 螺壳、步骤（编号圆圈，做完的变成 primary 底加勾，当前步高亮）、底部隐私说明
struct LKRail: View {
    let steps: [String]
    let current: Int
    @Environment(\.lkTopInset) private var topInset
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                if let logo = LKAsset.image("logo.svg") {      // 珊瑚红底方块、奶油海螺，原色画、不裁圆角（luoke-core#38）
                    Image(nsImage: logo).resizable().scaledToFit().frame(width: 24, height: 24)
                }
                Text("螺壳").font(.system(size: 14, weight: .semibold))
            }
            .padding(.horizontal, 4).padding(.bottom, 28)
            .padding(.top, topInset)       // 占整个主窗口：左栏顶到窗口上沿，logo 在红黄绿下面
            VStack(alignment: .leading, spacing: 2) {
                ForEach(Array(steps.enumerated()), id: \.offset) { i, name in
                    HStack(spacing: 10) {
                        ZStack {
                            if i < current {
                                Circle().fill(Color.lkPrimary)
                                LKIcon("check", size: 11).foregroundStyle(Color.lkOnPrimary)
                            } else {
                                Circle().stroke(i == current ? Color.lkPrimary : Color.lkBorder, lineWidth: 1)
                                Text("\(i + 1)").font(.system(size: 11)).foregroundStyle(i == current ? Color.lkForeground : Color.lkMuted)
                            }
                        }
                        .frame(width: 20, height: 20)
                        Text(name).font(.system(size: 13, weight: i == current ? .medium : .regular))
                            .foregroundStyle(i == current ? Color.lkForeground : Color.lkMuted)
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 8).padding(.vertical, 6)
                    .background { if i == current { RoundedRectangle(cornerRadius: 6).fill(Color.lkCard).shadow(color: .black.opacity(0.05), radius: 1, y: 1) } }
                }
            }
            Spacer()
            // brand DESIGN §7：安装栏底部一句隐私说明（10-23 审计恢复）
            Text("模型在这台 Mac 上运行，数据不经过任何云服务。").font(.system(size: 11)).foregroundStyle(Color.lkMuted)
                .fixedSize(horizontal: false, vertical: true).padding(.horizontal, 4)
        }
        .padding(.horizontal, 16).padding(.vertical, 24)
        .frame(width: 200)
        .frame(maxHeight: .infinity)
        .background(Color.lkSidebar)
        .overlay(alignment: .trailing) { Rectangle().fill(Color.lkBorder).frame(width: 1) }
    }
}

/// 页面标题：20/600 + 13 灰的副标题
struct LKTitle: View {
    let title: String
    var sub: String? = nil
    @Environment(\.lkHost) private var host
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            // 主窗口里的一页上面已经有页标题：这一句小一号，当这一步的小标题
            Text(title).font(.system(size: host == .page ? 17 : 20, weight: .semibold)).foregroundStyle(Color.lkForeground)
            if let s = sub { Text(s).font(.system(size: 14)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true) }
        }
        .padding(.bottom, 20)
    }
}

/// 截图用（issue 验收要浅色、深色各一张）：`LuokeCore --render-ui <目录>` 把安装、升级、完成几页用示例数据画成 PNG 就退出，
/// 不起主控、不碰任何安装。素材目录可用 LUOKE_UI_ASSETS 指（直接跑 .build 里的可执行文件时没有 .app 的 Resources）。
enum UIRender {
    @MainActor
    static func run(to dir: String) {
        FirstRunModel.previewing = true
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let apps = [
            AccessoryChoice(id: "luoke-mail", name: "螺壳邮件助手", bundleID: "ai.luoke.mail", type: "app", memory: true, size: 42_000_000, installed: false, unavailable: nil, selected: true),
            AccessoryChoice(id: "luoke-ime", name: "螺壳输入法", bundleID: "ai.luoke.inputmethod.Luoke", type: "input-method", memory: true, size: 33_000_000, installed: false, unavailable: nil, selected: true),
        ]
        let plan = [FirstRunModel.PlanRow(name: "螺壳", detail: "internal-1022", size: "已装好"),
                    FirstRunModel.PlanRow(name: "推理引擎", detail: "oMLX 0.7.0", size: "126 MB"),
                    FirstRunModel.PlanRow(name: "底座模型", detail: "Qwen3.5-9B", size: "5.8 GB"),
                    FirstRunModel.PlanRow(name: "螺壳邮件助手", detail: "0.16.7", size: "42 MB"),
                    FirstRunModel.PlanRow(name: "螺壳输入法", detail: "0.1.11", size: "33 MB")]
        func model(_ step: FirstRunModel.Step) -> FirstRunModel {
            let m = FirstRunModel(preselect: nil)
            m.accessories = apps
            m.plan = plan
            m.diskText = "可用磁盘 214 GB"
            m.expectText = "邮件分析、记忆整理都用它，这台 Mac 跑得动。"
            m.progress = step == .fetching ? "下载中 · 模型 Qwen3.5-9B-4bit 2.1 GB / 5.8 GB" : ""
            m.preflightOK = true
            m.step = step
            return m
        }
        let names = ["luoke-mail", "luoke-ime", "engine", "qwen3.5-9b-4bit"]
        let labels: [String: FirstRunModel.PlanRow] = ["luoke-mail": plan[3], "luoke-ime": plan[4], "engine": plan[1], "qwen3.5-9b-4bit": plan[2]]
        let dl: [String: [String: Any]] = [
            "luoke-mail": ["state": "done", "done": 42_000_000, "total": 42_000_000],
            "luoke-ime": ["state": "done", "done": 33_000_000, "total": 33_000_000],
            "engine": ["state": "done", "done": 126_000_000, "total": 126_000_000],
            "qwen3.5-9b-4bit": ["state": "downloading", "done": 2_100_000_000, "total": 5_800_000_000],
        ]
        func fetching(_ m: FirstRunModel) -> FirstRunModel {
            m.planNames = names
            m.planLabels = labels
            m.previewDownloads = dl
            m.fetchStarted = Date().addingTimeInterval(-120)
            return m
        }
        func done(_ m: FirstRunModel, ime: String?) -> FirstRunModel {
            if let st = ime { m.installedInputMethod = true; m.inputMethodState = st }
            m.notifyRows = [FirstRunModel.NotifyRow(id: "luoke-mail", name: "螺壳邮件助手", bundleID: "ai.luoke.mail", exe: URL(fileURLWithPath: "/"), state: "ask"),
                            FirstRunModel.NotifyRow(id: "luoke-agent", name: "螺壳", bundleID: "ai.luoke.agent", exe: URL(fileURLWithPath: "/"), state: "ok")]
            return m
        }
        let tierModel = model(.preflight)
        tierModel.tierChoices = Manifests.tierChoices(memGB: 128)
        tierModel.chosenTier = tierModel.tierChoices.first?.tier ?? ""
        let cardModel = model(.preflight)
        cardModel.tier = "32"
        let pages: [(String, FirstRunModel)] = [
            ("install-0", cardModel), ("first-tier", tierModel), ("install-1", model(.accessories)), ("install-2", model(.consent)),
            ("install-3", fetching(model(.fetching))), ("install-4", done(model(.done), ime: "not_added")),
            ("done-ime-ready", done(model(.done), ime: "ready")),
            ("done-ime-tutorial-1", { let m = done(model(.done), ime: "not_added"); m.tutorialStep = 0; return m }()),
            ("done-ime-tutorial-3", { let m = done(model(.done), ime: "not_added"); m.tutorialStep = 2; return m }()),
            ("done-ime-relogin", { let m = done(model(.done), ime: "ready"); m.selectFailed = true; m.reregistered = true; return m }()),
        ]
        let notes = """
        ## 更新说明

        **螺壳**
        - 检查更新窗口照规范重做：更新说明直接列在这里，下载有进度。
        - 菜单栏换成单色海螺图标，菜单左右排布。
        """
        // 说明长到正文区放不下时：标题、底栏不动，正文滚
        let longNotes = "## 更新说明\n\n" + (1...14).map { "- 第 \($0) 条：这一条写得比较长，一行放不下，要折成两行才看得完，用来看正文区能不能滚到底，标题和底栏的按钮还在不在。" }.joined(separator: "\n")
        let manifest: [String: Any] = ["items": ["hub": ["files": [["path": "luoke-core.dmg", "size": 41_900_000, "sha256": ""]]]]]
        func upd(_ phase: UpdateModel.Phase, done: Int64 = 0) -> UpdateModel {
            let u = UpdateModel(found: Updater.Found(tag: "internal-1022", page: "https://github.com/luokelabs/luoke-releases", manifest: manifest, notes: notes), autoCheck: false)
            u.phase = phase
            u.done = done
            u.total = 41_900_000
            return u
        }
        let updates: [(String, UpdateModel)] = [
            ("update-checking", upd(.checking)),
            ("update-latest", upd(.latest)),
            ("update-latest-apps", { let u = upd(.latest); u.appsWithUpdates = ["螺壳邮件助手"]; return u }()),
            ("update-found", upd(.found)),
            ("update-found-long", { let u = upd(.found); u.found?.notes = longNotes; return u }()),
            ("update-downloading", upd(.working(step: 0), done: 26_400_000)),
            ("update-offline", upd(.offline("更新服务器没有答复（请求超时）"))),
            ("update-unverified", upd(.unverified("安装包不完整，可能下载时断了"))),
            ("update-failed", upd(.failed(step: 1, "安装包没通过苹果的安全检查"))),
        ]
        for (name, u) in updates {
            for (suffix, scheme) in [("", ColorScheme.light), ("-dark", ColorScheme.dark)] {
                save(AnyView(UpdateView(m: u).environment(\.colorScheme, scheme)), scheme: scheme, to: dir, name: name + suffix, size: NSSize(width: 560, height: 460))
            }
        }
        do {      // 32 GB 机器上的"会装的模型"卡片（只有一个档可选）
            Diagnostics.fakeMemGB = 32
            let m = model(.preflight)
            m.preflight()
            for (suffix, scheme) in [("", ColorScheme.light), ("-dark", ColorScheme.dark)] {
                save(AnyView(FirstRunView(m: m).environment(\.colorScheme, scheme)), scheme: scheme, to: dir, name: "install-0-32gb" + suffix)
            }
            Diagnostics.fakeMemGB = nil
        }
        do {      // 8 GB：内存不够，拦下（样板 first-mem）
            Diagnostics.fakeMemGB = 8
            let m = model(.preflight)
            m.preflight()
            for (suffix, scheme) in [("", ColorScheme.light), ("-dark", ColorScheme.dark)] {
                save(AnyView(FirstRunView(m: m).environment(\.colorScheme, scheme)), scheme: scheme, to: dir, name: "first-mem" + suffix)
            }
            Diagnostics.fakeMemGB = nil
        }
        for (name, m) in pages {
            for (suffix, scheme) in [("", ColorScheme.light), ("-dark", ColorScheme.dark)] {
                save(AnyView(FirstRunView(m: m).environment(\.colorScheme, scheme)), scheme: scheme, to: dir, name: name + suffix)
            }
        }
        // 下载页走到头的几种异常：要假装主控报了某个 /status
        var allDone = dl
        allDone["qwen3.5-9b-4bit"] = ["state": "done", "done": 5_800_000_000, "total": 5_800_000_000]
        func status(smoke: Bool?, deferred: Bool = false, base: String? = "qwen3.5-9b-4bit") -> HubStatus {
            var o: [String: Any] = ["engine_alive": true, "engine_ready": true, "smoke": ["ok": smoke as Any, "deferred": deferred]]
            if let base { o["base"] = base }
            return HubStatus(json: o)
        }
        func after(_ downloads: [String: [String: Any]]) -> FirstRunModel {
            let m = fetching(model(.smoke))
            m.previewDownloads = downloads
            return m
        }
        var failedDl = dl
        failedDl["qwen3.5-9b-4bit"] = ["state": "failed", "done": 2_100_000_000, "total": 5_800_000_000]
        failedDl["luoke-ime"] = ["state": "failed", "done": 0, "total": 33_000_000]
        let fetchFailed = after(failedDl)
        fetchFailed.failed = ["qwen3.5-9b-4bit", "luoke-ime"]
        fetchFailed.failedReasons = ["qwen3.5-9b-4bit": "连不上 ModelScope，检查网络后重试。", "luoke-ime": "下下来了但核对不过，没有装。"]
        let hubDown = fetching(model(.smoke))
        hubDown.previewDownloads = nil
        let statePages: [(String, FirstRunModel, HubStatus?, Int32?)] = [
            ("first-fetch-failed", fetchFailed, status(smoke: nil), nil),
            ("first-smoke-failed", after(allDone), status(smoke: false), nil),
            ("first-smoke-deferred", after(allDone), status(smoke: nil, deferred: true), nil),
            ("first-no-base", after(allDone), status(smoke: nil, base: nil), nil),
            ("first-hub-down", hubDown, nil, 1),
        ]
        for (name, m, st, exit) in statePages {
            HubModel.shared.status = st
            HubModel.shared.hubExit = exit
            for (suffix, scheme) in [("", ColorScheme.light), ("-dark", ColorScheme.dark)] {
                save(AnyView(FirstRunView(m: m).environment(\.colorScheme, scheme)), scheme: scheme, to: dir, name: name + suffix)
            }
        }
        // 小窗口：卸载、配件回退、主控小窗口、设置
        func shot(_ name: String, _ v: AnyView, _ size: NSSize) {
            for (suffix, scheme) in [("", ColorScheme.light), ("-dark", ColorScheme.dark)] {
                save(AnyView(v.environment(\.colorScheme, scheme)), scheme: scheme, to: dir, name: name + suffix, size: size)
            }
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        let un = UninstallModel()
        un.items = [Accessories.Manifest(accessory: "luoke-mail", bundleID: "ai.luoke.mail", name: "螺壳邮件助手", resident: true, type: "app", app: home.appendingPathComponent("Applications/螺壳邮件助手.app"), version: "0.16.7"),
                    Accessories.Manifest(accessory: "luoke-ime", bundleID: "ai.luoke.inputmethod.Luoke", name: "螺壳输入法", resident: false, type: "input-method", app: home.appendingPathComponent("Library/Input Methods/螺壳输入法.app"), version: "0.1.11")]
        un.chosen = "luoke-mail"
        shot("uninstall-pick", AnyView(UninstallView(m: un)), NSSize(width: 560, height: 460))
        let unc = UninstallModel()
        unc.step = .confirm
        unc.chosen = "luoke-mail"
        unc.plans = [UninstallModel.Plan(accessory: "luoke-mail", name: "螺壳邮件助手", type: "app", bundleID: "ai.luoke.mail", app: "~/Applications/螺壳邮件助手.app",
                                         data: ["~/.luoke-mail"], forgetRecords: 312, forgetNotes: 18, memoryNeededBy: ["螺壳输入法"])]
        shot("uninstall-confirm", AnyView(UninstallView(m: unc)), NSSize(width: 560, height: 460))
        let und = UninstallModel()
        und.step = .done
        und.doneText = "螺壳邮件助手已卸载。"
        und.removed = ["~/Applications/螺壳邮件助手.app"]
        und.kept = ["~/.luoke-mail：螺壳邮件助手 的数据，重新安装后接着用", "记忆里 螺壳邮件助手 写的 312 条记录、18 条理解", "~/.luoke/mem.db：你的记忆，卸载从不删它；菜单「记忆」里能看、能删"]
        shot("uninstall-done", AnyView(UninstallView(m: und)), NSSize(width: 560, height: 460))
        let unw = UninstallModel()
        unw.step = .working
        shot("uninstall-working", AnyView(UninstallView(m: unw)), NSSize(width: 560, height: 460))
        let una = UninstallModel()
        una.step = .confirm; una.back = {}; una.chosen = "luoke-mail"; una.plans = unc.plans
        shot("uninstall-from-apps", AnyView(UninstallView(m: una)), NSSize(width: 560, height: 460))
        let ap = AppsModel()
        ap.rows = [AppsModel.Row(id: "luoke-mail", name: "螺壳邮件助手", type: "app", installed: "0.17.13", latest: "0.17.15", size: 42_000_000, unavailable: nil),
                   AppsModel.Row(id: "luoke-ime", name: "螺壳输入法", type: "input-method", installed: nil, latest: "0.1.11", size: 33_000_000, unavailable: nil)]
        shot("apps", AnyView(AppsView(m: ap)), NSSize(width: 560, height: 460))
        let apu = AppsModel()
        apu.rows = [AppsModel.Row(id: "luoke-mail", name: "螺壳邮件助手", type: "app", installed: "0.17.15", latest: "0.17.15", size: 42_000_000, unavailable: nil),
                    AppsModel.Row(id: "luoke-ime", name: "螺壳输入法", type: "input-method", installed: "0.1.11", latest: "0.1.11", size: 33_000_000, unavailable: nil)]
        apu.phase = .done("螺壳邮件助手已升级到 0.17.15", true)
        shot("apps-done", AnyView(AppsView(m: apu)), NSSize(width: 560, height: 460))
        HubModel.shared.status = HubStatus(json: ["tier": "128", "memory_tier": "128", "base": "gemma-4-26b-a4b-8bit", "engine_alive": true, "engine_ready": true,
                                                  "release": "internal-1022", "smoke": ["ok": true], "settings": ["power_mode": "balanced", "background_on_battery": false]])
        shot("home", AnyView(HomeView(model: HubModel.shared)), NSSize(width: 480, height: 520))
        shot("settings", AnyView(SettingsView().environmentObject(HubModel.shared)), NSSize(width: 560, height: 680))
        HubModel.shared.status = nil
        HubModel.shared.hubExit = nil
        let ip = InstallerProgressModel(title: "正在升级到 internal-1022", text: "螺壳会先退出，换好新版后自己重新打开，大约一分钟。")
        shot("installer-progress", AnyView(InstallerProgressView(m: ip)), NSSize(width: 560, height: 460))
        let ipf = InstallerProgressModel(title: "", text: "")
        ipf.failed = "换不上新版（磁盘已满），旧版已放回，照常能用。"
        shot("installer-failed", AnyView(InstallerProgressView(m: ipf)), NSSize(width: 560, height: 460))
        let ipp = InstallerProgressModel(title: "", text: "")
        ipp.failed = "macOS 的「App 管理」权限挡住了这次替换。原来的螺壳已经放回，照常能用。到 系统设置 → 隐私与安全性 → App 管理 里打开「螺壳」。"
        ipp.askSettings = true
        shot("installer-no-permission", AnyView(InstallerProgressView(m: ipp)), NSSize(width: 560, height: 460))
        // 升级的后半段：新版接着显示同样的卡片（补齐、试跑、完成、出错）
        func up(_ set: (UpgradeFlow) -> Void) -> AnyView { let f = UpgradeFlow(from: "internal-1021-3", to: "internal-1022"); set(f); return AnyView(UpgradeView(f: f)) }
        shot("upgrade-fetch", up { $0.line = "正在更新推理引擎 oMLX 0.7.2"; $0.fraction = 0.47 }, NSSize(width: 560, height: 460))
        shot("upgrade-smoke", up { $0.step = 4; $0.line = "正在本机试跑一次…" }, NSSize(width: 560, height: 460))
        shot("upgrade-done", up { $0.done = true; $0.model.releaseNotes = notes }, NSSize(width: 560, height: 460))
        shot("upgrade-done-long", up { $0.done = true; $0.model.releaseNotes = longNotes }, NSSize(width: 560, height: 460))
        shot("upgrade-done-apps", up { $0.done = true; $0.model.releaseNotes = notes; $0.appsWithUpdates = ["螺壳邮件助手"] }, NSSize(width: 560, height: 460))
        shot("upgrade-failed", up { $0.failure = UpgradeFlow.Failure(step: 3, title: "升级未完成", detail: "推理引擎 oMLX 0.7.2 没更新上：连不上更新服务器。没更新上的还是原来的，照常能用。", retry: .downloads) }, NSSize(width: 560, height: 460))
        let api = AppsModel()
        api.rows = [AppsModel.Row(id: "luoke-mail", name: "螺壳邮件助手", type: "app", installed: "0.17.15", latest: "0.17.15", size: 42_000_000, unavailable: nil),
                    AppsModel.Row(id: "luoke-ime", name: "螺壳输入法", type: "input-method", installed: nil, latest: "0.1.11", size: 33_000_000, unavailable: nil)]
        api.phase = .updating("luoke-ime")
        api.progress = "正在下载 61%（20 MB / 33 MB）"
        shot("apps-installing", AnyView(AppsView(m: api)), NSSize(width: 560, height: 460))
        let apm = AppsModel()
        apm.rows = [AppsModel.Row(id: "luoke-mail", name: "螺壳邮件助手", type: "app", installed: "0.17.15", latest: "0.17.15", size: 42_000_000, unavailable: nil),
                    AppsModel.Row(id: "luoke-ime", name: "螺壳输入法", type: "input-method", installed: "0.1.11", latest: "0.1.11", size: 33_000_000, unavailable: nil)]
        apm.phase = .done("螺壳输入法装好了。到 系统设置 → 键盘 → 输入法 里点「+」，添加「螺壳」。", true)
        apm.imeAdded = true
        shot("apps-ime-done", AnyView(AppsView(m: apm)), NSSize(width: 560, height: 460))
        let apup = AppsModel()
        apup.rows = ap.rows
        apup.phase = .updating("luoke-mail")
        apup.progress = "正在下载 40%（17 MB / 42 MB）"
        shot("apps-updating", AnyView(AppsView(m: apup)), NSSize(width: 560, height: 460))
        let aprb = AppsModel()
        aprb.rows = apu.rows
        aprb.phase = .updating("luoke-mail")
        aprb.progress = "正在回到上一版本 0.17.13…"
        shot("apps-rollback", AnyView(AppsView(m: aprb)), NSSize(width: 560, height: 460))
        let aprd = AppsModel()
        aprd.rows = [AppsModel.Row(id: "luoke-mail", name: "螺壳邮件助手", type: "app", installed: "0.17.13", latest: "0.17.13", size: 42_000_000, unavailable: nil), apu.rows[1]]
        aprd.phase = .done("螺壳邮件助手 已回到上一版本 0.17.13", true)
        shot("apps-rollback-done", AnyView(AppsView(m: aprd)), NSSize(width: 560, height: 460))
        let apf = AppsModel()
        apf.rows = api.rows
        apf.phase = .done("螺壳输入法没装上：连不上更新服务器。可以再点「安装」重试。", false)
        shot("apps-install-failed", AnyView(AppsView(m: apf)), NSSize(width: 560, height: 460))
        appsPages(dir: dir)
    }

    /// 主窗口「添加应用」页（对照稿 luoke-agent docs/主窗口原型.html 的「应用」）：目录三段、详情。
    /// 照 1004 宽的主窗口画内容区（减去 220 的侧栏是 784）：主窗口给的页标题（26 半粗，下面空 16）、边距（上 28、左右 32）这里照着画
    @MainActor
    static func appsPages(dir: String) {
        func pageShot(_ name: String, _ v: AnyView, height: CGFloat) {
            let page = AnyView(VStack(alignment: .leading, spacing: 0) {
                Text("应用").font(.system(size: 26, weight: .semibold)).tracking(-0.26).foregroundStyle(Color.lkForeground).padding(.bottom, 16)
                v.fixedSize(horizontal: false, vertical: true)      // 和滚动区里一样按内容的高度画，不被截图的高度撑开
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 32).padding(.top, 28)
            .frame(width: 784, height: height, alignment: .topLeading)
            .background(Color.lkBackground))
            for (suffix, scheme) in [("", ColorScheme.light), ("-dark", ColorScheme.dark)] {
                save(AnyView(page.environment(\.colorScheme, scheme)), scheme: scheme, to: dir, name: name + suffix, size: NSSize(width: 784, height: height))
            }
        }
        typealias Need = AppsModel.Need
        func mail() -> AppsModel.Row {
            var r = AppsModel.Row(id: "luoke-mail", name: "螺壳邮件助手", type: "app", installed: "0.19.46", latest: nil, size: 0, unavailable: nil, bundled: true)
            r.tagline = "用本机模型整理邮箱，每一步都能撤销"
            r.needs = [Need(title: "邮箱", kind: "account", facts: ["用授权码连接，授权码只存在这台 Mac 的钥匙串里", "邮件内容不离开这台 Mac", "不发信，不永久删除"])]
            r.memory = true
            r.reports = ["要你处理的邮件", "可疑邮件", "可以整理的邮件", "按习惯自动处理的邮件", "还没连邮箱"]
            r.actions = ["按方案处理邮件（归档、删除、移到垃圾邮件、加星标、写草稿）", "撤销刚才的处理", "打开搜到的邮件"]
            r.data = ["~/.luoke-mail"]
            r.minTier = "16"
            return r
        }
        func scan() -> AppsModel.Row {
            var r = AppsModel.Row(id: "luoke-scan", name: "螺壳扫描", type: "app", installed: "0.3.27", latest: nil, size: 0, unavailable: nil, bundled: true)
            r.tagline = "它为这台 Mac 加护：拂去你不会想念的，无一不可复还。"
            r.needs = [Need(title: "完全磁盘访问", kind: "system", facts: ["看文件名、大小、修改时间", "重复文件、散落的密钥、AI 的 skill 和说明文件在本机读内容", "结果不出这台 Mac"],
                            tcc: "SystemPolicyAllFiles"),
                       Need(title: "不用点就清 4 类", kind: "delegate", facts: []),
                       Need(title: "删除系统目录里的东西（要 Mac 的密码）", kind: "each-time", facts: [])]
            r.memory = true
            r.reports = ["安全问题", "空间被吃掉", "扫描结果", "需要你允许", "自动清掉的"]
            r.actions = ["执行你点过的清理方案", "把某项标成不用管", "把清理时移进废纸篓的放回原处", "开或关不用点就清的四类"]
            r.data = ["~/.luoke-scan"]
            r.minTier = "16"
            return r
        }
        func search(off: Bool = false) -> AppsModel.Row {
            var r = AppsModel.Row(id: "luoke-search", name: "螺壳搜索", type: "app", installed: "0.1.18", latest: nil, size: 0, unavailable: nil, bundled: true, disabled: off)
            r.tagline = "在这台 Mac 上找文件和照片，答案带出处"
            r.needs = [Need(title: "照片图库", kind: "system", facts: ["仅在开启照片搜索时读取", "只读，不修改图库"], tcc: "Photos"),
                       Need(title: "读你选的文件夹", kind: "each-time", facts: ["仅读取你选择的文件夹", "首次读取时由 macOS 询问"])]
            r.reports = ["螺壳搜索还没选要搜的地方", "螺壳搜索需要你允许读取", "第一遍索引建好了"]
            r.actions = ["改螺壳搜索的设置（搜哪些文件夹、要不要照片图库）", "在 Mac 上打开搜到的文件，或在访达中显示"]
            r.minTier = "16"
            return r
        }
        func ime(installed: String?, latest: String = "0.1.89") -> AppsModel.Row {
            var r = AppsModel.Row(id: "luoke-ime", name: "螺壳输入法", type: "input-method", installed: installed, latest: latest, size: 20_520_779, unavailable: nil)
            r.tagline = "小鹤、自然码双拼和全拼，本机模型帮你挑词、往下联想，从你打的字里学"
            r.memory = true
            r.minTier = "16"
            if installed != nil { r.reports = []; r.actions = []; r.data = ["~/.luoke/ime"] }
            return r
        }
        let catalog = AppsModel()
        catalog.rows = [ime(installed: nil), mail(), scan(), search()]
        pageShot("apps-page", AnyView(AppsView(m: catalog, inWindow: true)), height: 560)
        let withUpdate = AppsModel()
        withUpdate.rows = [ime(installed: "0.1.87"), mail(), scan(), search()]
        pageShot("apps-page-update", AnyView(AppsView(m: withUpdate, inWindow: true)), height: 520)
        pageShot("apps-detail-ime", AnyView(AppDetailView(m: catalog, r: ime(installed: nil))), height: 520)
        let installing = AppsModel()
        installing.rows = [ime(installed: nil)]
        installing.phase = .updating("luoke-ime")
        installing.progress = "正在下载 61%（12.5 MB / 20.5 MB）"
        installing.fraction = 0.61
        pageShot("apps-detail-ime-installing", AnyView(AppDetailView(m: installing, r: installing.rows[0])), height: 520)
        pageShot("apps-detail-scan", AnyView(AppDetailView(m: catalog, r: scan())), height: 820)
        pageShot("apps-detail-mail", AnyView(AppDetailView(m: catalog, r: mail())), height: 820)
        let off = AppsModel()
        off.rows = [ime(installed: "0.1.89"), mail(), scan(), search(off: true)]
        pageShot("apps-page-off", AnyView(AppsView(m: off, inWindow: true)), height: 420)
    }

    @MainActor
    static func save(_ view: AnyView, scheme: ColorScheme, to dir: String, name: String, size: NSSize = NSSize(width: 760, height: 540)) {
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: size)
        host.appearance = NSAppearance(named: scheme == .dark ? .darkAqua : .aqua)
        host.layoutSubtreeIfNeeded()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return }
        host.cacheDisplay(in: host.bounds, to: rep)
        if let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name + ".png"))
        }
    }
}
