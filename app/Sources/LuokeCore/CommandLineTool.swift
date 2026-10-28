import Foundation

/// 命令行工具 `luoke`（luoke_core/cli.py）在 ~/.local/bin 的链接，指向装好的螺壳.app 里固定的位置。
/// 设置里不再有安装入口（10-23 精简）；以前装过的，卸载全部主控时一起删。
enum CommandLineTool {
    static var dir: URL { FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin", isDirectory: true) }
    static var link: URL { dir.appendingPathComponent("luoke") }
    static let inApp = "Contents/Resources/bin/luoke"
    /// 现在应该指向的：装好的那份（/Applications 或 ~/Applications，ADR 0012）
    static var target: URL { (Installer.installed ?? Bundle.main.bundleURL).appendingPathComponent(inApp) }

    /// 链接在、而且指向我们（任何一个认得的位置；别人放的同名文件不认、不碰）
    static var installed: Bool {
        guard let dest = try? FileManager.default.destinationOfSymbolicLink(atPath: link.path) else { return false }
        return Installer.candidates.contains { $0.appendingPathComponent(inApp).path == dest }
    }

    /// 改名、换位置以后：以前装的链接指向别的位置（螺壳主控.app、~/Applications），改指到现在装好的那份。主控启动时调
    static func repoint() {
        guard installed, (try? FileManager.default.destinationOfSymbolicLink(atPath: link.path)) != target.path else { return }
        try? FileManager.default.removeItem(at: link)
        try? FileManager.default.createSymbolicLink(at: link, withDestinationURL: target)
    }

    /// 只删我们放的链接
    static func uninstall() {
        if installed { try? FileManager.default.removeItem(at: link) }
    }
}
