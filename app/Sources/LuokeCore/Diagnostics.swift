// 导出诊断信息（V1 §9）：只含机型、芯片、内存、档位、版本、任务状态、冒烟结果和速度、错误码、主控日志（只有元数据）。不含请求文字。
import AppKit
import Foundation

enum Diagnostics {
    static func export(model: HubModel) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "螺壳诊断-\(dateStamp()).txt"
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        var lines: [String] = []
        lines.append("螺壳 \(AppInfo.version)  \(ISO8601DateFormatter().string(from: Date()))")
        lines.append("机型：\(sysctl("hw.model"))  芯片：\(sysctl("machdep.cpu.brand_string"))  内存：\(memGB()) GB  macOS \(ProcessInfo.processInfo.operatingSystemVersionString)")
        if let t = UserDefaults.standard.array(forKey: FirstRunModel.timelineKey) as? [[String: Any]], !t.isEmpty {
            let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd HH:mm:ss"
            let steps = t.compactMap { e -> String? in
                guard let s = e["step"] as? String, let at = e["at"] as? Double else { return nil }
                return "\(s)@\(f.string(from: Date(timeIntervalSince1970: at)))"
            }
            lines.append("首次运行各步到达时刻：" + steps.joined(separator: " → ") + (t.last?["step"] as? String == "done" ? "（已完成）" : "（停在 \(t.last?["step"] as? String ?? "?")）"))
        }
        if let st = model.status {
            lines.append("硬件档：\(st.tier)  底座：\(st.base ?? "—")  已加载：\(st.loaded ?? "—")")
            lines.append("引擎：alive=\(st.engineAlive) ready=\(st.engineReady)  熔断：\(st.breaker ?? "无")  冒烟：\(st.smokeOK.map { $0 ? "通过" : "未通过" } ?? "没做")")
            lines.append("后台：\(st.backgroundState) \(st.backgroundReasons)  功耗档：\(st.powerMode)  用电池也运行：\(st.backgroundOnBattery)")
            lines.append("打分进程：\(st.scorers)")
        } else {
            lines.append("螺壳没连上：\(model.hubError ?? "")")
        }
        lines.append("")
        lines.append("== state.json ==")
        lines.append((try? String(contentsOf: AppInfo.envDir.appendingPathComponent("state.json"), encoding: .utf8)) ?? "（无）")
        lines.append("")
        lines.append("== hub.log（最近 200 行，只有元数据）==")
        let log = (try? String(contentsOf: AppInfo.envDir.appendingPathComponent("logs/hub.log"), encoding: .utf8)) ?? ""
        lines.append(log.split(separator: "\n").suffix(200).joined(separator: "\n"))
        lines.append("")
        lines.append("== app.log（最近 50 行）==")
        let app = (try? String(contentsOf: AppInfo.envDir.appendingPathComponent("logs/app.log"), encoding: .utf8)) ?? ""
        lines.append(app.split(separator: "\n").suffix(50).joined(separator: "\n"))
        try? lines.joined(separator: "\n").write(to: url, atomically: true, encoding: .utf8)
    }

    static func sysctl(_ key: String) -> String {
        var size = 0
        guard sysctlbyname(key, nil, &size, nil, 0) == 0, size > 0 else { return "?" }
        var buf = [CChar](repeating: 0, count: size)
        guard sysctlbyname(key, &buf, &size, nil, 0) == 0 else { return "?" }
        return String(cString: buf)
    }

    static var fakeMemGB: Int?          // 只给 --render-ui 截图用：画别的内存档的页面
    static func memGB() -> Int {
        if let f = fakeMemGB { return f }
        return Int(ProcessInfo.processInfo.physicalMemory / (1 << 30))
    }

    static func dateStamp() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyyMMdd-HHmm"
        return f.string(from: Date())
    }

    static func directorySize(_ url: URL) -> Int64 {
        var total: Int64 = 0
        let keys: [URLResourceKey] = [.isRegularFileKey, .fileSizeKey]
        if let e = FileManager.default.enumerator(at: url.resolvingSymlinksInPath(), includingPropertiesForKeys: keys) {
            for case let f as URL in e {
                if let v = try? f.resourceValues(forKeys: Set(keys)), v.isRegularFile == true { total += Int64(v.fileSize ?? 0) }
            }
        }
        return total
    }

    static func humanSize(_ n: Int64) -> String {
        let gb = Double(n) / Double(1 << 30)
        return gb >= 1 ? String(format: "%.1f GB", gb) : String(format: "%.0f MB", Double(n) / Double(1 << 20))
    }
}
