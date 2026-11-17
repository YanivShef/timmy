import Foundation
let appDir = URL(fileURLWithPath: CommandLine.arguments[1])
let configURL = appDir.appendingPathComponent("config.json")
func loadConfig() -> [String: Any] {
    guard let d = try? Data(contentsOf: configURL),
          let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return [:] }
    return o
}
func saveConfig(_ updates: [String: Any]) {
    var c = loadConfig()
    for (k, v) in updates { c[k] = v }
    try? FileManager.default.createDirectory(at: appDir, withIntermediateDirectories: true)
    if let d = try? JSONSerialization.data(withJSONObject: c, options: [.prettyPrinted, .withoutEscapingSlashes]) { try? d.write(to: configURL) }
}
saveConfig(["custom_instructions": ["x"]])
