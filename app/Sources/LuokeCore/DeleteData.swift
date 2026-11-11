// 删除本体里的应用的数据：「应用」窗口「更多操作」里的「删除数据…」，和「停用」分开，要用户确认。
// 带主窗口的这一份是主窗口里的一页（11-10：只有一个窗口），走完回「添加应用」。
// 程序先停，主控记下停用、跑它自己的收尾、删清单声明的数据，记忆里的可选。删完它是停用的，要用时点「开启」。记忆文件不动。
import AppKit
import SwiftUI

enum DeleteData {
    static var window: NSWindow?
    static let hostID = "delete-data"

    static func show(accessory: String, name: String) {
        window?.close()
        let m = DeleteDataModel(accessory: accessory, name: name)
        // 别的页正忙着（正在卸载）时没开（busy），主窗口停在那一页
        if MainWindow.present(hostID, title: "删除数据", style: .page, canLeave: { m.step != .working }, { DeleteDataView(m: m) }) != .noWindow { return }
        let host = NSHostingController(rootView: DeleteDataView(m: m))
        let w = NSWindow(contentViewController: host)
        w.title = "删除数据"
        w.styleMask = [.titled, .closable]
        w.setContentSize(NSSize(width: 560, height: 460))      // 和「应用」窗口一样大
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        AppWindows.track(w)
        Uninstall.bringFront(w)
    }

    /// 关掉，回到「应用」（主窗口的「添加应用」页）
    static func leave() {
        MainWindow.dismiss(hostID)
        window?.close()
        AppsWindow.show()
    }
}

final class DeleteDataModel: ObservableObject {
    enum Step { case loading, confirm, working, done }

    let accessory, name: String
    @Published var step = Step.loading
    @Published var paths: [String] = []
    @Published var records = 0
    @Published var notes = 0
    @Published var forget = false              // 默认不动记忆
    @Published var error: String?              // 主控没答应：什么都没删
    @Published var removed: [String] = []
    @Published var errors: [String] = []

    var canForget: Bool { records + notes > 0 }
    var canDelete: Bool { !paths.isEmpty || forget }
    /// 现在开着：删之前要先停
    var running: Bool { BundledApps.named(accessory)?.enabled == true }

    init(accessory: String, name: String) {
        self.accessory = accessory
        self.name = name
        DispatchQueue.global().async {
            let r = Result { try HubModel.shared.client.request("POST", "/accessories/delete-data", body: ["accessory": accessory, "dry_run": true], timeout: 150).1 }
            DispatchQueue.main.async {
                switch r {
                case .success(let o):
                    self.paths = o["data"] as? [String] ?? []
                    let f = o["forget"] as? [String: Any]
                    self.records = f?["records"] as? Int ?? 0
                    self.notes = f?["notes"] as? Int ?? 0
                case .failure(let e):
                    self.error = (e as? HubError)?.message ?? "\(e)"
                }
                self.step = .confirm
            }
        }
    }

    func run() {
        guard let app = BundledApps.named(accessory) else { return }
        step = .working
        let body: [String: Any] = ["accessory": accessory, "forget": forget && canForget]
        app.stopping({ try HubModel.shared.client.request("POST", "/accessories/delete-data", body: body, timeout: 300).1 }) { r in
            switch r {
            case .success(let o):
                self.removed = o["removed"] as? [String] ?? []
                self.errors = o["errors"] as? [String] ?? []
            case .failure(let e):
                self.error = (e as? HubError)?.message ?? "\(e)"
            }
            self.step = .done
        }
    }
}

struct DeleteDataView: View {
    @ObservedObject var m: DeleteDataModel

    var body: some View {
        LKPage {
            switch m.step {
            case .loading: progress("正在看有哪些数据")
            case .confirm: confirm
            case .working: progress("正在删除")
            case .done: done
            }
        } footer: {
            footer
        }
        .lkWindowSize(width: 560, height: 460)
    }

    private func progress(_ text: String) -> some View {
        VStack(spacing: 12) {
            ProgressView().controlSize(.small)
            Text(text).font(.system(size: 14)).foregroundStyle(Color.lkMuted)
        }
        .frame(maxWidth: .infinity).padding(.top, 140)
    }

    private var confirm: some View {
        VStack(alignment: .leading, spacing: 0) {
            LKTitle(title: "删除\(m.name)的数据", sub: (m.running ? "\(m.name)会先停用。" : "") + "删掉的数据找不回来。")
            if let e = m.error {
                LKNote(icon: "triangle-alert", text: e, kind: .danger)
            } else if m.paths.isEmpty && !m.canForget {
                LKNote(icon: "info", text: "\(m.name)还没有数据。")
            }
            if !m.paths.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(m.paths, id: \.self) { Text($0).font(.system(size: 12, design: .monospaced)).foregroundStyle(Color.lkMuted).textSelection(.enabled) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14).background(Color.lkCard).clipShape(.rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.lkBorder))
            }
            if m.canForget {
                LKGroup {
                    LKSettingRow(icon: "brain", title: "同时从记忆里忘掉它的 \(m.records) 条记录、\(m.notes) 条理解") {
                        Toggle("", isOn: $m.forget).toggleStyle(.switch).controlSize(.small).tint(.lkPrimary).labelsHidden()
                    }
                }
                .padding(.top, 12)
            }
        }
    }

    private var done: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let e = m.error {
                LKTitle(title: "没有删除", sub: e)
            } else {
                LKTitle(title: m.errors.isEmpty ? "数据删了" : "没有全部删掉", sub: "\(m.name)现在是停用的，要用时在「应用」里点「开启」。")
            }
            VStack(alignment: .leading, spacing: 8) {
                ForEach(m.removed, id: \.self) { r in
                    HStack(alignment: .top, spacing: 10) {
                        LKStateIcon(state: .done)
                        Text("已删除 " + r).font(.system(size: 12, design: .monospaced)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
                    }
                }
                ForEach(m.errors, id: \.self) { e in
                    HStack(alignment: .top, spacing: 10) {
                        LKStateIcon(state: .fail)
                        Text(e).font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    @ViewBuilder private var footer: some View {
        switch m.step {
        case .loading, .working:
            Spacer()
        case .confirm:
            Spacer()
            Button("取消") { DeleteData.leave() }.buttonStyle(LKSecondaryButton(ghost: true))
            Button("删除") { m.run() }.buttonStyle(LKPrimaryButton(destructive: true)).disabled(!m.canDelete)
        case .done:
            Spacer()
            Button("完成") { DeleteData.leave() }.buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction)
        }
    }
}
