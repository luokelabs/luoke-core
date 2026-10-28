// 和主控进程说话：HTTP/1.1 over Unix socket（V1 §2 线上格式）。只做本体需要的几条：status、settings、tasks。
import Foundation

struct HubError: Error, CustomStringConvertible {
    let status: Int
    let code: String
    let message: String
    var description: String { "\(status) \(code): \(message)" }
}

final class HubClient {
    let socketPath: String
    let accessory: String

    init(socketPath: String, accessory: String = "luoke-core/" + AppInfo.version) {
        self.socketPath = socketPath
        self.accessory = accessory
    }

    /// 一次请求一条连接（主控答复带 Connection: close）。阻塞，调用方放后台队列。
    func request(_ method: String, _ path: String, body: [String: Any]? = nil, timeout: TimeInterval = 10) throws -> (Int, [String: Any]) {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw HubError(status: 0, code: "socket", message: String(cString: strerror(errno))) }
        defer { close(fd) }
        var tv = timeval(tv_sec: Int(timeout), tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8CString)
        guard pathBytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else {
            throw HubError(status: 0, code: "socket", message: "socket 路径太长")
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: pathBytes.map { UInt8(bitPattern: $0) })
        }
        let len = socklen_t(MemoryLayout<sockaddr_un>.size)
        let rc = withUnsafePointer(to: &addr) { p in
            p.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, len) }
        }
        guard rc == 0 else { throw HubError(status: 0, code: "connect", message: "连不上螺壳：" + String(cString: strerror(errno))) }

        var payload = Data()
        if let body = body {
            payload = try JSONSerialization.data(withJSONObject: body)
        }
        var head = "\(method) \(path) HTTP/1.1\r\nHost: luoke\r\nX-Luoke-Accessory: \(accessory)\r\nContent-Type: application/json\r\nContent-Length: \(payload.count)\r\nConnection: close\r\n\r\n"
        var out = Data(head.utf8)
        out.append(payload)
        head = ""
        try out.withUnsafeBytes { raw in
            var off = 0
            while off < raw.count {
                let n = write(fd, raw.baseAddress!.advanced(by: off), raw.count - off)
                if n <= 0 { throw HubError(status: 0, code: "write", message: String(cString: strerror(errno))) }
                off += n
            }
        }
        var resp = Data()
        var buf = [UInt8](repeating: 0, count: 65536)
        while true {
            let n = read(fd, &buf, buf.count)
            if n <= 0 { break }
            resp.append(buf, count: n)
        }
        guard let sep = resp.range(of: Data("\r\n\r\n".utf8)) else {
            throw HubError(status: 0, code: "protocol", message: "答复不完整")
        }
        let headText = String(decoding: resp[..<sep.lowerBound], as: UTF8.self)
        let statusLine = headText.split(separator: "\r\n").first ?? ""
        let status = Int(statusLine.split(separator: " ").dropFirst().first ?? "0") ?? 0
        let bodyData = resp[sep.upperBound...]
        let obj = (try? JSONSerialization.jsonObject(with: bodyData)) as? [String: Any] ?? [:]
        if status >= 400 {
            throw HubError(status: status, code: obj["code"] as? String ?? "?", message: obj["message"] as? String ?? obj["error"] as? String ?? "")
        }
        return (status, obj)
    }

    func status() throws -> HubStatus {
        let (_, obj) = try request("GET", "/status")
        return HubStatus(json: obj)
    }

    func settings(_ patch: [String: Any]) throws -> [String: Any] {
        return try request("PUT", "/settings", body: patch).1
    }
}

struct HubStatus {
    let tier: String
    let base: String?
    let loaded: String?
    let engineAlive: Bool
    let engineReady: Bool
    let interactive: Bool
    let backgroundState: String
    let backgroundReasons: [String]
    let job: String?
    let scorers: [String: String]
    let powerMode: String
    let backgroundOnBattery: Bool
    let onAC: Bool
    let breaker: String?
    let memoryTier: String?
    let pendingTier: String?          // 换档等着底座下好的那一档（luoke-core#25）
    let neededDownloads: [String]
    let release: String?
    let localAllowed: [[String: Any]]   // 本机放行的配件版本（只有内部版 / 本机版主控认）
    let downloads: [String: [String: Any]]
    var downloading: Bool { downloads.values.contains { ["queued", "downloading"].contains($0["state"] as? String ?? "") } }
    let smokeOK: Bool?
    let smokeDeferred: Bool
    let phase: String
    let resident: [String: Bool]      // 配件最近一次的常驻声明（主控持久化）
    let exited: [String: Double]      // 配件最近一次正常退出的时刻
    let uptime: Int

    init(json o: [String: Any]) {
        tier = o["tier"] as? String ?? "?"
        memoryTier = o["memory_tier"] as? String
        pendingTier = o["pending_tier"] as? String
        neededDownloads = o["needed_downloads"] as? [String] ?? []
        release = o["release"] as? String
        localAllowed = o["local_allowed"] as? [[String: Any]] ?? []
        downloads = o["downloads"] as? [String: [String: Any]] ?? [:]
        base = o["base"] as? String
        loaded = o["loaded"] as? String
        engineAlive = o["engine_alive"] as? Bool ?? false
        engineReady = o["engine_ready"] as? Bool ?? false
        interactive = o["interactive"] as? Bool ?? false
        let bg = o["background"] as? [String: Any] ?? [:]
        backgroundState = bg["state"] as? String ?? "?"
        backgroundReasons = bg["reasons"] as? [String] ?? []
        job = o["job"] as? String
        scorers = o["scorers"] as? [String: String] ?? [:]
        let st = o["settings"] as? [String: Any] ?? [:]
        powerMode = st["power_mode"] as? String ?? "balanced"
        backgroundOnBattery = st["background_on_battery"] as? Bool ?? false
        onAC = (o["power"] as? [String: Any])?["on_ac"] as? Bool ?? true
        breaker = o["breaker"] as? String
        smokeOK = (o["smoke"] as? [String: Any])?["ok"] as? Bool
        smokeDeferred = (o["smoke"] as? [String: Any])?["deferred"] as? Bool ?? false
        phase = o["phase"] as? String ?? "running"
        resident = o["resident"] as? [String: Bool] ?? [:]
        exited = o["exited"] as? [String: Double] ?? [:]
        uptime = o["uptime"] as? Int ?? 0
    }

    /// 菜单第一行：出错了 / 启动中 / 工作中 / 空闲（和菜单栏图标同一套判断，HubModel.lightState）
    enum State: String { case error = "出错了", starting = "启动中", working = "工作中", idle = "空闲" }
    var state: State {
        if breaker != nil || !engineAlive { return .error }
        if !engineReady { return .starting }
        if job != nil || interactive { return .working }
        return .idle
    }
    var stateValue: String { state.rawValue }

    /// 后台暂停时菜单上多出的一行；在跑就不说
    var backgroundPaused: String? {
        if backgroundState == "running" { return nil }
        let names: [String: String] = ["battery": "在用电池", "low_power": "低电量模式", "thermal": "过热", "memory": "等内存"]
        let why = backgroundReasons.map { names[$0] ?? $0 }.joined(separator: "、")
        return why.isEmpty ? "后台已暂停" : "后台已暂停：\(why)"
    }
}
