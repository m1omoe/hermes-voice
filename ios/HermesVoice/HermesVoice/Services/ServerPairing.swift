import Foundation

struct ServerPairingPayload: Equatable, Sendable {
    let baseURL: String
    let pairingID: String
    let secret: String

    init(qrString: String) throws {
        guard let querySeparator = qrString.firstIndex(of: "?") else {
            throw ServerPairingError.invalidQR
        }
        let rawQuery = qrString[qrString.index(after: querySeparator)...]
        guard rawQuery.split(separator: "&", omittingEmptySubsequences: false).count == 4 else {
            throw ServerPairingError.invalidQR
        }

        guard let components = URLComponents(string: qrString),
              components.scheme == "harness-voice",
              components.host == "pair",
              components.path.isEmpty,
              components.fragment == nil,
              components.user == nil,
              components.password == nil,
              let queryItems = components.queryItems,
              queryItems.count == 4
        else {
            throw ServerPairingError.invalidQR
        }

        var values: [String: String] = [:]
        for item in queryItems {
            guard let value = item.value,
                  values.updateValue(value, forKey: item.name) == nil
            else {
                throw ServerPairingError.invalidQR
            }
        }

        guard Set(values.keys) == Set(["v", "url", "id", "secret"]),
              values["v"] == "1",
              let baseURL = values["url"],
              let pairingID = values["id"],
              let secret = values["secret"],
              Self.validBackendToken(pairingID, minLength: 16, maxLength: 128),
              Self.validBackendToken(secret, minLength: 32, maxLength: 256),
              Self.validBaseURL(baseURL)
        else {
            throw ServerPairingError.invalidQR
        }

        self.baseURL = baseURL
        self.pairingID = pairingID
        self.secret = secret
    }

    private static func validBackendToken(_ value: String, minLength: Int, maxLength: Int) -> Bool {
        let length = value.utf8.count
        guard (minLength...maxLength).contains(length), !value.isEmpty else { return false }
        return value.unicodeScalars.allSatisfy { scalar in
            switch scalar.value {
            case 48...57, 65...90, 97...122, 45, 95:
                return true
            default:
                return false
            }
        }
    }

    private static func validBaseURL(_ value: String) -> Bool {
        guard let url = URL(string: value),
              url.scheme == "https",
              let host = url.host,
              !host.isEmpty,
              url.fragment == nil,
              url.user == nil,
              url.password == nil
        else {
            return false
        }
        return true
    }
}

enum ServerPairingError: Error {
    case invalidQR
    case noAvailableHarness
    case emptyToken
}

protocol ServerPairingClient {
    func claim(_ payload: ServerPairingPayload) async throws -> String
    func listHarnesses(baseURL: String, token: String) async throws -> [HermesVoiceAPI.HarnessOption]
}

struct PairedServer: Equatable, Sendable {
    let url: String
    let token: String
    let harnesses: [HermesVoiceAPI.HarnessOption]
}

struct LiveServerPairingClient: ServerPairingClient {
    let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func claim(_ payload: ServerPairingPayload) async throws -> String {
        let api = HermesVoiceAPI(baseURL: payload.baseURL, authToken: "", session: session)
        return try await api.claimPairing(payload: payload)
    }

    func listHarnesses(baseURL: String, token: String) async throws -> [HermesVoiceAPI.HarnessOption] {
        let api = HermesVoiceAPI(baseURL: baseURL, authToken: token, session: session)
        return try await api.listHarnesses()
    }
}

struct ServerPairingCoordinator {
    let client: any ServerPairingClient

    init(client: any ServerPairingClient) {
        self.client = client
    }

    func pair(_ payload: ServerPairingPayload) async throws -> PairedServer {
        let token = try await client.claim(payload)
        guard !token.isEmpty else { throw ServerPairingError.emptyToken }

        let harnesses = try await client.listHarnesses(baseURL: payload.baseURL, token: token)
        guard harnesses.contains(where: \.available) else {
            throw ServerPairingError.noAvailableHarness
        }

        return PairedServer(url: payload.baseURL, token: token, harnesses: harnesses)
    }
}
