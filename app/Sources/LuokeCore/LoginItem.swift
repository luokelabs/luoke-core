// 登录时启动螺壳（SMAppService.mainApp）。每登记一次，macOS 就弹一次「已添加登录项」：首次运行登记一次，
// 之后只在登记找不到了、或螺壳换了位置时重新登记（luoke-core#130：之前每次升级都重新登记，每次都弹）。
import Foundation
import ServiceManagement

enum LoginItem {
    /// 上次登记时螺壳在哪（Bundle.main.bundlePath）
    static let pathKey = "loginItemPath"

    static var status: SMAppService.Status { SMAppService.mainApp.status }

    /// 首次运行、设置里打开「登录时启动螺壳」：登记，记下这一份的位置
    static func register() throws {
        try SMAppService.mainApp.register()
        UserDefaults.standard.set(Bundle.main.bundlePath, forKey: pathKey)
    }

    static func unregister() throws {
        try SMAppService.mainApp.unregister()
        UserDefaults.standard.removeObject(forKey: pathKey)
    }

    /// 升级后、挪走别处的旧副本后：登记还在、位置也对就不碰；登记找不到了或者位置变了才重新登记
    static func repoint() {
        let here = Bundle.main.bundlePath
        let saved = UserDefaults.standard.string(forKey: pathKey)
        let st = status
        if needsRegister(status: st, savedPath: saved, currentPath: here) {
            do { try register() } catch { NSLog("登录项没重新登记上：%@", error.localizedDescription) }
        } else if st == .enabled && saved == nil {
            UserDefaults.standard.set(here, forKey: pathKey)     // 记位置之前的版本登记的：系统里的那条就是这一份，记下来，不重新登记
        }
    }

    /// notRegistered：用户关掉过（或从没登记），不动；requiresApproval：等用户在系统设置里允许，再登记也只是再弹一次；
    /// notFound：登记的那份找不到了；enabled：记下的位置和这一份不一样（改名、换了位置）才重新登记
    static func needsRegister(status: SMAppService.Status, savedPath: String?, currentPath: String) -> Bool {
        switch status {
        case .notFound: return true
        case .enabled: return savedPath.map { $0 != currentPath } ?? false
        default: return false
        }
    }
}
