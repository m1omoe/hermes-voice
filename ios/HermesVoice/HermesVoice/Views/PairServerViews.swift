import AVFoundation
import Observation
import SwiftUI
import UIKit

@MainActor
@Observable
final class PairServerSheetModel {
    enum Failure: Equatable {
        case invalidCode
        case expiredOrUnavailable
        case tlsOrTransport
        case noHarness
        case cameraAccess(String)

        var title: String {
            switch self {
            case .invalidCode: return "Invalid pairing code"
            case .expiredOrUnavailable: return "Pairing code unavailable"
            case .tlsOrTransport: return "Connection failed"
            case .noHarness: return "No harness available"
            case .cameraAccess: return "Camera access needed"
            }
        }

        var message: String {
            switch self {
            case .invalidCode:
                return "That QR code is not a valid Harness Voice pairing code. Scan a new code or use manual setup."
            case .expiredOrUnavailable:
                return "This pairing code may be expired or the server is unavailable. Generate a new code and try again."
            case .tlsOrTransport:
                return "The secure connection to the server failed. Check the server address, network, or Tailscale connection and retry."
            case .noHarness:
                return "The server paired, but no harness is currently available. Start a harness on the Mac and retry."
            case .cameraAccess(let details):
                return details
            }
        }

        var canOpenSettings: Bool {
            if case .cameraAccess = self { return true }
            return false
        }
    }

    let client: any ServerPairingClient
    let onPaired: (PairedServer) -> Void
    private(set) var isPairing = false
    private(set) var didPair = false
    private(set) var failure: Failure?
    private(set) var isActive = true
    private var pairingTask: Task<Void, Never>?
    private var claimedCredential: ClaimedServerCredential?
    private let coordinator: ServerPairingCoordinator

    var canRetry: Bool { failure != nil && !didPair }
    var hasClaimedCredential: Bool { claimedCredential != nil }

    init(client: any ServerPairingClient, onPaired: @escaping (PairedServer) -> Void) {
        self.client = client
        self.onPaired = onPaired
        self.coordinator = ServerPairingCoordinator(client: client)
    }

    func activate() {
        isActive = true
    }

    func deactivate() {
        isActive = false
        pairingTask?.cancel()
        pairingTask = nil
        isPairing = false
    }

    func cancelPairing() {
        deactivate()
        claimedCredential = nil
        failure = nil
    }

    @discardableResult
    func startPairing(_ code: String) -> Task<Void, Never>? {
        guard isActive, !isPairing, !didPair else { return nil }
        pairingTask?.cancel()
        claimedCredential = nil
        let task = Task { [weak self] in
            guard let self else { return }
            await self.handleCode(code)
        }
        pairingTask = task
        return task
    }

    func handleCode(_ code: String) async {
        guard isActive, !isPairing, !didPair else { return }

        let payload: ServerPairingPayload
        do {
            payload = try ServerPairingPayload(qrString: code)
        } catch {
            guard isActive, !Task.isCancelled else { return }
            failure = .invalidCode
            return
        }

        claimedCredential = nil
        isPairing = true
        failure = nil
        defer {
            if !Task.isCancelled { isPairing = false }
        }

        do {
            let attempt = try await coordinator.pairAttempt(payload)
            guard isActive, !Task.isCancelled, !didPair else { return }
            consume(attempt)
        } catch {
            guard isActive, !Task.isCancelled, !didPair else { return }
            failure = Self.failure(for: error)
        }
    }

    func retryClaimedHarnesses() async {
        guard isActive, !isPairing, !didPair, let credential = claimedCredential else { return }

        isPairing = true
        failure = nil
        defer {
            if !Task.isCancelled { isPairing = false }
        }

        do {
            let attempt = try await coordinator.authenticate(credential)
            guard isActive, !Task.isCancelled, !didPair else { return }
            consume(attempt)
        } catch {
            guard isActive, !Task.isCancelled, !didPair else { return }
            failure = Self.failure(for: error)
        }
    }

    @discardableResult
    func startRetryClaimedHarnesses() -> Task<Void, Never>? {
        guard hasClaimedCredential, isActive, !isPairing, !didPair else { return nil }
        pairingTask?.cancel()
        let task = Task { [weak self] in
            guard let self else { return }
            await self.retryClaimedHarnesses()
        }
        pairingTask = task
        return task
    }

    func handleScannerFailure(_ message: String) {
        guard isActive, !didPair else { return }
        failure = .cameraAccess(message)
    }

    func resetScanner() {
        guard !didPair else { return }
        pairingTask?.cancel()
        pairingTask = nil
        isPairing = false
        failure = nil
    }

    func retry() {
        resetScanner()
    }

    private func consume(_ attempt: ServerPairingAttempt) {
        switch attempt {
        case .paired(let paired):
            claimedCredential = nil
            didPair = true
            onPaired(paired)
        case .noAvailableHarness(let credential):
            claimedCredential = credential
            failure = .noHarness
        }
    }

    private static func failure(for error: Error) -> Failure {
        if let error = error as? ServerPairingError {
            if case .noAvailableHarness = error { return .noHarness }
            return .expiredOrUnavailable
        }
        if let error = error as? HermesVoiceAPI.APIError {
            switch error {
            case .transport:
                return .tlsOrTransport
            case .pairingClaimFailed, .httpStatus:
                return .expiredOrUnavailable
            case .badURL, .decode:
                return .tlsOrTransport
            }
        }
        return .expiredOrUnavailable
    }
}

final class CaptureSessionLifecycle {
    private let queue: DispatchQueue
    private let startOnQueue: () -> Void
    private let stopOnQueue: () -> Void
    private var pendingActivation: DispatchWorkItem?

    init(
        queue: DispatchQueue,
        startOnQueue: @escaping () -> Void,
        stopOnQueue: @escaping () -> Void
    ) {
        self.queue = queue
        self.startOnQueue = startOnQueue
        self.stopOnQueue = stopOnQueue
    }

    func activate(configuration: @escaping () -> Bool) {
        pendingActivation?.cancel()
        let box = WorkItemBox()
        let work = DispatchWorkItem { [startOnQueue] in
            guard let work = box.item, !work.isCancelled else { return }
            guard configuration() else { return }
            guard !work.isCancelled else { return }
            startOnQueue()
        }
        box.item = work
        pendingActivation = work
        queue.async(execute: work)
    }

    func deactivate() {
        pendingActivation?.cancel()
        pendingActivation = nil
        queue.async { [stopOnQueue] in
            stopOnQueue()
        }
    }

    private final class WorkItemBox {
        var item: DispatchWorkItem?
    }
}

struct QRCodeScanner: UIViewControllerRepresentable {
    let onCode: (String) -> Void
    let onFailure: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onCode: onCode, onFailure: onFailure)
    }

    func makeUIViewController(context: Context) -> ScannerViewController {
        ScannerViewController(coordinator: context.coordinator)
    }

    func updateUIViewController(_ uiViewController: ScannerViewController, context: Context) {}

    static func dismantleUIViewController(_ uiViewController: ScannerViewController, coordinator: Coordinator) {
        coordinator.stop()
    }

    final class Coordinator: NSObject, AVCaptureMetadataOutputObjectsDelegate {
        private let onCode: (String) -> Void
        private let onFailure: (String) -> Void
        private let session = AVCaptureSession()
        private let captureQueue = DispatchQueue(label: "com.harnessvoice.qr-capture")
        private lazy var lifecycle = CaptureSessionLifecycle(
            queue: captureQueue,
            startOnQueue: { [session] in session.startRunning() },
            stopOnQueue: { [session] in session.stopRunning() }
        )
        private var previewLayer: AVCaptureVideoPreviewLayer?
        private var hasEmittedCode = false
        private var configurationRequested = false
        private var isActive = false
        private var activationID = UUID()

        init(onCode: @escaping (String) -> Void, onFailure: @escaping (String) -> Void) {
            self.onCode = onCode
            self.onFailure = onFailure
        }

        func configure(in view: UIView) {
            isActive = true
            let activation = UUID()
            activationID = activation
            guard !configurationRequested else {
                previewLayer?.frame = view.bounds
                return
            }

            switch AVCaptureDevice.authorizationStatus(for: .video) {
            case .authorized:
                configureSession(in: view, activation: activation)
            case .notDetermined:
                AVCaptureDevice.requestAccess(for: .video) { [weak self, weak view] granted in
                    DispatchQueue.main.async {
                        guard let self, let view,
                              self.isActive,
                              self.activationID == activation else { return }
                        if granted {
                            self.configureSession(in: view, activation: activation)
                        } else {
                            self.onFailure(Self.cameraAccessMessage)
                        }
                    }
                }
            case .denied, .restricted:
                onFailure(Self.cameraAccessMessage)
            @unknown default:
                onFailure(Self.cameraAccessMessage)
            }
        }

        func stop() {
            isActive = false
            activationID = UUID()
            configurationRequested = false
            lifecycle.deactivate()
        }

        func metadataOutput(
            _ output: AVCaptureMetadataOutput,
            didOutput metadataObjects: [AVMetadataObject],
            from connection: AVCaptureConnection
        ) {
            guard !hasEmittedCode,
                  let code = metadataObjects
                    .compactMap({ $0 as? AVMetadataMachineReadableCodeObject })
                    .first(where: { $0.type == .qr })?
                    .stringValue,
                  !code.isEmpty
            else { return }

            hasEmittedCode = true
            session.stopRunning()
            DispatchQueue.main.async { [weak self] in
                guard let self, self.isActive else { return }
                self.onCode(code)
            }
        }

        private func configureSession(in view: UIView, activation: UUID) {
            guard !configurationRequested else { return }
            configurationRequested = true
            lifecycle.activate { [weak self, weak view] in
                guard let self else { return false }
                guard let device = AVCaptureDevice.default(for: .video) else {
                    self.reportFailure("This device has no camera available for QR scanning.", activation: activation)
                    return false
                }
                do {
                    let input = try AVCaptureDeviceInput(device: device)
                    let output = AVCaptureMetadataOutput()
                    guard self.session.canAddInput(input), self.session.canAddOutput(output) else {
                        self.reportFailure("The camera could not be configured for QR scanning.", activation: activation)
                        return false
                    }
                    self.session.beginConfiguration()
                    self.session.addInput(input)
                    self.session.addOutput(output)
                    output.setMetadataObjectsDelegate(self, queue: self.captureQueue)
                    output.metadataObjectTypes = [.qr]
                    self.session.commitConfiguration()

                    DispatchQueue.main.async { [weak self, weak view] in
                        guard let self, let view,
                              self.isActive,
                              self.activationID == activation else { return }
                        let previewLayer = AVCaptureVideoPreviewLayer(session: self.session)
                        previewLayer.videoGravity = .resizeAspectFill
                        previewLayer.frame = view.bounds
                        view.layer.insertSublayer(previewLayer, at: 0)
                        self.previewLayer = previewLayer
                    }
                    return true
                } catch {
                    self.reportFailure("The camera could not be configured for QR scanning.", activation: activation)
                    return false
                }
            }
        }

        private func reportFailure(_ message: String, activation: UUID) {
            DispatchQueue.main.async { [weak self] in
                guard let self,
                      self.isActive,
                      self.activationID == activation else { return }
                self.onFailure(message)
            }
        }

        private static let cameraAccessMessage = "Camera access is denied or restricted. Open Settings to allow camera access for local server pairing QR scanning, or use manual setup."
    }
}

final class ScannerViewController: UIViewController {
    private let coordinator: QRCodeScanner.Coordinator

    init(coordinator: QRCodeScanner.Coordinator) {
        self.coordinator = coordinator
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        coordinator.configure(in: view)
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        view.layer.sublayers?.compactMap { $0 as? AVCaptureVideoPreviewLayer }.forEach {
            $0.frame = view.bounds
        }
    }
}

struct PairServerSheet: View {
    let onPaired: (PairedServer) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var model: PairServerSheetModel
    @State private var step: Step = .setup
    @State private var scannerID = UUID()
    @State private var showingFailureAlert = false
    @State private var didCopyPrompt = false

    private enum Step { case setup, scan }

    init(onPaired: @escaping (PairedServer) -> Void) {
        self.onPaired = onPaired
        _model = State(initialValue: PairServerSheetModel(client: LiveServerPairingClient(), onPaired: onPaired))
    }

    init(client: any ServerPairingClient, onPaired: @escaping (PairedServer) -> Void) {
        self.onPaired = onPaired
        _model = State(initialValue: PairServerSheetModel(client: client, onPaired: onPaired))
    }

    var body: some View {
        NavigationStack {
            ZStack {
                HVColor.bg.ignoresSafeArea()
                if step == .setup {
                    setupContent
                } else {
                    scanContent
                }
            }
            .navigationTitle(step == .setup ? "SET UP SERVER" : "SCAN PAIRING CODE")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                        .accessibilityHint("Closes server pairing setup")
                }
            }
            .toolbarBackground(HVColor.bg, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .toolbarColorScheme(.dark, for: .navigationBar)
        }
        .tint(HVColor.amber)
        .preferredColorScheme(.dark)
        .onAppear { model.activate() }
        .onDisappear { model.deactivate() }
        .onChange(of: model.failure) { _, newValue in
            showingFailureAlert = newValue != nil
        }
        .alert(model.failure?.title ?? "Pairing problem", isPresented: $showingFailureAlert) {
            if model.failure?.canOpenSettings == true {
                Button("Open Settings") {
                    guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
                    UIApplication.shared.open(url)
                }
            }
            Button("Retry scanning") { retryScanning() }
            Button("Manual setup") { showManualSetup() }
            Button("Cancel", role: .cancel) { restoreScanner() }
        } message: {
            Text(model.failure?.message ?? "Try scanning the pairing code again.")
        }
    }

    private var setupContent: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 8) {
                    Image(systemName: "desktopcomputer.and.arrow.down")
                        .font(.title2.weight(.semibold))
                        .foregroundStyle(HVColor.amber)
                        .accessibilityHidden(true)
                    Text("SET UP ON YOUR MAC")
                        .font(.title3.monospaced().weight(.semibold))
                        .tracking(0.8)
                        .foregroundStyle(HVColor.amber)
                    Text("Run the local provisioner on the Mac that hosts your harness. It starts the server and displays a one-time pairing QR code.")
                        .font(.body.monospaced())
                        .foregroundStyle(HVColor.creamDim)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Text("The server can be reached on the same Wi-Fi or through Tailscale. Keep the QR code private while pairing; it expires after use.")
                    .font(.footnote.monospaced())
                    .foregroundStyle(HVColor.creamDim)
                    .fixedSize(horizontal: false, vertical: true)

                Button(action: copySetupPrompt) {
                    HStack {
                        Image(systemName: didCopyPrompt ? "checkmark" : "doc.on.doc")
                            .accessibilityHidden(true)
                        Text(didCopyPrompt ? "Setup prompt copied" : "Copy setup prompt")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .accessibilityLabel(didCopyPrompt ? "Setup prompt copied" : "Copy setup prompt")
                .accessibilityHint("Copies credential-free setup instructions for the public repository")

                Button("Continue to scan") {
                    step = .scan
                    model.activate()
                    model.resetScanner()
                    scannerID = UUID()
                }
                .frame(maxWidth: .infinity)
                .buttonStyle(.bordered)
                .accessibilityHint("Opens the QR scanner")

                manualFallbackButton
            }
            .padding(24)
        }
    }

    private var scanContent: some View {
        VStack(spacing: 18) {
            QRCodeScanner(
                onCode: { code in
                    model.startPairing(code)
                },
                onFailure: { message in
                    model.handleScannerFailure(message)
                }
            )
            .id(scannerID)
            .clipShape(.rect(cornerRadius: 16))
            .overlay {
                RoundedRectangle(cornerRadius: 16)
                    .stroke(HVColor.amber.opacity(0.7), lineWidth: 2)
                    .padding(16)
            }
            .frame(minHeight: 280)
            .padding(.horizontal, 20)
            .accessibilityLabel("QR code scanner")
            .accessibilityHint("Point the camera at the local server pairing QR code")

            Text(model.isPairing ? "Pairing with your server…" : "Point your camera at the one-time pairing QR code.")
                .font(.body.monospaced())
                .foregroundStyle(HVColor.creamDim)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)

            if model.didPair {
                HStack {
                    Image(systemName: "checkmark.circle.fill")
                        .accessibilityHidden(true)
                    Text("Server paired")
                }
                .font(.body.monospaced().weight(.semibold))
                .foregroundStyle(HVColor.amber)
                .accessibilityLabel("Server paired successfully")
            }

            manualFallbackButton
        }
        .padding(.vertical, 20)
    }

    private var manualFallbackButton: some View {
        Button("Use manual setup instead") { showManualSetup() }
            .frame(maxWidth: .infinity)
            .buttonStyle(.plain)
            .foregroundStyle(HVColor.creamDim)
            .accessibilityHint("Returns to instructions for setting up the local server without scanning")
    }

    private func copySetupPrompt() {
        UIPasteboard.general.string = Self.setupPrompt
        didCopyPrompt = true
    }

    private func retryScanning() {
        let retryClaim = model.hasClaimedCredential
        model.resetScanner()
        model.activate()
        showingFailureAlert = false
        step = .scan
        scannerID = UUID()
        if retryClaim {
            model.startRetryClaimedHarnesses()
        }
    }

    private func restoreScanner() {
        model.resetScanner()
        model.activate()
        showingFailureAlert = false
        step = .scan
        scannerID = UUID()
    }

    private func showManualSetup() {
        model.cancelPairing()
        showingFailureAlert = false
        step = .setup
    }

    private static let setupPrompt = """
    Set up Harness Voice from the public repository https://github.com/TaylorFinklea/hermes-voice.

    From the repository root, run the Task 4 provisioner command:
    uv run --project backend --extra provision python backend/scripts/provision_macos.py setup

    Follow the prompts on the Mac, then display the one-time pairing QR code for the iPhone.
    """
}
