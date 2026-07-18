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
              components.port == nil,
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
              let rawURLValue = Self.rawQueryValue(named: "url", in: rawQuery),
              Self.validEncodedURLQueryValue(rawURLValue, decoded: baseURL),
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

    private static func rawQueryValue(named name: String, in rawQuery: Substring) -> String? {
        var matches: [String] = []
        for item in rawQuery.split(separator: "&", omittingEmptySubsequences: false) {
            guard let separator = item.firstIndex(of: "=") else { continue }
            guard String(item[..<separator]) == name else { continue }
            matches.append(String(item[item.index(after: separator)...]))
        }
        return matches.count == 1 ? matches.first : nil
    }

    private static func validEncodedURLQueryValue(_ rawValue: String, decoded value: String) -> Bool {
        let provisionerAllowed = CharacterSet(
            charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:"
        )
        let fullyEncodedAllowed = CharacterSet(
            charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
        )
        return rawValue == value.addingPercentEncoding(withAllowedCharacters: provisionerAllowed)
            || rawValue == value.addingPercentEncoding(withAllowedCharacters: fullyEncodedAllowed)
    }

    private static func validBaseURL(_ value: String) -> Bool {
        guard let components = URLComponents(string: value),
              components.scheme == "https",
              let host = components.host,
              !host.isEmpty,
              components.query == nil,
              components.fragment == nil,
              components.user == nil,
              components.password == nil
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

struct ClaimedServerCredential: Equatable, Sendable {
    let baseURL: String
    let token: String
}

enum ServerPairingAttempt: Sendable {
    case paired(PairedServer)
    case noAvailableHarness(ClaimedServerCredential)
}

struct ServerPairingCoordinator {
    let client: any ServerPairingClient

    init(client: any ServerPairingClient) {
        self.client = client
    }

    func claim(_ payload: ServerPairingPayload) async throws -> ClaimedServerCredential {
        let token = try await client.claim(payload)
        guard !token.isEmpty else { throw ServerPairingError.emptyToken }
        return ClaimedServerCredential(baseURL: payload.baseURL, token: token)
    }

    func authenticate(_ credential: ClaimedServerCredential) async throws -> ServerPairingAttempt {
        let harnesses = try await client.listHarnesses(baseURL: credential.baseURL, token: credential.token)
        guard harnesses.contains(where: \.available) else {
            return .noAvailableHarness(credential)
        }
        return .paired(PairedServer(url: credential.baseURL, token: credential.token, harnesses: harnesses))
    }

    func pairAttempt(_ payload: ServerPairingPayload) async throws -> ServerPairingAttempt {
        try await authenticate(claim(payload))
    }

    func pair(_ payload: ServerPairingPayload) async throws -> PairedServer {
        switch try await pairAttempt(payload) {
        case .paired(let server):
            return server
        case .noAvailableHarness:
            throw ServerPairingError.noAvailableHarness
        }
    }
}
