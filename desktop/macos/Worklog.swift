import AppKit
import WebKit

// A native shell for the same authenticated loopback UI. No remote navigation.
final class WorklogApp: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var process: Process?
    var pipe: Pipe?
    var launchURL: URL?
    var stdoutBuffer = ""
    var generation = 0
    var launchTimer: Timer?
    var watcher: Timer?
    var lastPythonFingerprint = ""
    var lastStaticFingerprint = ""
    let smoke = CommandLine.arguments.contains("--smoke-test")
    let testDB: String? = {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--test-db"), i + 1 < args.count { return args[i + 1] }
        return nil
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 AI Worklog", action: #selector(about), keyEquivalent: "")
        appMenu.addItem(withTitle: "返回上一位置", action: #selector(navigateBack), keyEquivalent: "[")
        appMenu.addItem(withTitle: "重新连接", action: #selector(reconnect), keyEquivalent: "r")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 AI Worklog", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "文件")
        fileMenu.addItem(withTitle: "打开已有工作数据库…", action: #selector(openDatabase), keyEquivalent: "o")
        fileMenu.addItem(withTitle: "在 Finder 中显示数据", action: #selector(showData), keyEquivalent: "")
        fileItem.submenu = fileMenu
        menu.addItem(fileItem)
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "编辑")
        for (title, action, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            editMenu.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        editItem.submenu = editMenu
        menu.addItem(editItem)
        NSApp.mainMenu = menu
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.userContentController.add(self, name: "exportReport")
        configuration.userContentController.add(self, name: "printReport")
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.title = "AI Worklog · 个人工作台"
        window.minSize = NSSize(width: 780, height: 600)
        window.contentView = webView
        window.center()
        if !smoke { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
        start()
    }

    var database: URL {
        if let testDB { return URL(fileURLWithPath: testDB) }
        if let saved = UserDefaults.standard.string(forKey: "database") { return URL(fileURLWithPath: saved) }
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return support.appendingPathComponent("AI Worklog/worklog.sqlite3")
    }

    // WORKLOG_SOURCE points at the repository root for development, so Python and
    // static assets are read from source instead of the copied app bundle.
    var sourceRoot: URL {
        if let path = ProcessInfo.processInfo.environment["WORKLOG_SOURCE"], !path.isEmpty {
            let url = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("worklog/desktop.py").path) { return url }
        }
        return Bundle.main.resourceURL!
    }

    var devMode: Bool { sourceRoot != Bundle.main.resourceURL }

    func python() -> String? {
        // Finder launches with a minimal PATH. Probe installed runtimes without a login shell.
        let candidates = [ProcessInfo.processInfo.environment["WORKLOG_PYTHON"], "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"].compactMap { $0 }
        for candidate in candidates where FileManager.default.isExecutableFile(atPath: candidate) {
            let check = Process()
            check.executableURL = URL(fileURLWithPath: candidate)
            check.arguments = ["-c", "import sys,sqlite3,zoneinfo;sys.exit(0 if sys.version_info >= (3,9) else 1)"]
            check.standardOutput = FileHandle.nullDevice
            check.standardError = FileHandle.nullDevice
            do { try check.run(); check.waitUntilExit(); if check.terminationStatus == 0 { return candidate } } catch { continue }
        }
        return nil
    }

    func start() {
        generation += 1
        let current = generation
        launchURL = nil
        stdoutBuffer = ""
        guard let python = python(), FileManager.default.fileExists(atPath: sourceRoot.path) else {
            fail("未找到 Python 3.9+。请先安装 Python 3，再重新打开 App。")
            return
        }
        let resources = sourceRoot
        let backend = Process()
        let output = Pipe()
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = resources.path
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        environment["PATH"] = "\(home)/.local/bin:\(home)/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (environment["PATH"] ?? "")
        backend.environment = environment
        backend.currentDirectoryURL = resources
        backend.executableURL = URL(fileURLWithPath: python)
        backend.arguments = ["-m", "worklog.desktop", "--db", database.path] + (smoke ? ["--no-discovery"] : [])
        backend.standardOutput = output
        backend.standardError = FileHandle.nullDevice // Never put session content or the access token into a log.
        backend.standardInput = FileHandle.nullDevice
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let bytes = handle.availableData
            guard !bytes.isEmpty, let text = String(data: bytes, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                guard let self, self.generation == current, self.launchURL == nil else { return }
                self.stdoutBuffer += text
                if let range = self.stdoutBuffer.range(of: #"http://127\.0\.0\.1:\d+/#token=[A-Za-z0-9_-]+"#, options: .regularExpression), let url = URL(string: String(self.stdoutBuffer[range])) {
                    self.launchURL = url
                    self.stdoutBuffer = ""
                    self.launchTimer?.invalidate()
                    self.webView.load(URLRequest(url: url))
                }
            }
        }
        backend.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, self.generation == current else { return }
                self.fail("本地服务已停止。可能有另一个实例正在使用该数据库，或数据库缺少原有 identity.key。请检查后重新连接。")
            }
        }
        process = backend
        pipe = output
        do {
            try backend.run()
            startWatcher()
            launchTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: false) { [weak self] _ in
                guard let self, self.generation == current else { return }
                self.fail("启动超过 30 秒。请检查日志目录是否过大、Python 环境和数据库状态。")
                self.stop()
            }
        } catch { fail("无法启动本地服务，请检查 Python 安装及数据库访问权限。") }
    }

    func stop() {
        generation += 1
        launchTimer?.invalidate()
        watcher?.invalidate()
        watcher = nil
        pipe?.fileHandleForReading.readabilityHandler = nil
        process?.terminationHandler = nil
        if let process, process.isRunning { process.interrupt(); process.waitUntilExit() }
        process = nil
        pipe = nil
    }
    // Dev mode only: poll source mtimes and apply edits without quitting the app.
    // Python changes restart the backend; static asset changes reload the page.
    func startWatcher() {
        guard devMode, !smoke else { return }
        lastPythonFingerprint = fingerprint(sourceRoot.appendingPathComponent("worklog"), ["py"])
        lastStaticFingerprint = fingerprint(sourceRoot.appendingPathComponent("static"), ["js", "css", "html", "svg"])
        watcher = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in self?.checkForChanges() }
    }

    func fingerprint(_ directory: URL, _ extensions: Set<String>) -> String {
        guard let enumerator = FileManager.default.enumerator(at: directory, includingPropertiesForKeys: [.contentModificationDateKey, .fileSizeKey], options: [.skipsHiddenFiles]) else { return "" }
        var parts: [String] = []
        for case let url as URL in enumerator where extensions.contains(url.pathExtension) {
            let values = try? url.resourceValues(forKeys: [.contentModificationDateKey, .fileSizeKey])
            parts.append("\(url.path):\(values?.contentModificationDate?.timeIntervalSince1970 ?? 0):\(values?.fileSize ?? 0)")
        }
        return parts.sorted().joined(separator: "|")
    }

    func checkForChanges() {
        let python = fingerprint(sourceRoot.appendingPathComponent("worklog"), ["py"])
        let assets = fingerprint(sourceRoot.appendingPathComponent("static"), ["js", "css", "html", "svg"])
        if python != lastPythonFingerprint {
            lastPythonFingerprint = python
            lastStaticFingerprint = assets
            confirmLeaving { allowed in
                guard allowed else { return }
                self.stop()
                self.start()
            }
        } else if assets != lastStaticFingerprint {
            lastStaticFingerprint = assets
            confirmLeaving { allowed in if allowed { self.webView?.reload() } }
        }
    }

    func fail(_ message: String) {
        launchTimer?.invalidate()
        if smoke { fputs("Desktop smoke failed: \(message)\n", stderr); stop(); exit(1) }
        let alert = NSAlert()
        alert.messageText = "AI Worklog 未就绪"
        alert.informativeText = message
        alert.addButton(withTitle: "知道了")
        alert.runModal()
    }
    func confirmLeaving(_ completion: @escaping (Bool) -> Void) {
        guard webView != nil else { completion(true); return }
        webView.evaluateJavaScript("Boolean(window.worklogTasks?.hasUnsavedWork?.() || window.worklogTasks?.drafts?.size)") { result, _ in
            guard result as? Bool == true else { completion(true); return }
            let alert = NSAlert()
            alert.messageText = "还有未保存或正在提交的工作"
            alert.informativeText = "新建表单、审阅修订或提交请求尚未处理完。继续可能丢失未保存文字；已提交请求也可能已写入。建议返回工作台，等待结果并保存后再继续。"
            alert.addButton(withTitle: "返回编辑")
            alert.addButton(withTitle: "放弃修订并继续")
            completion(alert.runModal() == .alertSecondButtonReturn)
        }
    }
    @objc func navigateBack() { webView?.evaluateJavaScript("goBack(); void 0") }
    @objc func reconnect() {
        confirmLeaving { allowed in if allowed { self.performReconnect() } }
    }
    func performReconnect() {
        if let launchURL, process?.isRunning == true { webView.load(URLRequest(url: launchURL)) }
        else { stop(); start() }
    }
    @objc func openDatabase() {
        confirmLeaving { allowed in if allowed { self.selectDatabase() } }
    }
    func selectDatabase() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.message = "选择已有的 Worklog 数据库；同目录必须保留原 identity.key。不会复制或移动文件。"
        if panel.runModal() == .OK, let url = panel.url {
            stop()
            UserDefaults.standard.set(url.path, forKey: "database")
            start()
        }
    }
    @objc func showData() { NSWorkspace.shared.activateFileViewerSelecting([database]) }
    @objc func about() {
        let alert = NSAlert()
        alert.messageText = "AI Worklog · macOS 预览版"
        alert.informativeText = "本地个人工作台。默认只采集本机 Pi/Codex 元数据；AI 调用需另行启用。退出 App 会停止采集。不安装开机启动项。运行需要已安装 Python 3.9+。"
        alert.runModal()
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if smoke { return .terminateNow }
        confirmLeaving { allowed in
            if !allowed { self.window.makeKeyAndOrderFront(nil) }
            sender.reply(toApplicationShouldTerminate: allowed)
        }
        return .terminateLater
    }
    func applicationWillTerminate(_ notification: Notification) { stop() }

    func isLocal(_ url: URL?) -> Bool {
        guard let url, let launchURL else { return false }
        return url.scheme == "http" && url.host == "127.0.0.1" && url.port == launchURL.port
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(isLocal(navigationAction.request.url) ? .allow : .cancel)
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard smoke else { return }
        webView.evaluateJavaScript("document.querySelector('[data-view=weekly]').click()")
        var attempts = 0
        var reportRequested = false
        var backRequested = false
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] timer in
            guard let self else { timer.invalidate(); return }
            attempts += 1
            let check = backRequested
                ? "!document.querySelector('#weekly').classList.contains('hidden') && document.querySelectorAll('#week-days > details').length === 7"
                : reportRequested
                    ? "document.querySelector('#report-preview > h2')?.textContent.includes('周报') && document.querySelector('#report-period').value === 'week' && document.querySelector('.report-stats strong')?.textContent === '0'"
                    : "document.querySelectorAll('#week-days > details').length === 7 && document.querySelector('#status').textContent.includes('本地数据已读取')"
            self.webView.evaluateJavaScript(check) { result, _ in
                if result as? Bool == true {
                    if !reportRequested {
                        reportRequested = true
                        self.webView.evaluateJavaScript("document.querySelector('[data-view=report]').click()")
                    } else if !backRequested {
                        backRequested = true
                        self.webView.evaluateJavaScript("goBack(); void 0")
                    } else {
                        timer.invalidate(); print("Desktop smoke passed: WKWebView, authenticated backend, weekly records, report, back navigation."); self.stop(); exit(0)
                    }
                }
                if attempts >= 80 { timer.invalidate(); self.fail("桌面页面未能完成加载。") }
            }
        }
    }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, isLocal(message.frameInfo.request.url) else { return }
        if message.name == "printReport" {
            guard let body = message.body as? [String: String], body["action"] == "print" else { return }
            let operation = webView.printOperation(with: NSPrintInfo.shared)
            operation.showsPrintPanel = true
            operation.showsProgressPanel = true
            operation.run()
            return
        }
        guard message.name == "exportReport", let body = message.body as? [String: String], let text = body["text"], text.utf8.count <= 10_000_000,
              let filename = body["filename"], filename.range(of: #"^ai-worklog-(day|week|month)-[0-9-]+\.md$"#, options: .regularExpression) != nil else { return }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = filename
        panel.beginSheetModal(for: window) { response in
            if response == .OK, let url = panel.url {
                do { try text.write(to: url, atomically: true, encoding: .utf8) }
                catch { self.fail("报告保存失败，请检查目标目录权限。") }
            }
        }
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.runModal(); completionHandler()
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: "确认"); alert.addButton(withTitle: "取消"); completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert(); alert.messageText = prompt
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 360, height: 24)); input.stringValue = defaultText ?? ""; alert.accessoryView = input
        alert.addButton(withTitle: "保存"); alert.addButton(withTitle: "取消"); completionHandler(alert.runModal() == .alertFirstButtonReturn ? input.stringValue : nil)
    }
}

let app = NSApplication.shared
let delegate = WorklogApp()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
