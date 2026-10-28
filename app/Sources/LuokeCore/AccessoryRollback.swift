import AppKit
import Foundation
import SwiftUI

/// 配件回到上一版（issue #12，10-20 定进 beta1）：换装时旧版留在 previous.noindex，这里把它换回来。
/// 对调和核签名由主控进程做（GET /accessories/previous、POST /accessories/rollback，只有主控自己的身份能调）；本体负责：
/// 问用户、结束普通配件的进程（按 bundle id；输入法由主控按路径发 SIGTERM，系统下次要用时从换回的包里起）、
/// 记住"不要自动再升到换下来的那一版"（升级页按它跳过）、常驻配件换好后再拉起。
enum AccessoryRollback {
    static let skipKey = "skipAccessoryVersions"      // [配件标识: 不再自动换上的版本]

    /// 升级页用：这个配件的这个版本是用户回滚掉的，不自动再换上
    static func skipped(_ accessory: String, _ version: String) -> Bool {
        (UserDefaults.standard.dictionary(forKey: skipKey) as? [String: String])?[accessory] == version
    }

    private static func remember(_ accessory: String, skip version: String) {
        var d = (UserDefaults.standard.dictionary(forKey: skipKey) as? [String: String]) ?? [:]
        d[accessory] = version
        UserDefaults.standard.set(d, forKey: skipKey)
    }

    static func label(_ c: [String: Any]) -> String {
        "\(c["current"] as? String ?? "") → \(c["previous"] as? String ?? "")"
    }

    /// 换回：结束普通配件的进程、请主控对调、记住跳过换下来的那一版、常驻配件再拉起。done(标题, 说明, 成没成)
    static func run(_ c: [String: Any], done: @escaping (String, String, Bool) -> Void) {
        guard let acc = c["accessory"] as? String, let bid = c["bundle_id"] as? String else { return }
        let isApp = (c["type"] as? String ?? "app") == "app"
        if isApp { Accessories.noRestart.insert(bid) }       // 主线程上记好"别重启"
        DispatchQueue.global().async {
            if isApp { Uninstall.terminate(bundleID: bid, markNoRestart: false) }
            var result: Result<[String: Any], Error>
            do {
                let (_, o) = try HubModel.shared.client.request("POST", "/accessories/rollback", body: ["accessory": acc], timeout: 120)
                result = .success(o)
            } catch {
                result = .failure(error)
            }
            DispatchQueue.main.async {
                Accessories.noRestart.remove(bid)
                switch result {
                case .success(let o):
                    let from = o["from"] as? String ?? "", to = o["to"] as? String ?? ""
                    remember(acc, skip: from)
                    if isApp { Accessories.launchResident() }
                    done("\(c["name"] as? String ?? acc) 已回到上一版本 \(to)",
                         (isApp ? "" : "下次切换到螺壳输入法时用的就是这一版。") + "换下来的 \(from) 还留着，想用它的话，在这一行的「…」里再选一次就行。", true)
                case .failure(let e):
                    if isApp { Accessories.launchResident() }
                    done("没能回到上一版本", (e as? HubError)?.message ?? "\(e)", false)
                }
            }
        }
    }
}
