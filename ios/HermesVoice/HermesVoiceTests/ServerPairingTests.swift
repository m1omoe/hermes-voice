import XCTest
@testable import HermesVoice

final class ServerPairingTests: XCTestCase {
    enum Call: Equatable {
        case claim
        case listHarnesses
    }

    final class FakePairingClient: ServerPairingClient {
        let claimResult: Result<String, Error>
        let listResult: Result<[HermesVoiceAPI.HarnessOption], Error>
        private(set) var calls: [Call] = []
        private(set) var claimedPayload: ServerPairingPayload?
        private(set) var listBaseURL: String?
        private(set) var listToken: String?

        init(
            claimResult: Result<String, Error> = .success("server-token"),
            listResult: Result<[HermesVoiceAPI.HarnessOption], Error> = .success([])
        ) {
            self.claimResult = claimResult
            self.listResult = listResult
        }

        func claim(_ payload: ServerPairingPayload) async throws -> String {
            calls.append(.claim)
            claimedPayload = payload
            return try claimResult.get()
        }

        func listHarnesses(baseURL: String, token: String) async throws -> [HermesVoiceAPI.HarnessOption] {
            calls.append(.listHarnesses)
            listBaseURL = baseURL
            listToken = token
            return try listResult.get()
        }
    }

    private let validQR = "harness-voice://pair?v=1&url=https%3A%2F%2Fserver.example%3A8765&id=abcdefghijklmnop&secret=abcdefghijklmnopqrstuvwxyz123456"

    private func makeValidPayload() throws -> ServerPairingPayload {
        try ServerPairingPayload(qrString: validQR)
    }

    private var availableHermes: HermesVoiceAPI.HarnessOption {
        HermesVoiceAPI.HarnessOption(harnessId: "hermes", name: "Hermes", available: true)
    }

    func testParsesVersionOnePairingPayload() throws {
        let payload = try makeValidPayload()

        XCTAssertEqual(payload.baseURL, "https://server.example:8765")
        XCTAssertEqual(payload.pairingID, "abcdefghijklmnop")
        XCTAssertEqual(payload.secret, "abcdefghijklmnopqrstuvwxyz123456")
    }

    func testRejectsUnknownAndDuplicateQueryItems() {
        let invalid = [
            validQR + "&extra=value",
            validQR.replacingOccurrences(of: "&id=abcdefghijklmnop", with: "&id=abcdefghijklmnop&id=qrstuvwxyzabcdef"),
            validQR.replacingOccurrences(of: "?v=1", with: "?v=1&v=1"),
        ]

        for (index, qrString) in invalid.enumerated() {
            XCTAssertThrowsError(try ServerPairingPayload(qrString: qrString), "invalid query case \(index)")
        }
    }

    func testRejectsNonV1AndMalformedPairingURIs() {
        let invalid = [
            validQR.replacingOccurrences(of: "v=1", with: "v=2"),
            "harness-voice://not-pair?v=1&url=https%3A%2F%2Fserver.example&id=abcdefghijklmnop&secret=abcdefghijklmnopqrstuvwxyz123456",
            "harness-voice://pair:123?v=1&url=https%3A%2F%2Fserver.example&id=abcdefghijklmnop&secret=abcdefghijklmnopqrstuvwxyz123456",
            "not a URL",
            "harness-voice://pair?v=1&url=https%3A%2F%2Fserver.example&id=&secret=abcdefghijklmnopqrstuvwxyz123456",
        ]

        for (index, qrString) in invalid.enumerated() {
            XCTAssertThrowsError(try ServerPairingPayload(qrString: qrString), "invalid URI case \(index)")
        }
    }

    func testRejectsRawAndNonCanonicalURLQueryValues() {
        let invalid = [
            validQR.replacingOccurrences(
                of: "url=https%3A%2F%2Fserver.example%3A8765",
                with: "url=https://server.example:8765"
            ),
            validQR.replacingOccurrences(
                of: "url=https%3A%2F%2Fserver.example%3A8765",
                with: "url=https:%2F%2Fserver.example:8765%3Fredirect%3Delsewhere"
            ),
        ]

        for (index, qrString) in invalid.enumerated() {
            XCTAssertThrowsError(try ServerPairingPayload(qrString: qrString), "non-canonical URL case \(index)")
        }
    }

    func testAcceptsProvisionerEncodedURLQueryValue() throws {
        let qrString = validQR.replacingOccurrences(
            of: "url=https%3A%2F%2Fserver.example%3A8765",
            with: "url=https:%2F%2Fserver.example:8765"
        )

        let payload = try ServerPairingPayload(qrString: qrString)

        XCTAssertEqual(payload.baseURL, "https://server.example:8765")
    }

    func testRejectsQueryBearingBaseURL() {
        let qrString = validQR.replacingOccurrences(
            of: "https%3A%2F%2Fserver.example%3A8765",
            with: "https%3A%2F%2Fserver.example%3A8765%3Fredirect%3Delsewhere"
        )

        XCTAssertThrowsError(try ServerPairingPayload(qrString: qrString))
    }

    func testRejectsInsecureOrMalformedBaseURLs() {
        let invalidURLs = [
            "http%3A%2F%2Fserver.example",
            "https%3A%2F%2F",
            "https%3A%2F%2Fuser%3Apass%40server.example",
            "https%3A%2F%2Fserver.example%2Fpath%23fragment",
        ]

        for encodedURL in invalidURLs {
            let qrString = validQR.replacingOccurrences(of: "https%3A%2F%2Fserver.example%3A8765", with: encodedURL)
            XCTAssertThrowsError(try ServerPairingPayload(qrString: qrString), encodedURL)
        }
    }

    func testRejectsPairingValuesOutsideBackendBounds() {
        let tooShortID = validQR.replacingOccurrences(of: "abcdefghijklmnop", with: "short")
        let tooLongSecret = validQR.replacingOccurrences(of: "abcdefghijklmnopqrstuvwxyz123456", with: String(repeating: "a", count: 257))
        let invalidCharacters = validQR.replacingOccurrences(of: "abcdefghijklmnop", with: "abc defghijklmnop")

        XCTAssertThrowsError(try ServerPairingPayload(qrString: tooShortID))
        XCTAssertThrowsError(try ServerPairingPayload(qrString: tooLongSecret))
        XCTAssertThrowsError(try ServerPairingPayload(qrString: invalidCharacters))
    }

    @MainActor
    func testPairingModelRejectsInvalidQRCodeWithoutCallingClient() async {
        let fake = FakePairingClient()
        let model = PairServerSheetModel(client: fake) { _ in }

        await model.handleCode("not-a-pairing-code")

        XCTAssertEqual(fake.calls, [])
        XCTAssertEqual(model.failure, .invalidCode)
    }

    @MainActor
    func testPairingModelExposesRetryableFailureAfterClaimFailure() async throws {
        let fake = FakePairingClient(claimResult: .failure(TestError.failed))
        let model = PairServerSheetModel(client: fake) { _ in }

        await model.handleCode(validQR)

        XCTAssertEqual(fake.calls, [.claim])
        XCTAssertEqual(model.failure, .expiredOrUnavailable)
        XCTAssertTrue(model.canRetry)
    }

    @MainActor
    func testPairingModelInvokesOnPairedOnceForDuplicateScans() async {
        let fake = FakePairingClient(listResult: .success([availableHermes]))
        var pairedCount = 0
        let model = PairServerSheetModel(client: fake) { _ in pairedCount += 1 }

        await model.handleCode(validQR)
        await model.handleCode(validQR)

        XCTAssertEqual(pairedCount, 1)
        XCTAssertEqual(fake.calls, [.claim, .listHarnesses])
    }

    func testCoordinatorClaimsThenAuthenticates() async throws {
        let fake = FakePairingClient(
            listResult: .success([availableHermes])
        )
        let server = try await ServerPairingCoordinator(client: fake).pair(try makeValidPayload())

        XCTAssertEqual(server.token, "server-token")
        XCTAssertEqual(server.url, "https://server.example:8765")
        XCTAssertEqual(server.harnesses, [availableHermes])
        XCTAssertEqual(fake.claimedPayload, try makeValidPayload())
        XCTAssertEqual(fake.listBaseURL, "https://server.example:8765")
        XCTAssertEqual(fake.listToken, "server-token")
        XCTAssertEqual(fake.calls, [.claim, .listHarnesses])
    }

    func testCoordinatorDoesNotListHarnessesAfterClaimFailure() async throws {
        let fake = FakePairingClient(claimResult: .failure(TestError.failed))

        do {
            _ = try await ServerPairingCoordinator(client: fake).pair(try makeValidPayload())
            XCTFail("claim failure should not produce a paired server")
        } catch {
            XCTAssertEqual(fake.calls, [.claim])
        }
    }

    func testCoordinatorRejectsAuthenticatedCheckFailure() async throws {
        let fake = FakePairingClient(
            listResult: .failure(TestError.failed)
        )

        do {
            _ = try await ServerPairingCoordinator(client: fake).pair(try makeValidPayload())
            XCTFail("authenticated harness check failure should not produce a paired server")
        } catch {
            XCTAssertEqual(fake.calls, [.claim, .listHarnesses])
        }
    }

    func testCoordinatorRequiresAnAvailableHarness() async throws {
        let unavailable = HermesVoiceAPI.HarnessOption(harnessId: "hermes", name: "Hermes", available: false)
        let fake = FakePairingClient(listResult: .success([unavailable]))

        do {
            _ = try await ServerPairingCoordinator(client: fake).pair(try makeValidPayload())
            XCTFail("an unavailable-only server should not be paired")
        } catch {
            XCTAssertEqual(fake.calls, [.claim, .listHarnesses])
        }
    }

    func testClaimPairingUsesFixedUnauthenticatedRoute() async throws {
        let payload = try makeValidPayload()
        let capture = URLProtocolStub.RequestCapture()
        URLProtocolStub.setHandler { request in
            capture.record(request)
            return URLProtocolStub.response(json: #"{"token":"server-token"}"#)
        }
        defer { URLProtocolStub.setHandler(nil) }

        let api = HermesVoiceAPI(
            baseURL: payload.baseURL,
            authToken: "must-not-be-sent",
            session: URLProtocolStub.session()
        )
        let token = try await api.claimPairing(payload: payload)

        let request = try XCTUnwrap(capture.request())
        XCTAssertEqual(token, "server-token")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.path, "/api/pairings/claim")
        XCTAssertNil(request.value(forHTTPHeaderField: "X-Hermes-Voice-Token"))
        let body = try XCTUnwrap(URLProtocolStub.bodyData(from: request))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: String])
        XCTAssertEqual(json, ["pairing_id": payload.pairingID, "secret": payload.secret])
    }

    func testListHarnessesUsesAuthenticatedRequest() async throws {
        let capture = URLProtocolStub.RequestCapture()
        URLProtocolStub.setHandler { request in
            capture.record(request)
            return URLProtocolStub.response(json: #"[{"id":"hermes","name":"Hermes","available":true}]"#)
        }
        defer { URLProtocolStub.setHandler(nil) }

        let api = HermesVoiceAPI(
            baseURL: "https://server.example:8765",
            authToken: "server-token",
            session: URLProtocolStub.session()
        )
        let harnesses = try await api.listHarnesses()

        let request = try XCTUnwrap(capture.request())
        XCTAssertEqual(harnesses, [availableHermes])
        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertEqual(request.url?.path, "/api/harnesses")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Hermes-Voice-Token"), "server-token")
    }

    func testClaimPairingRedactsNonSuccessResponseBody() async throws {
        let reflectedBody = "reflected-pairing-secret"
        URLProtocolStub.setHandler { _ in
            URLProtocolStub.response(statusCode: 401, body: reflectedBody)
        }
        defer { URLProtocolStub.setHandler(nil) }

        let api = HermesVoiceAPI(
            baseURL: "https://server.example:8765",
            authToken: "must-not-be-sent",
            session: URLProtocolStub.session()
        )

        do {
            _ = try await api.claimPairing(payload: makeValidPayload())
            XCTFail("claim failure should throw")
        } catch {
            XCTAssertFalse(String(describing: error).contains(reflectedBody))
            XCTAssertTrue(error.localizedDescription.contains("Pairing claim failed"))
        }
    }

    private enum TestError: Error {
        case failed
    }
}

private final class URLProtocolStub: URLProtocol {
    final class RequestCapture {
        private let lock = NSLock()
        private var storedRequest: URLRequest?

        func record(_ request: URLRequest) {
            lock.lock()
            storedRequest = request
            lock.unlock()
        }

        func request() -> URLRequest? {
            lock.lock()
            defer { lock.unlock() }
            return storedRequest
        }
    }

    private static let state = HandlerState()

    static func setHandler(_ handler: ((URLRequest) -> (HTTPURLResponse, Data))?) {
        state.setHandler(handler)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    static func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [URLProtocolStub.self]
        return URLSession(configuration: configuration)
    }

    static func bodyData(from request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
    }

    static func response(json: String) -> (HTTPURLResponse, Data) {
        response(statusCode: 200, body: json)
    }

    static func response(statusCode: Int, body: String) -> (HTTPURLResponse, Data) {
        (
            HTTPURLResponse(
                url: URL(string: "https://server.example:8765")!,
                statusCode: statusCode,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!,
            Data(body.utf8)
        )
    }

    override func startLoading() {
        guard let handler = Self.state.handler() else {
            client?.urlProtocol(self, didFailWithError: TestError.missingHandler)
            return
        }
        let (response, data) = handler(request)
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private final class HandlerState {
        private let lock = NSLock()
        private var storedHandler: ((URLRequest) -> (HTTPURLResponse, Data))?

        func setHandler(_ handler: ((URLRequest) -> (HTTPURLResponse, Data))?) {
            lock.lock()
            storedHandler = handler
            lock.unlock()
        }

        func handler() -> ((URLRequest) -> (HTTPURLResponse, Data))? {
            lock.lock()
            defer { lock.unlock() }
            return storedHandler
        }
    }

    private enum TestError: Error {
        case missingHandler
    }
}
