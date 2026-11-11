import SwiftUI
import WebKit

@main
struct SalonMaxApp: App {
    var body: some Scene {
        WindowGroup { PortalView() }
    }
}

struct PortalView: View {
    @AppStorage("salonCode") private var salonCode = ""
    @State private var selection = ""
    @State private var showingSalons = false

    var body: some View {
        NavigationStack {
            PortalWebView(url: portalURL)
                .id(salonCode)
                .navigationTitle("Salon Max")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    Button("Choose salon") {
                        selection = salonCode
                        showingSalons = true
                    }
                }
                .sheet(isPresented: $showingSalons) {
                    NavigationStack {
                        Form {
                            TextField("Salon code", text: $selection)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                            Button("Open salon") {
                                salonCode = selection.trimmingCharacters(in: .whitespacesAndNewlines)
                                showingSalons = false
                            }
                            .disabled(selection.range(of: #"^[a-zA-Z0-9_-]{1,100}$"#, options: .regularExpression) == nil)
                        }
                        .navigationTitle("Choose salon")
                    }
                }
        }
    }

    private var portalURL: URL {
        let root = URL(string: "https://www.salonmax.co.uk/salon/")!
        return salonCode.isEmpty ? root : root.appendingPathComponent(salonCode).appendingPathComponent("")
    }
}

struct PortalWebView: UIViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let view = WKWebView()
        view.navigationDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = true
        view.load(URLRequest(url: url))
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate {
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url, url.scheme == "https" else {
                decisionHandler(.cancel)
                return
            }
            if url.host == "www.salonmax.co.uk", url.path.hasPrefix("/salon/") {
                decisionHandler(.allow)
            } else if url.host == "checkout.stripe.com" {
                decisionHandler(.cancel)
                UIApplication.shared.open(url)
            } else {
                decisionHandler(.cancel)
            }
        }
    }
}
