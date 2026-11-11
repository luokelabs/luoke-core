import AppKit
import SwiftUI

/// 本体窗口里的图标都是 Lucide（lucide-static 1.64.0，ISC，见 app/Resources/lucide/LICENSE），和官网、主窗口一套，不用 SF Symbols
/// （品牌规范 DESIGN.md 第 5 节；菜单栏图标另有规定，不在此列）。只放用到的 SVG，build_app.sh 拷进 Contents/Resources/lucide；
/// 系统按矢量画，当模板图跟着字色。
enum Lucide {
    static let folder = "lucide"

    /// 图标目录：打包好的螺壳在 Contents/Resources/lucide；直接跑 .build 里的程序截图时没有 .app，
    /// 在 LUOKE_UI_ASSETS（app/Resources/ui）旁边的 app/Resources/lucide
    static var dir: URL? {
        if let ui = ProcessInfo.processInfo.environment["LUOKE_UI_ASSETS"] {
            return URL(fileURLWithPath: ui).deletingLastPathComponent().appendingPathComponent(folder, isDirectory: true)
        }
        return Bundle.main.resourceURL?.appendingPathComponent(folder, isDirectory: true)
    }

    private static var cache: [String: NSImage] = [:]
    private static let lock = NSLock()

    /// 名字只认 Lucide 的写法（小写字母、数字、连字符），找不到是 nil
    static func image(_ name: String, in dir: URL? = Lucide.dir) -> NSImage? {
        guard let dir, !name.isEmpty, name.allSatisfy({ $0.isASCII && ($0.isLowercase || $0.isNumber || $0 == "-") }) else { return nil }
        let url = dir.appendingPathComponent(name + ".svg")
        lock.lock()
        defer { lock.unlock() }
        if let hit = cache[url.path] { return hit }
        guard let img = NSImage(contentsOf: url) else { return nil }
        img.isTemplate = true
        cache[url.path] = img
        return img
    }

    /// Menu 的 label 在 macOS 上只认 Text、Image，放不进 LKIcon 这样的视图：给一张定好大小的模板图（图没带上就是空的一块）
    static func menuImage(_ name: String, size: CGFloat = 16) -> Image {
        let img = (image(name)?.copy() as? NSImage) ?? NSImage()
        img.size = NSSize(width: size, height: size)
        img.isTemplate = true
        return Image(nsImage: img)
    }
}

/// 一个 Lucide 图标：默认 16 pt（DESIGN.md 第 5 节），跟着字色。图没带上就空着那块地方
struct LKIcon: View {
    let name: String
    var size: CGFloat = 16

    init(_ name: String, size: CGFloat = 16) {
        self.name = name
        self.size = size
    }

    var body: some View {
        Group {
            if let img = Lucide.image(name) {
                Image(nsImage: img).resizable().renderingMode(.template).aspectRatio(contentMode: .fit)
            } else {
                Color.clear
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// 字 + 左边一个 Lucide 图标（按钮里的「重试」「打开键盘设置」这些）
struct LKLabel: View {
    let title: String
    let icon: String
    var size: CGFloat = 16

    init(_ title: String, icon: String, size: CGFloat = 16) {
        self.title = title
        self.icon = icon
        self.size = size
    }

    var body: some View {
        Label { Text(title) } icon: { LKIcon(icon, size: size) }
    }
}
